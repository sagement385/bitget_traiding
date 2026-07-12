from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import pandas as pd

from src.bitget.public_client import BitgetPublicClient, normalize_bitget_candles
from src.bitget.websocket_client import PUBLIC_WS, candle_topic
from src.data_engine.canonical import aggregate_from_1m, bucket_start_ms
from src.data_engine.storage import save_csv, upsert_candles

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None


@dataclass(frozen=True)
class StreamConfig:
    symbol: str = "BTCUSDT"
    category: str = "USDT-FUTURES"
    interval: str = "1m"
    cache_path: str | None = None
    write_csv: bool = False
    initial_limit: int = 1000
    fallback_poll_sec: float = 5.0
    max_rows: int = 6000


def merge_candle_frames(old: pd.DataFrame, new: pd.DataFrame, max_rows: int = 5000) -> pd.DataFrame:
    """Merge initial REST candles with live WS candles.

    Same timestamp means the currently open candle has been revised; the new row
    replaces the old row. This mirrors chart libraries' `series.update()` model:
    closed candles are stable; the latest candle is mutable until it closes.
    """
    frames = [x for x in (old, new) if x is not None and not x.empty]
    if not frames:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval"])
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df["timestamp"] = df["timestamp"].astype("int64")
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").tail(max_rows).reset_index(drop=True)
    return df


class LiveCandleStream:
    """REST bootstrap + Bitget WebSocket live K-line stream.

    Flow:
    1. Fetch recent candles once via REST so the chart has context.
    2. Subscribe to Bitget public candle WebSocket.
    3. Yield only revised/new candles from the WebSocket.
    4. If WS fails, keep the chart alive with low-frequency REST fallback.
    """

    def __init__(self, config: StreamConfig):
        self.config = config
        self.public = BitgetPublicClient()
        self.cache = pd.DataFrame()
        self.last_emit_ts = 0
        self.last_status = "init"

    def _save(self) -> None:
        if self.config.write_csv and self.config.cache_path and not self.cache.empty:
            save_csv(self.cache, self.config.cache_path)

    def _persist(self, df: pd.DataFrame) -> None:
        if df is not None and not df.empty:
            upsert_candles(df, source="bitget_live")

    def initial(self, cached: pd.DataFrame | None = None) -> pd.DataFrame:
        limit = min(max(50, int(self.config.initial_limit)), 1000)
        df = self.public.get_recent_candles(self.config.symbol, self.config.category, self.config.interval, limit=limit)
        seed = cached if cached is not None and not cached.empty else self.cache
        self.cache = merge_candle_frames(seed, df, max_rows=self.config.max_rows)
        self._persist(df)
        self._save()
        self.last_status = "rest_bootstrap_ok"
        return self.cache.copy()

    def _normalize_ws_message(self, msg: dict[str, Any]) -> pd.DataFrame:
        rows = msg.get("data") or []
        if not rows:
            return pd.DataFrame()
        return normalize_bitget_candles(rows, self.config.symbol.upper(), self.config.category, self.config.interval)

    async def _ws_messages(self) -> AsyncIterator[dict[str, Any]]:
        if websockets is None:
            raise RuntimeError("websockets package is not installed")
        topic = candle_topic(self.config.symbol, self.config.interval, self.config.category)
        async with websockets.connect(PUBLIC_WS, ping_interval=None, close_timeout=5) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    data = json.loads(raw)
                    # Subscription acknowledgements are useful status, not candles.
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
        backoff = 1.0
        while True:
            try:
                async for msg in self._ws_messages():
                    df = self._normalize_ws_message(msg)
                    if df.empty:
                        continue
                    self.cache = merge_candle_frames(self.cache, df, max_rows=self.config.max_rows)
                    self._persist(df)
                    self._save()
                    backoff = 1.0
                    for _, row in df.iterrows():
                        self.last_emit_ts = int(row["timestamp"])
                        yield {"type": "candle_update", "transport": "websocket", "candle": self._row_to_chart(row), "rows": int(len(self.cache))}
            except Exception as exc:
                self.last_status = f"ws_error: {exc}"
                # Degraded mode: do not spam Bitget. Fetch recent candles at low frequency.
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)
                try:
                    df = self.public.get_recent_candles(self.config.symbol, self.config.category, self.config.interval, limit=60)
                    before = set(self.cache["timestamp"].astype("int64")) if not self.cache.empty and "timestamp" in self.cache else set()
                    self.cache = merge_candle_frames(self.cache, df, max_rows=self.config.max_rows)
                    self._persist(df)
                    self._save()
                    latest = self.cache.tail(3)
                    yield {"type": "status", "transport": "rest_fallback", "message": str(exc), "rows": int(len(self.cache))}
                    for _, row in latest.iterrows():
                        # Send last few rows so an updated current candle is not missed.
                        yield {"type": "candle_update", "transport": "rest_fallback", "candle": self._row_to_chart(row), "rows": int(len(self.cache)), "is_new": int(row["timestamp"]) not in before}
                except Exception as rest_exc:
                    yield {"type": "status", "transport": "offline", "message": f"WS and REST failed: {rest_exc}", "rows": int(len(self.cache))}

    @staticmethod
    def _row_to_chart(row: pd.Series) -> dict[str, Any]:
        return {
            "time": int(int(row["timestamp"]) / 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0) or 0),
        }


class LiveSyntheticCandleStream:
    """Build Bitget's unsupported 2m live candle from its 1m Kline stream.

    This deliberately does not use the trade feed: trade messages can be
    dropped during reconnects and do not share Bitget's candle timestamp
    contract. The 1m Kline update is the same source used by the canonical DB,
    so the live 2m bar replaces the stored bar at the exact same timestamp.
    """

    def __init__(self, config: StreamConfig):
        if config.interval != "2m":
            raise ValueError("LiveSyntheticCandleStream currently supports 2m only")
        self.config = config
        self.public = BitgetPublicClient()
        self.base_cache = pd.DataFrame()
        self.cache = pd.DataFrame()
        self.last_status = "init"

    def _save(self) -> None:
        if self.config.write_csv and self.config.cache_path and not self.cache.empty:
            save_csv(self.cache, self.config.cache_path)

    def _persist(self, base: pd.DataFrame | None = None, derived: pd.DataFrame | None = None) -> None:
        if base is not None and not base.empty:
            upsert_candles(base, source="bitget_live")
        if derived is not None and not derived.empty:
            upsert_candles(derived, source="derived_1m_live")

    def _derived_tail(self, changed: pd.DataFrame | None = None) -> pd.DataFrame:
        if self.base_cache.empty:
            return pd.DataFrame()
        derived = aggregate_from_1m(self.base_cache, self.config.interval)
        if changed is None or changed.empty:
            return derived
        buckets = {
            bucket_start_ms(int(ts), self.config.interval)
            for ts in pd.to_numeric(changed["timestamp"], errors="coerce").dropna()
        }
        return derived[derived["timestamp"].isin(buckets)].reset_index(drop=True)

    def initial(self, cached: pd.DataFrame | None = None) -> pd.DataFrame:
        base = self.public.get_recent_candles(
            self.config.symbol, self.config.category, "1m",
            limit=min(max(50, int(self.config.initial_limit) * 2 + 4), 1000),
        )
        self.base_cache = merge_candle_frames(self.base_cache, base, max_rows=2000)
        derived = self._derived_tail()
        self.cache = merge_candle_frames(cached if cached is not None else pd.DataFrame(), derived, max_rows=self.config.max_rows)
        self._persist(base=base, derived=derived)
        self._save()
        self.last_status = "rest_bootstrap_ok"
        return self.cache.copy()

    def _normalize_ws_message(self, msg: dict[str, Any]) -> pd.DataFrame:
        rows = msg.get("data") or []
        if not rows:
            return pd.DataFrame()
        return normalize_bitget_candles(rows, self.config.symbol.upper(), self.config.category, "1m")

    async def _ws_messages(self) -> AsyncIterator[dict[str, Any]]:
        if websockets is None:
            raise RuntimeError("websockets package is not installed")
        topic = candle_topic(self.config.symbol, "1m", self.config.category)
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
        backoff = 1.0
        while True:
            try:
                async for msg in self._ws_messages():
                    base = self._normalize_ws_message(msg)
                    if base.empty:
                        continue
                    self.base_cache = merge_candle_frames(self.base_cache, base, max_rows=2000)
                    derived = self._derived_tail(base)
                    if derived.empty:
                        continue
                    self.cache = merge_candle_frames(self.cache, derived, max_rows=self.config.max_rows)
                    self._persist(base=base, derived=derived)
                    self._save()
                    backoff = 1.0
                    for _, row in derived.iterrows():
                        yield {
                            "type": "candle_update",
                            "transport": "websocket_1m_aggregate",
                            "candle": LiveCandleStream._row_to_chart(row),
                            "rows": int(len(self.cache)),
                        }
            except Exception as exc:
                self.last_status = f"ws_error: {exc}"
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)
                try:
                    base = self.public.get_recent_candles(self.config.symbol, self.config.category, "1m", limit=60)
                    self.base_cache = merge_candle_frames(self.base_cache, base, max_rows=2000)
                    derived = self._derived_tail(base)
                    self.cache = merge_candle_frames(self.cache, derived, max_rows=self.config.max_rows)
                    self._persist(base=base, derived=derived)
                    self._save()
                    yield {"type": "status", "transport": "rest_fallback", "message": str(exc), "rows": int(len(self.cache))}
                    for _, row in derived.tail(2).iterrows():
                        yield {"type": "candle_update", "transport": "rest_fallback", "candle": LiveCandleStream._row_to_chart(row), "rows": int(len(self.cache))}
                except Exception as rest_exc:
                    yield {"type": "status", "transport": "offline", "message": f"WS and REST failed: {rest_exc}", "rows": int(len(self.cache))}
