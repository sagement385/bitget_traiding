from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from src.data_engine.storage import DEFAULT_DB, upsert_candles
from src.models import Candle


@dataclass(frozen=True, slots=True)
class CandleWrite:
    market_type: str
    symbol: str
    category: str
    interval: str
    candle: Candle
    source: str

    @classmethod
    def from_candle(cls, candle: Candle, source: str | None = None) -> "CandleWrite":
        return cls(
            market_type=candle.market_type,
            symbol=candle.symbol,
            category=candle.category,
            interval=candle.interval,
            candle=candle,
            source=source or candle.source,
        )


class CandleWriteBuffer:
    """Single async writer that coalesces repeated updates by candle key."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB,
        flush_interval_seconds: float = 1.0,
        max_batch_size: int = 500,
        max_pending: int = 5000,
    ) -> None:
        self.db_path = db_path
        self.flush_interval_seconds = max(0.05, float(flush_interval_seconds))
        self.max_batch_size = max(1, int(max_batch_size))
        self.max_pending = max(self.max_batch_size, int(max_pending))
        self._pending: dict[tuple[str, str, str, str, int], CandleWrite] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.last_error: str | None = None
        self.flush_count = 0
        self.write_count = 0
        self.failed_flush_count = 0
        self.max_queue_depth = 0
        self.overflow_count = 0
        self.last_flush_duration_ms = 0.0

    @property
    def queue_depth(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._run(), name="candle-db-writer")

    async def put(self, item: CandleWrite) -> None:
        await self.start()
        key = (
            item.market_type,
            item.symbol,
            item.category,
            item.interval,
            item.candle.open_time_ms,
        )
        self._pending[key] = item
        self.max_queue_depth = max(self.max_queue_depth, len(self._pending))
        if len(self._pending) > self.max_pending:
            self.overflow_count += 1
            oldest = next(iter(self._pending))
            self._pending.pop(oldest, None)
        if len(self._pending) >= self.max_batch_size or item.candle.is_closed:
            self._wake.set()

    async def put_many(self, items: Iterable[CandleWrite]) -> None:
        for item in items:
            await self.put(item)

    async def put_frame(self, frame: pd.DataFrame, *, source: str) -> None:
        if frame is None or frame.empty:
            return
        items: list[CandleWrite] = []
        for row in frame.to_dict(orient="records"):
            try:
                candle = Candle.from_row(row)
                items.append(CandleWrite.from_candle(candle, source=source))
            except (TypeError, ValueError, KeyError):
                continue
        await self.put_many(items)

    async def flush(self) -> None:
        self._wake.set()
        if self._task is not None:
            await asyncio.sleep(0)

    async def close(self) -> None:
        if self._task is None:
            return
        self._closing = True
        self._wake.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._closing or self._pending:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.flush_interval_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._pending:
                await self._flush_pending()

    async def _flush_pending(self) -> None:
        batch = list(self._pending.values())
        self._pending.clear()
        if not batch:
            return
        started = time.perf_counter()
        try:
            grouped: dict[tuple[str, str], list[CandleWrite]] = {}
            for item in batch:
                grouped.setdefault((item.source, item.market_type), []).append(item)
            for (source, market_type), items in grouped.items():
                frame = pd.DataFrame([item.candle.to_row() for item in items])
                written = await asyncio.to_thread(
                    upsert_candles,
                    frame,
                    self.db_path,
                    source,
                    50_000,
                    market_type,
                )
                self.write_count += int(written or 0)
            self.last_error = None
        except Exception as exc:  # persistence must not stop the live chart
            self.failed_flush_count += 1
            self.last_error = str(exc)
        finally:
            self.flush_count += 1
            self.last_flush_duration_ms = (time.perf_counter() - started) * 1000

    def consume_error(self) -> str | None:
        error = self.last_error
        self.last_error = None
        return error
