from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from src.bitget.public_client import BitgetPublicClient
from src.data_engine.canonical import CANONICAL_INTERVALS, load_or_fetch_canonical_candles
from src.data_engine.storage import DEFAULT_DB, load_candles, record_gap, save_csv, upsert_candles
from src.data_engine.validator import DataValidationError, find_gaps, validate_candles
from src.utils.time import INTERVAL_MS, now_ms, to_ms


@dataclass
class LoadReport:
    rows: int = 0
    from_cache: int = 0
    fetched: int = 0
    gaps_found: int = 0
    gaps_repaired: int = 0
    gaps_failed: int = 0


MAX_AUTO_CANONICAL_1M_ROWS = 90_000


def _range_missing(df: pd.DataFrame, start_ms: int, end_ms: int, interval: str) -> bool:
    if df is None or df.empty:
        return True
    step = INTERVAL_MS.get(interval, 0)
    if not step:
        return False
    d = df.sort_values('timestamp')
    return int(d['timestamp'].iloc[0]) > start_ms + step or int(d['timestamp'].iloc[-1]) < end_ms - step


def _missing_ranges(df: pd.DataFrame, start_ms: int, end_ms: int, interval: str) -> list[tuple[int, int]]:
    """Return only the intervals that are absent from cache.

    Ranges are inclusive candle timestamps. Callers should pass end + step to
    Bitget because its explicit range endpoint behaves like an exclusive end.
    """
    step = INTERVAL_MS.get(interval)
    if not step:
        return []
    start_ms = (int(start_ms) // step) * step
    end_ms = (int(end_ms) // step) * step
    if df is None or df.empty:
        return [(start_ms, end_ms)]
    d = df.copy()
    d['timestamp'] = pd.to_numeric(d['timestamp'], errors='coerce')
    d = d.dropna(subset=['timestamp']).drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    if d.empty:
        return [(start_ms, end_ms)]

    ranges: list[tuple[int, int]] = []
    first = int(d['timestamp'].iloc[0])
    last = int(d['timestamp'].iloc[-1])
    if first > start_ms:
        ranges.append((start_ms, min(end_ms, first - step)))
    for gap_start, gap_end, missing in find_gaps(d, interval):
        if missing > 0:
            a = max(start_ms, int(gap_start))
            b = min(end_ms, int(gap_end))
            if a <= b:
                ranges.append((a, b))
    if last < end_ms:
        ranges.append((max(start_ms, last + step), end_ms))
    return [(a, b) for a, b in ranges if a <= b]


def repair_gaps(symbol: str, category: str, interval: str, df: pd.DataFrame, max_repairs: int = 50, db_path: str = DEFAULT_DB) -> tuple[pd.DataFrame, LoadReport]:
    """Retry only missing ranges. No synthetic bars are inserted."""
    report = LoadReport(rows=len(df))
    gaps = find_gaps(df, interval)
    real_gaps = [g for g in gaps if g[2] > 0]
    report.gaps_found = len(real_gaps)
    if not real_gaps:
        return df, report
    client = BitgetPublicClient()
    frames = [df]
    for start_ts, end_ts, missing in real_gaps[:max_repairs]:
        try:
            patch = client.download_candles(symbol, category, interval, int(start_ts), int(end_ts + INTERVAL_MS[interval]))
            if patch.empty:
                report.gaps_failed += 1
                try:
                    record_gap(symbol, category, interval, start_ts, end_ts, 'failed', 'empty repair response', db_path)
                except Exception:
                    pass
                continue
            try:
                upsert_candles(patch, db_path)
            except Exception:
                pass
            frames.append(patch)
            report.gaps_repaired += 1
            try:
                record_gap(symbol, category, interval, start_ts, end_ts, 'repaired', f'{len(patch)} rows', db_path)
            except Exception:
                pass
        except Exception as exc:
            report.gaps_failed += 1
            try:
                record_gap(symbol, category, interval, start_ts, end_ts, 'failed', str(exc), db_path)
            except Exception:
                pass
    merged = pd.concat(frames, ignore_index=True) if frames else df
    if not merged.empty:
        merged = merged.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    report.rows = len(merged)
    return merged, report


def load_or_fetch_candles(
    symbol: str,
    category: str,
    interval: str,
    start: str | int | None = None,
    end: str | int | None = None,
    limit: int = 1000,
    source: str = 'cache-first',
    repair: bool = True,
    strict_backtest: bool = False,
    db_path: str = DEFAULT_DB,
) -> tuple[pd.DataFrame, LoadReport]:
    """Cache-first historical loader used by UI and backtests.

    Policy:
    - UI loads only a bounded recent window by default.
    - Already cached data is reused.
    - Missing data is fetched in Bitget-safe chunks with rate limiting.
    - Gaps are retried by range; unresolved gaps are not filled with fake prices.
    """
    symbol = symbol.upper()
    limit = max(1, int(limit or 1000))
    end_ms = min(to_ms(end), now_ms()) if end is not None else now_ms()
    step = INTERVAL_MS.get(interval, 60_000)
    if start is not None:
        requested_start_ms = to_ms(start)
    else:
        requested_start_ms = end_ms - step * limit

    # Important: UI/backtest calls normally ask for a broad date range but only
    # need the latest N candles. Do not fetch years of data just to tail(1000).
    # CLI users can pass a very large --limit when they intentionally want a
    # full backfill. This keeps REST requests bounded and protects the API key/IP.
    if limit is not None and interval in INTERVAL_MS:
        bounded_start_ms = end_ms - step * int(limit)
        start_ms = max(requested_start_ms, bounded_start_ms)
    else:
        start_ms = requested_start_ms

    if start_ms >= end_ms:
        raise ValueError('start must be earlier than end')

    if source in ('bitget', 'refresh') and interval in CANONICAL_INTERVALS:
        base_rows_needed = max(1, int((end_ms - start_ms) // INTERVAL_MS['1m']) + 1)
        fetch_canonical = base_rows_needed <= MAX_AUTO_CANONICAL_1M_ROWS
        try:
            df_can, can = load_or_fetch_canonical_candles(
                symbol,
                category,
                interval,
                start_ms,
                end_ms,
                limit=limit,
                fetch=fetch_canonical,
                persist_derived=True,
                db_path=db_path,
            )
            if fetch_canonical or (not df_can.empty and can.gaps_found == 0):
                report = LoadReport(
                    rows=len(df_can),
                    from_cache=max(0, int(can.base_rows) - int(can.fetched_1m)),
                    fetched=int(can.fetched_1m),
                    gaps_found=int(can.gaps_found),
                    gaps_repaired=int(can.gaps_repaired),
                    gaps_failed=0,
                )
                issues = validate_candles(df_can, interval, strict=False) if not df_can.empty else ['empty candle data']
                if strict_backtest and issues:
                    raise DataValidationError('; '.join(issues))
                return df_can.reset_index(drop=True), report
        except Exception:
            if interval == '2m':
                raise
            # Large ranges without a canonical 1m cache fall back to the native
            # Bitget interval below so the UI stays responsive. Full stability is
            # obtained by running backfill-year/backfill-full first.
            pass

    try:
        cached = load_candles(symbol, category, interval, start_ms, end_ms, limit=None, db_path=db_path)
    except Exception:
        cached = pd.DataFrame()
    report = LoadReport(rows=len(cached), from_cache=len(cached))
    missing = _missing_ranges(cached, start_ms, end_ms, interval)
    need_fetch = source in ('refresh',) or bool(missing)
    if need_fetch:
        client = BitgetPublicClient()
        patches = []
        fetch_ranges = [(start_ms, end_ms)] if source == 'refresh' else missing
        for a, b in fetch_ranges:
            patch = client.download_candles(symbol, category, interval, int(a), int(b) + step)
            if patch.empty:
                continue
            patches.append(patch)
            report.fetched += len(patch)
            try:
                upsert_candles(patch, db_path)
            except Exception:
                pass
        frames = [x for x in [cached, *patches] if x is not None and not x.empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not df.empty:
            df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
            df = df[(df['timestamp'] >= start_ms) & (df['timestamp'] <= end_ms)].copy()
    else:
        df = cached

    if repair and not df.empty and interval in INTERVAL_MS:
        df, rep = repair_gaps(symbol, category, interval, df, db_path=db_path)
        report.gaps_found = rep.gaps_found
        report.gaps_repaired = rep.gaps_repaired
        report.gaps_failed = rep.gaps_failed

    if not df.empty:
        df = df.drop_duplicates('timestamp').sort_values('timestamp').tail(limit).reset_index(drop=True)

    issues = validate_candles(df, interval, strict=False) if not df.empty else ['empty candle data']
    if strict_backtest and issues:
        raise DataValidationError('; '.join(issues))
    report.rows = len(df)
    return df, report


def download_to_csv(symbol, category, interval, start, end, out, limit: int | None = None):
    df, report = load_or_fetch_candles(
        symbol, category, interval, start, end,
        limit=limit or 1_000_000,
        source='bitget',
        repair=True,
        strict_backtest=False,
    )
    if df.empty:
        raise RuntimeError(
            'Bitget returned 0 candles. Check symbol/category/interval/date range. '
            'Example: --symbol BTCUSDT --category USDT-FUTURES --interval 1H '
            '--start 2024-01-01 --end 2025-01-01'
        )
    issues=validate_candles(df, interval, strict=False)
    if issues:
        print('data warnings:', issues)
    save_csv(df, out)
    return df
