from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time
from typing import Any

from src.markets import CRYPTO, MARKET_TYPES, normalize_market_type

PositionProbe = Callable[[], int | bool | Awaitable[int | bool]]
RiskCheck = Callable[[], Any | Awaitable[Any]]


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass
class MarketRuntimeState:
    market_type: str
    heavy_connections: int = 0
    risk_polling: bool = False
    known_position_count: int = 0
    last_position_check_ms: int = 0
    last_error: str = ""
    updated_at_ms: int = 0
    heavy_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    risk_task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "market_type": self.market_type,
            "heavy_connections": self.heavy_connections,
            "risk_polling": self.risk_polling,
            "known_position_count": self.known_position_count,
            "last_position_check_ms": self.last_position_check_ms,
            "last_error": self.last_error,
            "updated_at_ms": self.updated_at_ms,
        }


class MarketStateManager:
    """Owns expensive market resources independently from position safety.

    A chart WebSocket registers itself as a heavy task. Switching the active
    market cancels heavy tasks for every inactive market. If an inactive market
    still reports a position, a low-frequency REST risk callback remains alive
    until that market has no position left.
    """

    def __init__(self, *, risk_poll_seconds: float = 20.0):
        self.risk_poll_seconds = max(5.0, float(risk_poll_seconds))
        self.active_market = CRYPTO
        self._states = {market: MarketRuntimeState(market) for market in MARKET_TYPES}
        self._position_probes: dict[str, PositionProbe] = {}
        self._risk_checks: dict[str, RiskCheck] = {}
        self._lock = asyncio.Lock()

    def configure_market(self, market_type: str, *, position_probe: PositionProbe | None = None, risk_check: RiskCheck | None = None) -> None:
        market = normalize_market_type(market_type)
        if position_probe is not None:
            self._position_probes[market] = position_probe
        if risk_check is not None:
            self._risk_checks[market] = risk_check

    def set_known_position_count(self, market_type: str, count: int) -> None:
        state = self._states[normalize_market_type(market_type)]
        state.known_position_count = max(0, int(count))
        state.updated_at_ms = int(time() * 1000)

    async def update_known_position_count(self, market_type: str, count: int) -> dict[str, Any]:
        self.set_known_position_count(market_type, count)
        await self._refresh_inactive_risk_loops()
        return self.snapshot()

    async def attach_heavy_task(self, market_type: str, task: asyncio.Task | None = None) -> bool:
        market = normalize_market_type(market_type)
        task = task or asyncio.current_task()
        if task is None:
            raise RuntimeError("attach_heavy_task requires a running asyncio task")
        async with self._lock:
            if market != self.active_market:
                task.cancel()
                return False
            state = self._states[market]
            state.heavy_tasks.add(task)
            state.heavy_connections = len(state.heavy_tasks)
            state.updated_at_ms = int(time() * 1000)
        task.add_done_callback(lambda done: self._discard_heavy_task(market, done))
        return True

    def _discard_heavy_task(self, market_type: str, task: asyncio.Task) -> None:
        state = self._states[market_type]
        state.heavy_tasks.discard(task)
        state.heavy_connections = len(state.heavy_tasks)
        state.updated_at_ms = int(time() * 1000)

    async def detach_heavy_task(self, market_type: str, task: asyncio.Task | None = None) -> None:
        task = task or asyncio.current_task()
        if task is not None:
            self._discard_heavy_task(normalize_market_type(market_type), task)

    async def switch_active(self, market_type: str) -> dict[str, Any]:
        market = normalize_market_type(market_type)
        async with self._lock:
            self.active_market = market
            for name, state in self._states.items():
                state.updated_at_ms = int(time() * 1000)
                if name != market:
                    for task in tuple(state.heavy_tasks):
                        if not task.done():
                            task.cancel()
                else:
                    self._stop_risk_task(state)
        await self._refresh_inactive_risk_loops()
        return self.snapshot()

    async def _position_count(self, market_type: str) -> int:
        state = self._states[market_type]
        probe = self._position_probes.get(market_type)
        if probe is None:
            return state.known_position_count
        try:
            value = await _resolve(probe())
            if value is not None:
                count = int(value) if not isinstance(value, bool) else int(value)
                state.known_position_count = max(0, count)
                state.last_error = ""
        except Exception as exc:
            # Preserve the previous count on a probe failure. A known open
            # position must keep its risk loop rather than being discarded.
            state.last_error = f"position_probe:{exc}"
        state.last_position_check_ms = int(time() * 1000)
        return state.known_position_count

    async def _refresh_inactive_risk_loops(self) -> None:
        for market, state in self._states.items():
            if market == self.active_market:
                self._stop_risk_task(state)
                continue
            positions = await self._position_count(market)
            if positions > 0:
                self._start_risk_task(market, state)
            else:
                self._stop_risk_task(state)

    def _start_risk_task(self, market_type: str, state: MarketRuntimeState) -> None:
        if state.risk_task is not None and not state.risk_task.done():
            state.risk_polling = True
            return
        state.risk_task = asyncio.create_task(self._risk_loop(market_type), name=f"risk-poll-{market_type}")
        state.risk_polling = True

    def _stop_risk_task(self, state: MarketRuntimeState) -> None:
        if state.risk_task is not None and not state.risk_task.done():
            state.risk_task.cancel()
        state.risk_task = None
        state.risk_polling = False

    async def _risk_loop(self, market_type: str) -> None:
        state = self._states[market_type]
        try:
            while market_type != self.active_market:
                if await self._position_count(market_type) <= 0:
                    break
                check = self._risk_checks.get(market_type)
                if check is not None:
                    try:
                        await _resolve(check())
                        state.last_error = ""
                    except Exception as exc:
                        state.last_error = f"risk_check:{exc}"
                await asyncio.sleep(self.risk_poll_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            if state.risk_task is asyncio.current_task():
                state.risk_task = None
            state.risk_polling = False
            state.updated_at_ms = int(time() * 1000)

    async def shutdown(self) -> None:
        async with self._lock:
            for state in self._states.values():
                for task in tuple(state.heavy_tasks):
                    if not task.done():
                        task.cancel()
                self._stop_risk_task(state)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_market": self.active_market,
            "markets": {name: state.public() for name, state in self._states.items()},
        }
