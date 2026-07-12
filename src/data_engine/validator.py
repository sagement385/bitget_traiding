from __future__ import annotations

import pandas as pd
from src.utils.time import INTERVAL_MS

class DataValidationError(ValueError):
    pass


BITGET_DAILY_OFFSET_MS = 8 * 60 * 60 * 1000


def normalize_for_validation(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    if 'timestamp' in d:
        d['timestamp'] = pd.to_numeric(d['timestamp'], errors='coerce')
        d = d.dropna(subset=['timestamp'])
        d['timestamp'] = d['timestamp'].astype('int64')
        d = d.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        if c in d:
            d[c] = pd.to_numeric(d[c], errors='coerce')
    return d


def find_gaps(df: pd.DataFrame, interval: str) -> list[tuple[int, int, int]]:
    """Return gaps as (missing_start_ts, missing_end_ts, missing_bar_count).

    This function does not synthesize prices. It only reports where source data is
    discontinuous so the loader can retry those ranges or the backtester can stop.
    """
    if interval == '1M':
        return find_month_gaps(df)

    step = INTERVAL_MS.get(interval)
    if not step or df is None or len(df) < 2 or 'timestamp' not in df:
        return []
    d = normalize_for_validation(df)
    if len(d) < 2:
        return []
    ts = d['timestamp'].tolist()
    gaps: list[tuple[int, int, int]] = []
    for prev, cur in zip(ts, ts[1:]):
        delta = int(cur) - int(prev)
        if delta > step:
            missing = max(0, int(delta // step) - 1)
            if missing:
                gaps.append((int(prev + step), int(cur - step), missing))
        elif delta < step:
            gaps.append((int(prev), int(cur), -1))
    return gaps


def _month_ordinal(ts_ms: int) -> int:
    local = pd.Timestamp(int(ts_ms) + BITGET_DAILY_OFFSET_MS, unit='ms', tz='UTC')
    return int(local.year) * 12 + int(local.month)


def _next_month_start_ms(ts_ms: int) -> int:
    local = pd.Timestamp(int(ts_ms) + BITGET_DAILY_OFFSET_MS, unit='ms', tz='UTC')
    year = int(local.year)
    month = int(local.month) + 1
    if month == 13:
        year += 1
        month = 1
    nxt = pd.Timestamp(year=year, month=month, day=1, tz='UTC')
    return int(nxt.timestamp() * 1000) - BITGET_DAILY_OFFSET_MS


def find_month_gaps(df: pd.DataFrame) -> list[tuple[int, int, int]]:
    """Return non-contiguous monthly candle gaps using Bitget's UTC+8 month roll."""
    if df is None or len(df) < 2 or 'timestamp' not in df:
        return []
    d = normalize_for_validation(df)
    if len(d) < 2:
        return []
    ts = d['timestamp'].tolist()
    gaps: list[tuple[int, int, int]] = []
    for prev, cur in zip(ts, ts[1:]):
        prev_i = _month_ordinal(int(prev))
        cur_i = _month_ordinal(int(cur))
        expected = _next_month_start_ms(int(prev))
        if int(cur) == expected:
            continue
        if cur_i > prev_i + 1:
            gaps.append((expected, int(cur), cur_i - prev_i - 1))
        else:
            gaps.append((int(prev), int(cur), -1))
    return gaps


def validate_candles(df: pd.DataFrame, interval: str, strict=True, max_gap_count: int = 0) -> list[str]:
    errors=[]
    need=['timestamp','open','high','low','close','volume']
    if df is None or df.empty:
        errors.append('empty candle data')
        if strict: raise DataValidationError('; '.join(errors))
        return errors
    d = normalize_for_validation(df)
    for c in need:
        if c not in d.columns: errors.append(f'missing column: {c}')
    if errors:
        if strict: raise DataValidationError('; '.join(errors))
        return errors
    if df['timestamp'].duplicated().any(): errors.append('duplicated timestamp')
    if d[need].isna().any().any(): errors.append('null in OHLCV')
    if (d['high'] < d['low']).any(): errors.append('high < low')
    if (d['volume'] < 0).any(): errors.append('negative volume')
    if not d['timestamp'].is_monotonic_increasing: errors.append('timestamp not sorted')
    gaps = find_gaps(d, interval)
    actual_gaps = [g for g in gaps if g[2] > 0]
    if actual_gaps:
        total_missing = sum(g[2] for g in actual_gaps)
        errors.append(f'missing/nonuniform candles: {len(actual_gaps)} gaps, {total_missing} missing bars')
    if errors and strict:
        if actual_gaps and len(actual_gaps) <= max_gap_count:
            return errors
        raise DataValidationError('; '.join(errors))
    return errors
