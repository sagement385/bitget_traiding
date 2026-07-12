from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Literal, Mapping

import pandas as pd

from src.models import Candle


Action = Literal["inserted", "updated", "historical_patch", "ignored"]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    action: Action
    candle_closed: Candle | None = None
    evicted: bool = False


class CandleStore:
    """Bounded, ordered in-memory candle state for live chart updates.

    The common path only looks at the last candle and appends/replaces it. The
    timestamp index is rebuilt only when a historical correction arrives or a
    full snapshot replaces the store.
    """

    def __init__(
        self,
        *,
        symbol: str = "BTCUSDT",
        category: str = "USDT-FUTURES",
        interval: str = "1m",
        market_type: str = "CRYPTO",
        max_rows: int = 6000,
    ) -> None:
        self.symbol = str(symbol).upper()
        self.category = str(category).upper()
        self.interval = str(interval)
        self.market_type = str(market_type).upper()
        self.max_rows = max(1, int(max_rows))
        self._candles: deque[Candle] = deque(maxlen=self.max_rows)
        self._by_time: dict[int, Candle] = {}

    @property
    def size(self) -> int:
        return len(self._candles)

    def last(self) -> Candle | None:
        return self._candles[-1] if self._candles else None

    def values(self) -> list[Candle]:
        return list(self._candles)

    def replace_all(self, candles: list[Candle] | tuple[Candle, ...] | Any) -> None:
        normalized: dict[int, Candle] = {}
        for raw in candles or []:
            candle = self._coerce(raw)
            if candle is not None:
                normalized[candle.open_time_ms] = candle
        ordered = sorted(normalized.values(), key=lambda item: item.open_time_ms)[-self.max_rows :]
        self._candles = deque(ordered, maxlen=self.max_rows)
        self._rebuild_index()

    def apply(self, raw: Candle | Mapping[str, Any] | pd.Series) -> ApplyResult:
        candle = self._coerce(raw)
        if candle is None or not self._matches_context(candle):
            return ApplyResult("ignored")

        last = self.last()
        if last is None:
            self._candles.append(candle)
            self._by_time[candle.open_time_ms] = candle
            return ApplyResult("inserted", evicted=False)

        if candle.open_time_ms == last.open_time_ms:
            self._candles[-1] = candle
            self._by_time[candle.open_time_ms] = candle
            return ApplyResult("updated", candle_closed=candle if candle.is_closed else None)

        if candle.open_time_ms > last.open_time_ms:
            closed = last if not last.is_closed else None
            was_full = len(self._candles) == self.max_rows
            if was_full:
                self._by_time.pop(self._candles[0].open_time_ms, None)
            self._candles.append(candle)
            self._by_time[candle.open_time_ms] = candle
            return ApplyResult("inserted", candle_closed=closed, evicted=was_full)

        # Historical patches are rare and deliberately take the slower path.
        self._by_time[candle.open_time_ms] = candle
        ordered = {item.open_time_ms: item for item in self._candles}
        ordered[candle.open_time_ms] = candle
        values = sorted(ordered.values(), key=lambda item: item.open_time_ms)[-self.max_rows :]
        self._candles = deque(values, maxlen=self.max_rows)
        self._rebuild_index()
        return ApplyResult("historical_patch")

    def to_frame(self) -> pd.DataFrame:
        columns = [
            "timestamp", "open", "high", "low", "close", "volume", "turnover",
            "symbol", "category", "interval", "market_type", "is_closed", "source",
        ]
        if not self._candles:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame([c.to_row() for c in self._candles], columns=columns)

    def _matches_context(self, candle: Candle) -> bool:
        return (
            candle.symbol == self.symbol
            and candle.category == self.category
            and candle.interval == self.interval
            and candle.market_type == self.market_type
        )

    def _rebuild_index(self) -> None:
        self._by_time = {candle.open_time_ms: candle for candle in self._candles}

    def _coerce(self, raw: Candle | Mapping[str, Any] | pd.Series | None) -> Candle | None:
        if raw is None:
            return None
        if isinstance(raw, Candle):
            return raw
        if isinstance(raw, pd.Series):
            raw = raw.to_dict()
        if not isinstance(raw, Mapping):
            return None
        try:
            return Candle.from_row(
                raw,
                defaults={
                    "symbol": self.symbol,
                    "category": self.category,
                    "interval": self.interval,
                    "market_type": self.market_type,
                },
            )
        except (TypeError, ValueError):
            return None
