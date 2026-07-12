from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import BACKEND_ROOT, save_api_snapshots


KST = ZoneInfo("Asia/Seoul")
SNAPSHOT_DIR = BACKEND_ROOT / "logs" / "api_snapshots"


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return cleaned.strip("_")[:160] or "snapshot"


def save_snapshot(
    *,
    prefix: str,
    source_name: str,
    source_url: str,
    request_params: dict[str, Any],
    raw_response: Any,
    status: str = "success",
    reason: str | None = None,
) -> Path | None:
    if not save_api_snapshots():
        return None
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = now_kst()
    payload = {
        "fetched_at": fetched_at,
        "source_name": source_name,
        "source_url": source_url,
        "request_params": request_params,
        "status": status,
        "reason": reason,
        "raw_response": raw_response,
    }
    stamp = fetched_at.replace(":", "").replace("-", "")
    filename = f"{safe_filename(prefix)}_{stamp}.json"
    path = SNAPSHOT_DIR / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
