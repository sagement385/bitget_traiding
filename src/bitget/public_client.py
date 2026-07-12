import time
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import requests

from src.utils.time import INTERVAL_MS, to_ms, now_ms

BITGET_GRAN = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1H",
    "2H": "2H",
    "4H": "4H",
    "6H": "6H",
    "12H": "12H",
    "1D": "1D",
    "1W": "1W",
    "1M": "1M",
}

# Bitget futures does not expose 2m candles through REST, but its chart can be
# matched by building 2m bars from native 1m market candles.
AGGREGATED_INTERVAL_BASE = {
    "2m": "1m",
}

PRODUCT_TYPE = {
    "USDT-FUTURE": "usdt-futures",
    "USDT-FUTURES": "usdt-futures",
    "USDC-FUTURES": "usdc-futures",
    "COIN-FUTURES": "coin-futures",
    "SUSDT-FUTURES": "susdt-futures",
    "SUSDC-FUTURES": "susdc-futures",
    "SCOIN-FUTURES": "scoin-futures",
}


class RateLimiter:
    """Simple process-local token spacing limiter.

    Bitget's public market endpoints have IP-level rate limits. We deliberately
    run below the published limit so UI clicks, live polling, and gap repair do
    not burst at the same time.
    """
    def __init__(self, calls_per_sec: float = 6.0):
        self.min_interval = 1.0 / max(float(calls_per_sec), 0.1)
        self._lock = Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.min_interval


_PUBLIC_RATE_LIMITER = RateLimiter(16.0)

# Bitget v2 futures candle endpoints impose both a max returned row count and an
# interval-dependent date range limit. We keep chunks well below those limits.
# Official rules include: 1m/3m/5m <= 1 month, 15m <= 52 days,
# 30m <= 62 days, 1H <= 83 days, etc. Requests with explicit start/end must
# also stay below Bitget's 90-day range guard, so long intervals are capped here.
MAX_LOOKBACK_DAYS = {
    "1m": 29,
    "3m": 29,
    "5m": 29,
    "15m": 50,
    "30m": 60,
    "1H": 80,
    "2H": 88,
    "4H": 88,
    "6H": 88,
    "12H": 88,
    "1D": 88,
    "1W": 88,
    "1M": 88,
}


@dataclass
class CandleFetchReport:
    chunks: int = 0
    rows_raw: int = 0
    rows_final: int = 0
    errors_retried: int = 0


class BitgetPublicClient:
    def __init__(self, base_url: str = "https://api.bitget.com", timeout: int = 15, retries: int = 4, calls_per_sec: float = 16.0, max_workers: int = 16):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.rate_limiter = _PUBLIC_RATE_LIMITER
        self.max_workers = max(1, int(max_workers))
        self.last_report = CandleFetchReport()

    def _product_type(self, category: str) -> str:
        key = str(category).upper().strip()
        return PRODUCT_TYPE.get(key, key.lower())

    def _get_json(self, endpoint: str, params: dict):
        url = f"{self.base_url}{endpoint}"
        last_error = None
        for attempt in range(self.retries):
            try:
                self.rate_limiter.wait()
                r = requests.get(url, params=params, timeout=self.timeout)
                txt = r.text[:1200]
                if r.status_code in (408, 425, 429, 500, 502, 503, 504):
                    raise RuntimeError(f"temporary HTTP {r.status_code}; url={r.url}; response={txt}")
                try:
                    r.raise_for_status()
                except requests.HTTPError as e:
                    raise RuntimeError(f"HTTP {r.status_code}; url={r.url}; response={txt}") from e
                js = r.json()
                if js.get("code") not in (None, "00000"):
                    raise RuntimeError(f"Bitget API error; url={r.url}; response={js}")
                return js
            except Exception as e:
                last_error = e
                if attempt < self.retries - 1:
                    self.last_report.errors_retried += 1
                    time.sleep(min(8.0, 1.0 * (2 ** attempt)))
        raise last_error

    def _request_candles(
        self,
        endpoint: str,
        symbol: str,
        product_type: str,
        granularity: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ):
        max_limit = 1000 if endpoint.endswith("/market/candles") else 200
        params = {
            "symbol": symbol.upper(),
            "productType": product_type,
            "granularity": granularity,
            "limit": str(min(int(limit), max_limit)),
        }
        if start_ms is not None:
            params["startTime"] = str(int(start_ms))
        if end_ms is not None:
            params["endTime"] = str(int(end_ms))
        js = self._get_json(endpoint, params)
        return js.get("data") or []

    def _chunk_ms(self, interval: str) -> int:
        if interval not in INTERVAL_MS:
            raise ValueError(f"Unsupported interval: {interval}")
        by_count = INTERVAL_MS[interval] * 190  # safely below 200-return limit
        by_days = MAX_LOOKBACK_DAYS.get(interval, 80) * 24 * 60 * 60 * 1000
        return max(INTERVAL_MS[interval], min(by_count, by_days))

    def _fetch_chunk(self, symbol: str, product_type: str, gran: str, cur: int, nxt: int):
        endpoints = ["/api/v2/mix/market/history-candles", "/api/v2/mix/market/candles"]
        last = None
        saw_empty = False
        for ep in endpoints:
            try:
                data = self._request_candles(ep, symbol, product_type, gran, cur, nxt, limit=200)
                if data:
                    return data
                saw_empty = True
            except Exception as e:
                last = e
        if saw_empty and last is None:
            return []
        if last:
            raise last
        return []

    def get_recent_candles(self, symbol: str, category: str, interval: str, limit: int = 300) -> pd.DataFrame:
        """Fetch only recent candles for live chart polling.

        This method intentionally uses no start/end range so it avoids long-range
        historical restrictions and is cheap enough for repeated polling.
        """
        base_interval = AGGREGATED_INTERVAL_BASE.get(interval, interval)
        if base_interval not in BITGET_GRAN:
            supported = sorted(set(BITGET_GRAN) | set(AGGREGATED_INTERVAL_BASE))
            raise ValueError(f"Unsupported interval: {interval}. Supported: {supported}")
        product_type = self._product_type(category)
        gran = BITGET_GRAN[base_interval]
        base_limit = int(limit)
        if interval in AGGREGATED_INTERVAL_BASE:
            base_limit = min(1000, int(limit) * max(1, INTERVAL_MS[interval] // INTERVAL_MS[base_interval]) + 4)
        data = self._request_candles(
            "/api/v2/mix/market/candles",
            symbol,
            product_type,
            gran,
            None,
            None,
            limit=base_limit,
        )
        df = normalize_bitget_candles(data, symbol.upper(), category, base_interval)
        if interval in AGGREGATED_INTERVAL_BASE:
            df = aggregate_ohlcv(df, interval)
        return df.tail(limit).reset_index(drop=True)

    def download_candles(self, symbol: str, category: str, interval: str, start, end) -> pd.DataFrame:
        """Download any requested range by stitching valid Bitget chunks.

        Native Bitget intervals are fetched directly. Synthetic intervals such
        as 2m are built from their native base candles after download.
        """
        self.last_report = CandleFetchReport()
        base_interval = AGGREGATED_INTERVAL_BASE.get(interval, interval)
        if base_interval not in BITGET_GRAN:
            supported = sorted(set(BITGET_GRAN) | set(AGGREGATED_INTERVAL_BASE))
            raise ValueError(f"Unsupported interval: {interval}. Supported: {supported}")
        product_type = self._product_type(category)
        gran = BITGET_GRAN[base_interval]
        start_ms = to_ms(start)
        end_ms = min(to_ms(end), now_ms())
        if start_ms >= end_ms:
            raise ValueError("start must be earlier than end")
        cur = max(0, start_ms - INTERVAL_MS[base_interval])
        step = self._chunk_ms(base_interval)
        chunks: list[tuple[int, int]] = []
        seen: set[str] = set()
        while cur < end_ms:
            nxt = min(cur + step, end_ms)
            chunks.append((cur, nxt))
            cur = nxt

        rows: list[list] = []

        def collect(data):
            self.last_report.rows_raw += len(data)
            for row in data or []:
                if not row:
                    continue
                key = str(row[0])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)

        self.last_report.chunks = len(chunks)
        if len(chunks) <= 1 or self.max_workers <= 1:
            for a, b in chunks:
                collect(self._fetch_chunk(symbol, product_type, gran, a, b))
        else:
            workers = min(self.max_workers, len(chunks))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._fetch_chunk, symbol, product_type, gran, a, b) for a, b in chunks]
                for fut in as_completed(futures):
                    collect(fut.result())
        df = normalize_bitget_candles(rows, symbol.upper(), category, base_interval)
        if not df.empty:
            finish_ms = min(to_ms(end), now_ms())
            df = df[(df["timestamp"] >= start_ms) & (df["timestamp"] <= finish_ms)].copy()
        if interval in AGGREGATED_INTERVAL_BASE:
            df = aggregate_ohlcv(df, interval)
        self.last_report.rows_final = len(df)
        return df.reset_index(drop=True)


def aggregate_ohlcv(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    d = df.copy()
    d["dt"] = pd.to_datetime(d["timestamp"], unit="ms", utc=True)
    if interval == "2m":
        d["bucket"] = (pd.to_numeric(d["timestamp"], errors="coerce").astype("int64") // INTERVAL_MS["2m"]) * INTERVAL_MS["2m"]
        grouped = d.sort_values("timestamp").groupby("bucket", sort=True)
        agg = grouped.agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "turnover": "sum",
                "symbol": "last",
                "category": "last",
            }
        ).dropna(subset=["open", "high", "low", "close"])
        agg["timestamp"] = agg.index.astype("int64")
        agg["interval"] = interval
        cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval"]
        return agg[cols].reset_index(drop=True)

    rule = {"1W": "W-MON", "1M": "MS"}[interval]
    # Bitget's non-UTC day/week/month candles roll at UTC+8 midnight. When this
    # helper is used for fallback aggregation, shift into that exchange day,
    # resample, then shift labels back to the timestamp Bitget uses.
    d["dt"] = d["dt"] + pd.Timedelta(hours=8)
    d = d.set_index("dt").sort_index()
    agg = (
        d.resample(rule, label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "turnover": "sum",
                "symbol": "last",
                "category": "last",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
    )
    shifted_index = agg.index - pd.Timedelta(hours=8)
    agg["timestamp"] = (shifted_index.view("int64") // 1_000_000).astype("int64")
    agg["interval"] = interval
    cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval"]
    return agg[cols].reset_index(drop=True)


def normalize_bitget_candles(rows: Iterable, symbol: str, category: str, interval: str) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    if not rows:
        return pd.DataFrame(columns=cols + ["symbol", "category", "interval"])
    df = pd.DataFrame(rows).iloc[:, :7]
    df.columns = cols
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df["timestamp"] = df["timestamp"].astype("int64")
    df["symbol"] = symbol
    df["category"] = category
    df["interval"] = interval
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
