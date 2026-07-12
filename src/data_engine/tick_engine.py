from __future__ import annotations

import asyncio
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import pandas as pd

from src.bitget.public_client import BitgetPublicClient
from src.bitget.websocket_client import PUBLIC_WS, trade_topic
from src.data_engine.candle_stream import merge_candle_frames
from src.data_engine.canonical import bucket_start_ms as canonical_bucket_start_ms
from src.data_engine.storage import save_csv, upsert_candles
from src.utils.time import INTERVAL_MS, now_ms

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None


SECOND_INTERVALS = {"1s", "3s", "5s", "15s", "30s"}
REALTIME_AGG_INTERVALS = SECOND_INTERVALS | {"1m", "2m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M"}


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    size: float
    side: str
    timestamp: int


@dataclass(frozen=True)
class TickStreamConfig:
    symbol: str = "BTCUSDT"
    category: str = "USDT-FUTURES"
    interval: str = "1s"
    cache_path: str | None = None
    write_csv: bool = False
    tick_path: str | None = None
    initial_limit: int = 1000
    fallback_poll_sec: float = 7.5
    max_rows: int = 6000


def bucket_start_ms(ts_ms: int, interval: str) -> int:
    """Use the same exchange buckets as cached and historical candles."""
    ts_ms = int(ts_ms)
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval for tick aggregation: {interval}")
    return canonical_bucket_start_ms(ts_ms, interval)


class TickStore:
    """Small append-only CSV tick store.

    It is intentionally simple. For 24/7 heavy capture this can be replaced by
    PostgreSQL/TimescaleDB without touching the chart or strategy interfaces.
    """

    def __init__(self, path: str | Path | None = None, max_buffer: int = 20000):
        self.path = Path(path) if path else None
        self.max_buffer = max_buffer
        self.buffer: list[Tick] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                with self.path.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["timestamp", "symbol", "price", "size", "side"])

    def append_many(self, ticks: Iterable[Tick]) -> None:
        rows = list(ticks)
        if not rows:
            return
        self.buffer.extend(rows)
        if len(self.buffer) > self.max_buffer:
            self.buffer = self.buffer[-self.max_buffer :]
        if self.path:
            with self.path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for t in rows:
                    w.writerow([t.timestamp, t.symbol, t.price, t.size, t.side])


class CandleAggregator:
    """Build OHLCV candles from tick events.

    The current candle is mutable. Every tick either revises the active bucket
    or starts a new bucket. This is the same update model that chart engines use:
    `series.update(current_candle)` for same time, append when time advances.
    """

    def __init__(self, symbol: str, category: str, interval: str, seed: pd.DataFrame | None = None):
        if interval not in REALTIME_AGG_INTERVALS:
            raise ValueError(f"Unsupported realtime interval: {interval}")
        self.symbol = symbol.upper()
        self.category = category
        self.interval = interval
        self.current: dict[str, Any] | None = None
        if seed is not None and not seed.empty:
            last = seed.sort_values("timestamp").iloc[-1]
            self.current = {
                "timestamp": int(last["timestamp"]),
                "open": float(last["open"]),
                "high": float(last["high"]),
                "low": float(last["low"]),
                "close": float(last["close"]),
                "volume": float(last.get("volume", 0) or 0),
                "turnover": float(last.get("turnover", 0) or 0),
                "symbol": self.symbol,
                "category": self.category,
                "interval": self.interval,
            }

    def update(self, tick: Tick) -> dict[str, Any]:
        b = bucket_start_ms(tick.timestamp, self.interval)
        price = float(tick.price)
        size = max(0.0, float(tick.size or 0.0))
        turnover = price * size
        if self.current is None or int(self.current["timestamp"]) != b:
            self.current = {
                "timestamp": b,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": size,
                "turnover": turnover,
                "symbol": self.symbol,
                "category": self.category,
                "interval": self.interval,
            }
        else:
            self.current["high"] = max(float(self.current["high"]), price)
            self.current["low"] = min(float(self.current["low"]), price)
            self.current["close"] = price
            self.current["volume"] = float(self.current.get("volume", 0) or 0) + size
            self.current["turnover"] = float(self.current.get("turnover", 0) or 0) + turnover
        return dict(self.current)


def candles_from_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    for c in ["timestamp", "open", "high", "low", "close", "volume", "turnover"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["timestamp", "open", "high", "low", "close"]).drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)[cols]


def parse_trade_message(msg: dict[str, Any], symbol: str) -> list[Tick]:
    """Normalize Bitget public trade WS payloads.

    Bitget may return trade rows as dicts or arrays depending on channel/version.
    This parser accepts both to keep the stream resilient.
    """
    data = msg.get("data") or []
    ticks: list[Tick] = []
    for item in data:
        try:
            if isinstance(item, dict):
                price = float(item.get("price") or item.get("p") or item.get("px"))
                size = float(item.get("size") or item.get("qty") or item.get("sz") or item.get("amount") or 0)
                ts = int(item.get("ts") or item.get("time") or item.get("timestamp") or now_ms())
                side = str(item.get("side") or item.get("S") or "").lower()
            else:
                # Common compact layouts include [ts, price, size, side] or [price, size, side, ts].
                vals = list(item)
                if len(vals) >= 4 and str(vals[0]).isdigit() and len(str(vals[0])) >= 12:
                    ts, price, size, side = int(vals[0]), float(vals[1]), float(vals[2]), str(vals[3]).lower()
                elif len(vals) >= 4:
                    price, size, side, ts = float(vals[0]), float(vals[1]), str(vals[2]).lower(), int(vals[3])
                else:
                    continue
            if math.isfinite(price) and price > 0:
                ticks.append(Tick(symbol=symbol.upper(), price=price, size=max(size, 0.0), side=side, timestamp=ts))
        except Exception:
            continue
    return ticks


class LiveTickCandleStream:
    """REST bootstrap + Bitget trade tick WebSocket + internal candle aggregator.

    This stream supports true tick-driven charting. Seconds candles are created
    locally from trades. Minute/hour/day candles are bootstrapped from REST then
    revised from incoming ticks so the current candle moves without reloading the
    full chart.
    """

    def __init__(self, config: TickStreamConfig):
        self.config = config
        self.public = BitgetPublicClient()
        self.cache = pd.DataFrame()
        self.tick_store = TickStore(config.tick_path)
        self.aggregator: CandleAggregator | None = None
        self.last_status = "init"

    def _save(self) -> None:
        if not self.cache.empty:
            upsert_candles(self.cache, source="bitget_tick_live")
        if self.config.write_csv and self.config.cache_path and not self.cache.empty:
            save_csv(self.cache, self.config.cache_path)

    def _seed_from_rest(self) -> pd.DataFrame:
        interval = self.config.interval
        if interval in SECOND_INTERVALS:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval"])
        return self.public.get_recent_candles(self.config.symbol, self.config.category, interval, limit=min(1000, max(50, self.config.initial_limit)))

    def initial(self, cached: pd.DataFrame | None = None) -> pd.DataFrame:
        frames = []
        if cached is not None and not cached.empty:
            frames.append(cached)
        try:
            rest = self._seed_from_rest()
            if not rest.empty:
                frames.append(rest)
            self.last_status = "rest_bootstrap_ok"
        except Exception as exc:
            self.last_status = f"rest_bootstrap_failed: {exc}"
        if frames:
            self.cache = merge_candle_frames(pd.DataFrame(), pd.concat(frames, ignore_index=True), max_rows=self.config.max_rows)
        self.aggregator = CandleAggregator(self.config.symbol, self.config.category, self.config.interval, seed=self.cache)
        self._save()
        return self.cache.copy()

    async def _ws_messages(self) -> AsyncIterator[dict[str, Any]]:
        if websockets is None:
            raise RuntimeError("websockets package is not installed")
        topic = trade_topic(self.config.symbol, self.config.category)
        async with websockets.connect(PUBLIC_WS, ping_interval=None, close_timeout=5) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    data = json.loads(raw)
                    if data.get("event"):
                        self.last_status = f"ws_{data.get('event')}"
                        continue
                    yield data
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(25)
            await ws.send("ping")

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        if self.aggregator is None:
            self.initial()
        backoff = 1.0
        while True:
            try:
                async for msg in self._ws_messages():
                    ticks = parse_trade_message(msg, self.config.symbol)
                    if not ticks:
                        continue
                    self.tick_store.append_many(ticks)
                    updated_rows = [self.aggregator.update(t) for t in ticks] if self.aggregator else []
                    df = candles_from_rows(updated_rows)
                    if df.empty:
                        continue
                    self.cache = merge_candle_frames(self.cache, df, max_rows=self.config.max_rows)
                    self._save()
                    backoff = 1.0
                    latest = df.iloc[-1]
                    yield {
                        "type": "candle_update",
                        "engine": "tick_aggregator",
                        "transport": "trade_websocket",
                        "tickCount": len(ticks),
                        "candle": self._row_to_chart(latest),
                        "rows": int(len(self.cache)),
                        "lastTick": {"price": ticks[-1].price, "size": ticks[-1].size, "side": ticks[-1].side, "timestamp": ticks[-1].timestamp},
                    }
            except Exception as exc:
                self.last_status = f"ws_error: {exc}"
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)
                # Fallback is intentionally low-frequency REST candle merge, not tight polling.
                try:
                    if self.config.interval not in SECOND_INTERVALS:
                        df = self._seed_from_rest().tail(5)
                        self.cache = merge_candle_frames(self.cache, df, max_rows=self.config.max_rows)
                        if self.aggregator:
                            self.aggregator = CandleAggregator(self.config.symbol, self.config.category, self.config.interval, seed=self.cache)
                        self._save()
                        yield {"type": "status", "engine": "tick_aggregator", "transport": "rest_fallback", "message": str(exc), "rows": int(len(self.cache))}
                        for _, row in df.tail(2).iterrows():
                            yield {"type": "candle_update", "engine": "tick_aggregator", "transport": "rest_fallback", "candle": self._row_to_chart(row), "rows": int(len(self.cache))}
                    else:
                        yield {"type": "status", "engine": "tick_aggregator", "transport": "offline", "message": f"trade WS failed; second candles need live ticks: {exc}", "rows": int(len(self.cache))}
                except Exception as rest_exc:
                    yield {"type": "status", "engine": "tick_aggregator", "transport": "offline", "message": f"WS and REST failed: {rest_exc}", "rows": int(len(self.cache))}

    @staticmethod
    def _row_to_chart(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
        get = row.get if isinstance(row, dict) else lambda k, default=None: row.get(k, default)
        return {
            "time": int(int(get("timestamp")) / 1000),
            "open": float(get("open")),
            "high": float(get("high")),
            "low": float(get("low")),
            "close": float(get("close")),
            "volume": float(get("volume", 0) or 0),
        }
