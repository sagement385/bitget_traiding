from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

import pandas as pd

from src.bitget.public_client import BitgetPublicClient, normalize_bitget_candles
from src.bitget.websocket_client import PUBLIC_WS, candle_topic
from src.data_engine.canonical import aggregate_from_1m, bucket_start_ms
from src.data_engine.candle_store import CandleStore
from src.data_engine.storage import save_csv, upsert_candles
from src.data_engine.write_buffer import CandleWriteBuffer

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
    market_type: str = "CRYPTO"


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
        self.store = CandleStore(
            symbol=config.symbol,
            category=config.category,
            interval=config.interval,
            market_type=config.market_type,
            max_rows=config.max_rows,
        )
        self.writer = CandleWriteBuffer()
        self.last_emit_ts = 0
        self.last_status = "init"
        self.connection_id = ""
        self.sequence = 0
        self.reconnect_count = 0
        self.last_message_ms = 0

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
        initial = merge_candle_frames(seed, df, max_rows=self.config.max_rows)
        self.store.replace_all(initial.to_dict(orient="records"))
        self.cache = self.store.to_frame()
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
            self.connection_id = uuid.uuid4().hex
            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    data = json.loads(raw)
                    self.last_message_ms = int(time.time() * 1000)
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
        await self.writer.start()
        try:
            while True:
                try:
                    async for msg in self._ws_messages():
                        df = self._normalize_ws_message(msg)
                        if df.empty:
                            continue
                        records = df.to_dict(orient="records")
                        for record in records:
                            self.store.apply(record)
                        self.cache = self.store.to_frame() if self.config.write_csv else self.cache
                        await self.writer.put_frame(df, source="bitget_live")
                        if (write_error := self.writer.consume_error()) is not None:
                            yield self._event({"type": "status", "transport": "db_writer", "message": write_error, "rows": self.store.size})
                        self._save()
                        backoff = 1.0
                        for record in records:
                            self.last_emit_ts = int(record["timestamp"])
                            yield self._event({"type": "candle_update", "transport": "websocket", "candle": self._row_to_chart(record), "rows": self.store.size})
                except Exception as exc:
                    self.last_status = f"ws_error: {exc}"
                    self.reconnect_count += 1
                    delay = min(30.0, backoff)
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                    backoff = min(30.0, backoff * 1.7)
                    try:
                        df = self.public.get_recent_candles(self.config.symbol, self.config.category, self.config.interval, limit=60)
                        before = {c.open_time_ms for c in self.store.values()}
                        records = df.to_dict(orient="records")
                        for record in records:
                            self.store.apply(record)
                        self.cache = self.store.to_frame() if self.config.write_csv else self.cache
                        await self.writer.put_frame(df, source="bitget_rest_fallback")
                        self._save()
                        latest = records[-3:]
                        yield self._event({"type": "status", "transport": "rest_fallback", "message": str(exc), "rows": self.store.size, "reconnect_count": self.reconnect_count})
                        for record in latest:
                            yield self._event({"type": "candle_update", "transport": "rest_fallback", "candle": self._row_to_chart(record), "rows": self.store.size, "is_new": int(record["timestamp"]) not in before})
                    except Exception as rest_exc:
                        yield self._event({"type": "status", "transport": "offline", "message": f"WS and REST failed: {rest_exc}", "rows": self.store.size, "reconnect_count": self.reconnect_count})
        finally:
            await self.writer.close()

    def _event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        return {
            **payload,
            "connection_id": self.connection_id,
            "sequence": self.sequence,
            "server_time_ms": int(time.time() * 1000),
            "last_ws_message_ms": self.last_message_ms or int(time.time() * 1000),
            "db_queue_depth": self.writer.queue_depth,
        }

    @staticmethod
    def _row_to_chart(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
        get = row.get
        return {
            "time": int(int(get("timestamp")) / 1000),
            "open": float(get("open")),
            "high": float(get("high")),
            "low": float(get("low")),
            "close": float(get("close")),
            "volume": float(get("volume", 0) or 0),
            "turnover": float(get("turnover", 0) or 0),
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
        self.writer = CandleWriteBuffer()
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
        await self.writer.start()
        try:
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
                        await self.writer.put_frame(base, source="bitget_live")
                        await self.writer.put_frame(derived, source="derived_1m_live")
                        self._save()
                        backoff = 1.0
                        for row in derived.to_dict(orient="records"):
                            yield {"type": "candle_update", "transport": "websocket_1m_aggregate", "candle": LiveCandleStream._row_to_chart(row), "rows": int(len(self.cache))}
                except Exception as exc:
                    self.last_status = f"ws_error: {exc}"
                    await asyncio.sleep(min(30.0, backoff) + random.uniform(0, min(30.0, backoff) * 0.2))
                    backoff = min(30.0, backoff * 1.7)
                    try:
                        base = self.public.get_recent_candles(self.config.symbol, self.config.category, "1m", limit=60)
                        self.base_cache = merge_candle_frames(self.base_cache, base, max_rows=2000)
                        derived = self._derived_tail(base)
                        self.cache = merge_candle_frames(self.cache, derived, max_rows=self.config.max_rows)
                        await self.writer.put_frame(base, source="bitget_rest_fallback")
                        await self.writer.put_frame(derived, source="derived_rest_fallback")
                        self._save()
                        yield {"type": "status", "transport": "rest_fallback", "message": str(exc), "rows": int(len(self.cache))}
                        for row in derived.tail(2).to_dict(orient="records"):
                            yield {"type": "candle_update", "transport": "rest_fallback", "candle": LiveCandleStream._row_to_chart(row), "rows": int(len(self.cache))}
                    except Exception as rest_exc:
                        yield {"type": "status", "transport": "offline", "message": f"WS and REST failed: {rest_exc}", "rows": int(len(self.cache))}
        finally:
            await self.writer.close()
