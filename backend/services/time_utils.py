from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def to_ymdhm(value: str) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid datetime: {value}")
    return parsed.strftime("%Y%m%d%H%M")


def ymdhm_to_iso(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) != 12 or not text.isdigit():
        return None
    parsed = datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=KST)
    return parsed.isoformat(timespec="minutes")


def within_range(iso_value: str | None, start_time: str | None, end_time: str | None) -> bool:
    observed = parse_datetime(iso_value)
    if observed is None:
        return False
    start = parse_datetime(start_time)
    end = parse_datetime(end_time)
    if start and observed < start:
        return False
    if end and observed > end:
        return False
    return True
