from __future__ import annotations

from typing import Any

from .time_utils import parse_datetime


REQUIRED_SOURCE_FIELDS = ("source_name", "source_url", "observed_at", "unit")


def _is_number(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def validate_observation(
    observation: dict[str, Any],
    *,
    requested_start: str | None = None,
    requested_end: str | None = None,
) -> dict[str, Any]:
    for field in REQUIRED_SOURCE_FIELDS:
        if not observation.get(field):
            return {
                "valid": False,
                "reason": f"missing_{field}",
                "action": "exclude_from_risk_calculation",
            }

    if "value" not in observation or observation.get("value") is None:
        return {
            "valid": False,
            "reason": "missing_value",
            "action": "exclude_from_risk_calculation",
        }
    if not _is_number(observation.get("value")):
        return {
            "valid": False,
            "reason": "invalid_numeric_value",
            "action": "exclude_from_risk_calculation",
        }

    observed_at = parse_datetime(observation.get("observed_at"))
    if observed_at is None:
        return {
            "valid": False,
            "reason": "invalid_observed_at",
            "action": "exclude_from_risk_calculation",
        }

    start = parse_datetime(requested_start)
    end = parse_datetime(requested_end)
    if start and observed_at < start:
        return {
            "valid": False,
            "reason": "observed_at_before_request_start",
            "action": "exclude_from_risk_calculation",
        }
    if end and observed_at > end:
        return {
            "valid": False,
            "reason": "observed_at_after_request_end",
            "action": "exclude_from_risk_calculation",
        }

    return {"valid": True, "reason": "ok", "action": "include_in_risk_calculation"}


def validate_collection(
    observations: list[dict[str, Any]],
    *,
    requested_start: str | None = None,
    requested_end: str | None = None,
) -> dict[str, Any]:
    results = [
        validate_observation(item, requested_start=requested_start, requested_end=requested_end)
        for item in observations
    ]
    return {
        "valid_count": sum(1 for item in results if item["valid"]),
        "invalid_count": sum(1 for item in results if not item["valid"]),
        "results": results,
    }
