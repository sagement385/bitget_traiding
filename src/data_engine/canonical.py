from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from src.bitget.public_client import BitgetPublicClient
from src.data_engine.storage import DEFAULT_DB, load_candles, upsert_candles
from src.data_engine.validator import find_gaps, validate_candles
from src.markets import CRYPTO, is_regular_session_timestamp, market_local_datetime, market_spec, normalize_market_type
from src.utils.time import INTERVAL_MS, now_ms, to_ms

BASE_INTERVAL = "1m"
TAIL_REFRESH_MS = 48 * 60 * 60 * 1000
STOCK_TAIL_REFRESH_MS = 30 * 60 * 1000
STOCK_FETCH_CHUNK_MS = 7 * 24 * 60 * 60 * 1000
CANONICAL_INTERVALS = {
    "1m",
    "2m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1H",
    "2H",
    "4H",
    "6H",
    "12H",
    "1D",
    "1W",
    "1M",
}


@dataclass
class CanonicalReport:
    rows: int = 0
    base_rows: int = 0
    fetched_1m: int = 0
    missing_ranges: int = 0
    gaps_found: int = 0
    gaps_repaired: int = 0
    from_canonical: bool = True


def _floor_fixed(ts_ms: int, step_ms: int) -> int:
    return (int(ts_ms) // int(step_ms)) * int(step_ms)


def market_to_ms(value: str | int, market_type: str = CRYPTO, *, end_of_day: bool = False) -> int:
    """Interpret date-only UI values in the selected market's local timezone."""
    if isinstance(value, str):
        text = value.strip()
        if text and "T" not in text:
            local = datetime.fromisoformat(text).replace(tzinfo=market_spec(market_type).timezone)
            if end_of_day:
                local = local + timedelta(days=1) - timedelta(milliseconds=1)
            return int(local.astimezone(timezone.utc).timestamp() * 1000)
    return to_ms(value)


def _local_midnight_ms(ts_ms: int, market_type: str) -> int:
    local = market_local_datetime(ts_ms, market_type)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.astimezone(timezone.utc).timestamp() * 1000)


def _floor_local_calendar(ts_ms: int, interval: str, market_type: str) -> int:
    local = market_local_datetime(ts_ms, market_type)
    if interval == "1D":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "1W":
        start = (local - pd.Timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "1M":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unsupported calendar interval: {interval}")
    return int(start.astimezone(timezone.utc).timestamp() * 1000)


def _floor_local_multi_hour(ts_ms: int, interval: str, market_type: str) -> int:
    local = market_local_datetime(ts_ms, market_type)
    step_hours = INTERVAL_MS[interval] // INTERVAL_MS["1H"]
    start = local.replace(hour=(local.hour // step_hours) * step_hours, minute=0, second=0, microsecond=0)
    return int(start.astimezone(timezone.utc).timestamp() * 1000)


def bucket_start_ms(ts_ms: int, interval: str, market_type: str = CRYPTO) -> int:
    market_type = normalize_market_type(market_type)
    if interval in {"6H", "12H"}:
        return _floor_local_multi_hour(ts_ms, interval, market_type)
    if interval in {"1D", "1W", "1M"}:
        return _floor_local_calendar(ts_ms, interval, market_type)
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported canonical interval: {interval}")
    return _floor_fixed(ts_ms, INTERVAL_MS[interval])


def _bucket_series_ms(timestamps: pd.Series, interval: str, market_type: str = CRYPTO) -> pd.Series:
    ts = pd.to_numeric(timestamps, errors="coerce").astype("int64")
    if interval in {"6H", "12H", "1D", "1W", "1M"}:
        # Calendar/DST rules make vectorized epoch arithmetic unsafe for US
        # equities. The number of source one-minute rows is bounded by the
        # chart window, so explicit market-aware bucketing is preferable.
        return ts.map(lambda value: bucket_start_ms(int(value), interval, market_type)).astype("int64")
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported canonical interval: {interval}")
    step = INTERVAL_MS[interval]
    return ((ts // step) * step).astype("int64")


def _expanded_base_range(start_ms: int, end_ms: int, interval: str, market_type: str = CRYPTO) -> tuple[int, int]:
    start = bucket_start_ms(start_ms, interval, market_type)
    end = bucket_start_ms(end_ms, interval, market_type)
    if interval in INTERVAL_MS and interval not in ("1M",):
        end = min(now_ms(), end + INTERVAL_MS[interval] - INTERVAL_MS[BASE_INTERVAL])
    else:
        end = min(now_ms(), end_ms)
    return max(0, start), max(start, end)


def _missing_1m_ranges(cached: pd.DataFrame, start_ms: int, end_ms: int, market_type: str = CRYPTO) -> list[tuple[int, int]]:
    start = bucket_start_ms(start_ms, BASE_INTERVAL, market_type)
    end = bucket_start_ms(end_ms, BASE_INTERVAL, market_type)
    if cached is None or cached.empty:
        if market_type == CRYPTO:
            return [(start, end)]
        return _stock_session_windows(start, end, market_type)
    d = cached.copy()
    d["timestamp"] = pd.to_numeric(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    if d.empty:
        if market_type == CRYPTO:
            return [(start, end)]
        return _stock_session_windows(start, end, market_type)
    if market_type != CRYPTO:
        ranges: list[tuple[int, int]] = []
        step = INTERVAL_MS[BASE_INTERVAL]
        for session_start, session_end in _stock_session_windows(start, end, market_type):
            frame = d[(d["timestamp"] >= session_start) & (d["timestamp"] <= session_end)]
            if frame.empty:
                ranges.append((session_start, session_end))
                continue
            first = int(frame["timestamp"].iloc[0])
            last = int(frame["timestamp"].iloc[-1])
            if first > session_start:
                ranges.append((session_start, first - step))
            for gap_start, gap_end, missing in find_gaps(frame, BASE_INTERVAL):
                if missing > 0:
                    ranges.append((max(session_start, int(gap_start)), min(session_end, int(gap_end))))
            if last < session_end:
                ranges.append((last + step, session_end))
        return [(a, b) for a, b in ranges if a <= b]
    ranges: list[tuple[int, int]] = []
    first = int(d["timestamp"].iloc[0])
    last = int(d["timestamp"].iloc[-1])
    step = INTERVAL_MS[BASE_INTERVAL]
    if first > start:
        ranges.append((start, first - step))
    if market_type == CRYPTO:
        gap_frames = [d]
    else:
        # Do not treat closed overnight/weekend intervals as missing stock
        # candles. Holiday-aware calendars can be supplied by a provider later;
        # this baseline only checks gaps within an observed regular session day.
        d["_session_day"] = d["timestamp"].map(lambda value: market_local_datetime(int(value), market_type).date())
        gap_frames = [part for _, part in d.groupby("_session_day", sort=False)]
    for frame in gap_frames:
        for gap_start, gap_end, missing in find_gaps(frame, BASE_INTERVAL):
            if missing > 0:
                ranges.append((max(start, int(gap_start)), min(end, int(gap_end))))
    if last < end:
        ranges.append((last + step, end))
    return [(a, b) for a, b in ranges if a <= b]


def _stock_session_windows(start_ms: int, end_ms: int, market_type: str) -> list[tuple[int, int]]:
    """Return expected regular-session minute windows, excluding nights/weekends."""
    spec = market_spec(market_type)
    if spec.session_open is None or spec.session_close is None:
        return [(int(start_ms), int(end_ms))]
    start_local = market_local_datetime(start_ms, market_type)
    end_local = market_local_datetime(end_ms, market_type)
    day = start_local.date()
    final_day = end_local.date()
    windows: list[tuple[int, int]] = []
    while day <= final_day:
        if day.weekday() < 5:
            session_start = datetime.combine(day, spec.session_open, tzinfo=spec.timezone)
            session_close = datetime.combine(day, spec.session_close, tzinfo=spec.timezone) - timedelta(minutes=1)
            a = max(int(start_ms), int(session_start.astimezone(timezone.utc).timestamp() * 1000))
            b = min(int(end_ms), int(session_close.astimezone(timezone.utc).timestamp() * 1000))
            if a <= b:
                windows.append((a, b))
        day += timedelta(days=1)
    return windows


def find_market_gaps(df: pd.DataFrame, interval: str, market_type: str = CRYPTO) -> list[tuple[int, int, int]]:
    """Report only discontinuities expected during the selected market session."""
    market_type = normalize_market_type(market_type)
    if market_type == CRYPTO:
        return find_gaps(df, interval)
    if df is None or df.empty or interval in {"1D", "1W", "1M"}:
        # Weekend and exchange-holiday gaps are not errors for daily-or-larger
        # stock candles. The provider calendar will refine holiday detection.
        return []
    d = df.copy()
    d["timestamp"] = pd.to_numeric(d.get("timestamp"), errors="coerce")
    d = d.dropna(subset=["timestamp"]).sort_values("timestamp")
    if len(d) < 2:
        return []
    d["_session_day"] = d["timestamp"].map(lambda value: market_local_datetime(int(value), market_type).date())
    gaps: list[tuple[int, int, int]] = []
    for _, frame in d.groupby("_session_day", sort=False):
        gaps.extend(find_gaps(frame, interval))
    return gaps


def _merge_1m_ranges(ranges: list[tuple[int, int]], market_type: str = CRYPTO) -> list[tuple[int, int]]:
    if not ranges:
        return []
    step = INTERVAL_MS[BASE_INTERVAL]
    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in ranges if a <= b):
        if market_type != CRYPTO and merged and start - merged[-1][0] <= STOCK_FETCH_CHUNK_MS:
            # One provider request per session is wasteful for a first stock
            # import. Bridge overnight/weekend closures, but cap a request to
            # a weekly span so the future Toss adapter can paginate safely.
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
            continue
        if not merged or start > merged[-1][1] + step:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        merged[-1] = (prev_start, max(prev_end, end))
    return merged


def ensure_1m_range(
    symbol: str,
    category: str,
    start_ms: int,
    end_ms: int,
    *,
    db_path: str = DEFAULT_DB,
    fetch: bool = True,
    force_refresh_start_ms: int | None = None,
    refresh_recent_tail: bool = True,
    market_type: str = CRYPTO,
    client: Any | None = None,
) -> tuple[pd.DataFrame, CanonicalReport]:
    symbol = symbol.upper()
    market_type = normalize_market_type(market_type, category)
    start_ms = bucket_start_ms(start_ms, BASE_INTERVAL, market_type)
    current_ms = now_ms()
    end_ms = min(bucket_start_ms(end_ms, BASE_INTERVAL, market_type), current_ms)
    cached = load_candles(symbol, category, BASE_INTERVAL, start_ms, end_ms, limit=None, db_path=db_path, market_type=market_type)
    report = CanonicalReport(base_rows=len(cached))
    missing = _missing_1m_ranges(cached, start_ms, end_ms, market_type)
    if fetch:
        if refresh_recent_tail:
            if market_type == CRYPTO and end_ms >= current_ms - TAIL_REFRESH_MS:
                refresh_start = max(start_ms, end_ms - TAIL_REFRESH_MS)
                missing.append((bucket_start_ms(refresh_start, BASE_INTERVAL, market_type), end_ms))
            elif market_type != CRYPTO and is_regular_session_timestamp(end_ms, market_type):
                refresh_start = max(start_ms, end_ms - STOCK_TAIL_REFRESH_MS)
                missing.append((bucket_start_ms(refresh_start, BASE_INTERVAL, market_type), end_ms))
        if force_refresh_start_ms is not None:
            refresh_start = max(start_ms, min(int(force_refresh_start_ms), end_ms))
            missing.append((bucket_start_ms(refresh_start, BASE_INTERVAL, market_type), end_ms))
        missing = _merge_1m_ranges(missing, market_type)
    report.missing_ranges = len(missing)
    if fetch and missing:
        frames = [cached] if cached is not None and not cached.empty else []
        client = client or (BitgetPublicClient() if market_type == CRYPTO else _toss_public_client())
        for a, b in missing:
            if market_type == CRYPTO:
                patch = client.download_candles(symbol, category, BASE_INTERVAL, int(a), int(b) + INTERVAL_MS[BASE_INTERVAL])
            else:
                patch = client.download_candles(
                    symbol, category, BASE_INTERVAL, int(a), int(b) + INTERVAL_MS[BASE_INTERVAL], market_type=market_type
                )
            if patch.empty:
                continue
            patch["market_type"] = market_type
            upsert_candles(patch, db_path, market_type=market_type)
            frames.append(patch)
            report.fetched_1m += int(len(patch))
            report.gaps_repaired += 1
        cached = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not cached.empty:
            cached = cached.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
            cached = cached[(cached["timestamp"] >= start_ms) & (cached["timestamp"] <= end_ms)].copy()
    report.base_rows = len(cached)
    report.gaps_found = len([g for g in find_market_gaps(cached, BASE_INTERVAL, market_type) if g[2] > 0]) if not cached.empty else 0
    return cached.reset_index(drop=True), report


def _toss_public_client():
    # Import lazily so crypto-only users do not need Toss credentials or a
    # configured stock endpoint during normal Bitget startup.
    from src.toss.public_client import TossPublicClient

    return TossPublicClient()


def aggregate_from_1m(df: pd.DataFrame, interval: str, market_type: str = CRYPTO) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval", "market_type"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    market_type = normalize_market_type(market_type, df.get("category", pd.Series([None])).iloc[0] if len(df) else None)
    source = df.copy()
    if market_type != CRYPTO and "timestamp" in source:
        source = source[source["timestamp"].map(lambda value: is_regular_session_timestamp(int(value), market_type))].copy()
    if source.empty:
        return pd.DataFrame(columns=cols)
    if interval == BASE_INTERVAL:
        out = source
        out["interval"] = BASE_INTERVAL
        out["market_type"] = market_type
        return out[cols].reset_index(drop=True)
    if interval not in CANONICAL_INTERVALS:
        raise ValueError(f"Unsupported canonical interval: {interval}")
    d = source.sort_values("timestamp").reset_index(drop=True)
    for c in ["timestamp", "open", "high", "low", "close", "volume", "turnover"]:
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["timestamp", "open", "high", "low", "close"])
    d["timestamp"] = d["timestamp"].astype("int64")
    d["_bar_high"] = d[["open", "high", "low", "close"]].max(axis=1)
    d["_bar_low"] = d[["open", "high", "low", "close"]].min(axis=1)
    d["bucket"] = _bucket_series_ms(d["timestamp"], interval, market_type)
    out = (
        d.groupby("bucket", sort=True)
        .agg(
            {
                "open": "first",
                "_bar_high": "max",
                "_bar_low": "min",
                "close": "last",
                "volume": "sum",
                "turnover": "sum",
                "symbol": "last",
                "category": "last",
            }
        )
        .reset_index()
        .rename(columns={"bucket": "timestamp", "_bar_high": "high", "_bar_low": "low"})
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["interval"] = interval
    out["market_type"] = market_type
    return out[cols].reset_index(drop=True)


def load_or_fetch_canonical_candles(
    symbol: str,
    category: str,
    interval: str,
    start: str | int,
    end: str | int | None,
    *,
    limit: int | None = None,
    fetch: bool = True,
    persist_derived: bool = True,
    force_refresh_start_ms: int | None = None,
    refresh_recent_tail: bool = True,
    db_path: str = DEFAULT_DB,
    market_type: str = CRYPTO,
    client: Any | None = None,
) -> tuple[pd.DataFrame, CanonicalReport]:
    if interval not in CANONICAL_INTERVALS:
        raise ValueError(f"Unsupported canonical interval: {interval}")
    market_type = normalize_market_type(market_type, category)
    end_ms = min(market_to_ms(end, market_type, end_of_day=True), now_ms()) if end is not None else now_ms()
    start_ms = market_to_ms(start, market_type)
    base_start, base_end = _expanded_base_range(start_ms, end_ms, interval, market_type)
    one_min, report = ensure_1m_range(
        symbol, category, base_start, base_end, db_path=db_path, fetch=fetch,
        force_refresh_start_ms=force_refresh_start_ms, refresh_recent_tail=refresh_recent_tail,
        market_type=market_type, client=client,
    )
    df = aggregate_from_1m(one_min, interval, market_type)
    if not df.empty:
        filter_start = bucket_start_ms(start_ms, interval, market_type)
        df = df[(df["timestamp"] >= filter_start) & (df["timestamp"] <= end_ms)].copy()
        df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if limit is not None:
            df = df.tail(int(limit)).reset_index(drop=True)
        if persist_derived and interval != BASE_INTERVAL:
            upsert_candles(df, db_path, market_type=market_type)
    report.rows = int(len(df))
    report.gaps_found = len([g for g in find_market_gaps(df, interval, market_type) if g[2] > 0]) if not df.empty else 0
    issues = validate_candles(df, interval, strict=False) if not df.empty else ["empty candle data"]
    if issues:
        report.from_canonical = True
    return df.reset_index(drop=True), report


def default_history_start(symbol: str = "BTCUSDT") -> str:
    # Bitget BTCUSDT futures public candles start here on the public history
    # endpoint. Starting earlier just burns requests on empty pre-listing time.
    # The caller can override this if a product has a different listing date.
    return "2019-07-10T11:49:00Z" if symbol.upper() == "BTCUSDT" else "2020-01-01T00:00:00Z"


def utc_year_bounds(year: int) -> tuple[str, str]:
    start = datetime(int(year), 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end = datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return start, end
