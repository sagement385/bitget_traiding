from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.data_engine.canonical import (
    CANONICAL_INTERVALS,
    aggregate_from_1m,
    bucket_start_ms,
    default_history_start,
    ensure_1m_range,
    load_or_fetch_canonical_candles,
    utc_year_bounds,
)
from src.data_engine.storage import save_csv, upsert_candles
from src.data_engine.storage import delete_candles, load_candles
from src.data_engine.validator import validate_candles
from src.utils.time import now_ms, to_ms

DERIVED_INTERVALS = ("2m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "1W", "1M")


def aggregate_from_1m_for_backfill(df, interval: str):
    return aggregate_from_1m(df, interval)


def _save_interval(df, symbol: str, interval: str, out_dir: Path, suffix: str = "") -> int:
    if df is None or df.empty:
        return 0
    validate_candles(df, interval, strict=True)
    tail = f"_{suffix}" if suffix else ""
    save_csv(df, str(out_dir / f"{symbol}_{interval}{tail}.csv"))
    upsert_candles(df)
    return int(len(df))


def backfill_year_minute_data(
    symbol: str,
    category: str,
    year: int,
    out_dir: str | Path = "data/raw",
    derive_intervals: tuple[str, ...] = DERIVED_INTERVALS,
) -> dict[str, int]:
    """Backfill one calendar year of 1m candles and rebuild higher intervals."""
    start, end = utc_year_bounds(year)
    return backfill_range(
        symbol=symbol,
        category=category,
        start=start,
        end=end,
        out_dir=out_dir,
        suffix=str(year),
        derive_intervals=derive_intervals,
    )


def backfill_range(
    symbol: str,
    category: str,
    start: str,
    end: str | None = None,
    out_dir: str | Path = "data/raw",
    suffix: str = "",
    derive_intervals: tuple[str, ...] = DERIVED_INTERVALS,
) -> dict[str, int]:
    """Fill canonical 1m data over a range and rebuild derived intervals."""
    symbol = symbol.upper()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_ms = to_ms(start)
    end_ms = min(to_ms(end), now_ms()) if end else now_ms()

    one_min, report = ensure_1m_range(symbol, category, start_ms, end_ms, fetch=True)
    rows = {"1m": _save_interval(one_min, symbol, "1m", out, suffix)}
    rows["_fetched_1m"] = int(report.fetched_1m)
    rows["_missing_ranges"] = int(report.missing_ranges)

    for interval in derive_intervals:
        if interval not in CANONICAL_INTERVALS or interval == "1m":
            continue
        df = aggregate_from_1m(one_min, interval)
        rows[interval] = _save_interval(df, symbol, interval, out, suffix)
    return rows


def rebuild_derived_intervals(
    symbol: str,
    category: str,
    intervals: tuple[str, ...] = DERIVED_INTERVALS,
    out_dir: str | Path = "data/raw",
    start: str | None = None,
    end: str | None = None,
    write_csv: bool = False,
    progress: bool = False,
) -> dict[str, int]:
    """Rebuild all derived candles from the canonical 1m DB in one pass.

    Monthly chunk backfills must not persist 1D/1W/1M directly because exchange
    day/week/month buckets can cross the chunk boundary. Rebuilding from the
    full 1m cache removes stale partial buckets and matches Bitget's bucket
    starts.
    """
    symbol = symbol.upper()
    start_ms = to_ms(start) if start else None
    end_ms = min(to_ms(end), now_ms()) if end else None
    rebuild_intervals = tuple(i for i in intervals if i != "1m" and i in CANONICAL_INTERVALS)
    load_start_ms = None
    if start_ms is not None and rebuild_intervals:
        load_start_ms = min(bucket_start_ms(start_ms, interval) for interval in rebuild_intervals)
    one_min = load_candles(symbol, category, "1m", load_start_ms, end_ms, limit=None)
    rows: dict[str, int] = {}
    if one_min.empty:
        return rows
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for interval in rebuild_intervals:
        delete_start_ms = bucket_start_ms(start_ms, interval) if start_ms is not None else None
        if progress:
            print(
                json.dumps(
                    {"status": "rebuild_interval_start", "interval": interval, "start_ms": delete_start_ms, "end_ms": end_ms},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        df = aggregate_from_1m(one_min, interval)
        if delete_start_ms is not None and not df.empty:
            df = df[df["timestamp"] >= delete_start_ms].copy()
        if end_ms is not None and not df.empty:
            df = df[df["timestamp"] <= end_ms].copy()
        if df.empty:
            rows[interval] = 0
            if progress:
                print(json.dumps({"status": "rebuild_interval_done", "interval": interval, "rows": 0}, ensure_ascii=False), flush=True)
            continue
        delete_candles(symbol, category, interval, delete_start_ms, end_ms)
        validate_candles(df, interval, strict=True)
        upsert_candles(df, source="derived_1m")
        if write_csv:
            save_csv(df, str(out / f"{symbol}_{interval}_full.csv"))
        rows[interval] = int(len(df))
        if progress:
            print(json.dumps({"status": "rebuild_interval_done", "interval": interval, "rows": int(len(df))}, ensure_ascii=False), flush=True)
    return rows


def backfill_full_history(
    symbol: str,
    category: str,
    start: str | None = None,
    end: str | None = None,
    out_dir: str | Path = "data/raw",
    derive_intervals: tuple[str, ...] = DERIVED_INTERVALS,
    progress: bool = False,
) -> dict[str, int]:
    """Backfill from listing-era start to now, using 1m as the source of truth.

    The full BTCUSDT 1m history is large, so this works month-by-month. Each
    chunk upserts into SQLite, which means reruns only request missing ranges.
    Derived intervals are rebuilt from the full 1m cache after all chunks finish
    so day/week/month buckets are never saved from partial monthly data.
    """
    start = start or default_history_start(symbol)
    start_ms = to_ms(start)
    end_ms = min(to_ms(end), now_ms()) if end else now_ms()
    totals: dict[str, int] = {"_chunks": 0}

    def iso(ms: int) -> str:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")

    cur = start_ms
    while cur < end_ms:
        cur_dt = datetime.fromtimestamp(int(cur) / 1000, timezone.utc)
        if cur_dt.month == 12:
            next_month = datetime(cur_dt.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(cur_dt.year, cur_dt.month + 1, 1, tzinfo=timezone.utc)
        nxt = min(end_ms, int(next_month.timestamp() * 1000))
        if nxt <= cur:
            break
        if progress:
            print(json.dumps({"chunk": f"{cur_dt.year}-{cur_dt.month:02d}", "start": iso(cur), "end": iso(nxt), "status": "start"}, ensure_ascii=False), flush=True)
        rows = backfill_range(
            symbol=symbol,
            category=category,
            start=iso(cur),
            end=iso(nxt),
            out_dir=out_dir,
            suffix=f"full_{cur_dt.year}_{cur_dt.month:02d}",
            derive_intervals=(),
        )
        totals["_chunks"] += 1
        for k, v in rows.items():
            totals[k] = totals.get(k, 0) + int(v)
        if progress:
            print(json.dumps({"chunk": f"{cur_dt.year}-{cur_dt.month:02d}", "start": iso(cur), "end": iso(nxt), "rows": rows}, ensure_ascii=False), flush=True)
        cur = nxt
    if derive_intervals:
        if progress:
            print(json.dumps({"status": "rebuild_derived_start", "intervals": list(derive_intervals)}, ensure_ascii=False), flush=True)
        rebuilt = rebuild_derived_intervals(symbol, category, derive_intervals, out_dir=out_dir, start=None, end=end, write_csv=False, progress=progress)
        for k, v in rebuilt.items():
            totals[k] = int(v)
        if progress:
            print(json.dumps({"status": "rebuild_derived_done", "rows": rebuilt}, ensure_ascii=False), flush=True)
    return totals
