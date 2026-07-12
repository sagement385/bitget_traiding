from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data_engine.backfill import backfill_range, rebuild_derived_intervals
from src.data_engine.storage import available_range
from src.utils.time import INTERVAL_MS, now_ms


def iso(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill the latest missing 1m tail and rebuild derived candle cache.")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--category", default="USDT-FUTURES")
    parser.add_argument("--out-dir", default="data/raw")
    parser.add_argument("--rebuild-start", default="", help="override the derived-cache rebuild start time")
    parser.add_argument("--refresh-hours", type=float, default=48.0, help="recent 1m window to refetch and overwrite")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    _, max_ts, count = available_range(symbol, args.category, "1m")
    if max_ts is None:
        raise SystemExit("No canonical 1m data exists. Run backfill-full first.")

    tail_start_ms = int(max_ts) + INTERVAL_MS["1m"]
    refresh_ms = max(INTERVAL_MS["1m"], int(float(args.refresh_hours) * 60 * 60 * 1000))
    start_ms = min(tail_start_ms, now_ms() - refresh_ms)
    start_iso = iso(start_ms)
    print(json.dumps({"status": "tail_start", "start": start_iso, "existing_1m": int(count)}, ensure_ascii=False), flush=True)
    rows = backfill_range(
        symbol,
        args.category,
        start=start_iso,
        end=None,
        out_dir=args.out_dir,
        suffix="tail_refresh",
        derive_intervals=(),
    )
    print(json.dumps({"status": "tail_done", "rows": rows}, ensure_ascii=False), flush=True)
    rebuild_start = args.rebuild_start or start_iso
    rebuilt = rebuild_derived_intervals(symbol, args.category, progress=True, start=rebuild_start, end=None, write_csv=False)
    print(json.dumps({"status": "tail_rebuild_done", "rows": rebuilt}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
