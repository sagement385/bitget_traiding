from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bitget.public_client import BitgetPublicClient
from src.data_engine.canonical import bucket_start_ms, load_or_fetch_canonical_candles
from src.utils.time import INTERVAL_MS, now_ms


DEFAULT_WINDOWS_DAYS = {
    "1m": 2,
    "2m": 2,
    "3m": 5,
    "5m": 7,
    "15m": 14,
    "30m": 30,
    "1H": 60,
    "2H": 120,
    "4H": 240,
    "6H": 360,
    "12H": 720,
    "1D": 720,
    "1W": 1800,
    "1M": 2500,
}


def _completed_only(df: pd.DataFrame, interval: str, reference_ms: int | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy().sort_values("timestamp").reset_index(drop=True)
    reference_ms = int(reference_ms or now_ms())
    if interval in INTERVAL_MS and interval != "1M":
        cutoff = reference_ms - INTERVAL_MS[interval]
        return d[d["timestamp"] <= cutoff].reset_index(drop=True)
    # Month buckets have variable length. Drop only the exchange month that is
    # in progress at the comparison end, instead of blindly removing the last
    # row from both local and remote responses.
    cutoff = bucket_start_ms(reference_ms, interval)
    return d[d["timestamp"] < cutoff].reset_index(drop=True)


def compare_interval(symbol: str, category: str, interval: str, days: int, end: datetime | None = None) -> dict:
    end = (end or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    start = end - timedelta(days=int(days))
    start_ms = int(start.timestamp() * 1000)
    start_ms = bucket_start_ms(start_ms, interval)
    end_ms = int(end.timestamp() * 1000)
    local, report = load_or_fetch_canonical_candles(
        symbol,
        category,
        interval,
        start_ms,
        end_ms,
        limit=None,
        fetch=False,
        persist_derived=False,
    )
    remote = BitgetPublicClient().download_candles(symbol, category, interval, start_ms, end_ms)
    local = _completed_only(local, interval, end_ms)
    remote = _completed_only(remote, interval, end_ms)
    keys = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    merged = local[keys].merge(remote[keys], on="timestamp", suffixes=("_local", "_bitget"), how="outer", indicator=True)
    both = merged[merged["_merge"] == "both"].copy()
    diffs = []
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        if both.empty:
            continue
        a = pd.to_numeric(both[f"{col}_local"], errors="coerce")
        b = pd.to_numeric(both[f"{col}_bitget"], errors="coerce")
        tolerance = 1e-8 if col in {"open", "high", "low", "close"} else 1e-4
        bad = (a - b).abs() > tolerance
        if bad.any():
            diffs.append({"field": col, "count": int(bad.sum()), "max_abs": float((a - b).abs().max())})
    only_local = merged[merged["_merge"] == "left_only"]["timestamp"].head(5).astype("int64").tolist() if not merged.empty else []
    only_bitget = merged[merged["_merge"] == "right_only"]["timestamp"].head(5).astype("int64").tolist() if not merged.empty else []
    ok = not diffs and not only_local and not only_bitget and len(remote) > 0
    return {
        "interval": interval,
        "ok": ok,
        "days": int(days),
        "local_rows": int(len(local)),
        "bitget_rows": int(len(remote)),
        "base_rows": int(report.base_rows),
        "base_gaps": int(report.gaps_found),
        "only_local_sample": only_local,
        "only_bitget_sample": only_bitget,
        "diffs": diffs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--category", default="USDT-FUTURES")
    parser.add_argument("--intervals", default="1m,2m,3m,5m,15m,30m,1H,1D,1W,1M")
    parser.add_argument("--days", type=int, default=0, help="override comparison window for every interval")
    parser.add_argument("--end", default="", help="UTC ISO end time for comparison, e.g. 2026-06-18T08:50:00Z")
    args = parser.parse_args()

    end = None
    if args.end:
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).astimezone(timezone.utc)
    results = []
    for interval in [x.strip() for x in args.intervals.split(",") if x.strip()]:
        days = args.days or DEFAULT_WINDOWS_DAYS.get(interval, 30)
        results.append(compare_interval(args.symbol.upper(), args.category, interval, days, end=end))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(x["ok"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
