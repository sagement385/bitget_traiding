from __future__ import annotations


def water_level_risk(current_level: float | None, reference_level: float | None) -> dict:
    """
    Compare current water level to a sourced reference level.
    Missing or invalid values are not calculated.
    """
    if current_level is None or reference_level is None:
        return {"status": "not_available", "reason": "missing_level_or_reference"}
    if reference_level <= 0:
        return {"status": "not_available", "reason": "invalid_reference_level"}

    ratio = current_level / reference_level
    if ratio < 0.7:
        status = "normal"
    elif ratio < 0.9:
        status = "watch"
    elif ratio < 1.0:
        status = "danger"
    else:
        status = "critical"
    return {"status": status, "ratio": ratio}


def discharge_trend(current_q: float | None, previous_q: float | None) -> dict:
    """Return the direction of discharge change only when both values exist."""
    if current_q is None or previous_q is None:
        return {"status": "unknown", "reason": "missing_discharge"}
    if current_q > previous_q:
        trend = "increasing"
    elif current_q < previous_q:
        trend = "decreasing"
    else:
        trend = "stable"
    return {"status": trend}


def release_trend(current_release: float | None, previous_release: float | None) -> dict:
    """
    Return dam release direction without asserting downstream flood causality.
    """
    if current_release is None or previous_release is None:
        return {
            "status": "unknown",
            "reason": "missing_release",
            "caution": "하류 영향 검토 필요",
            "causality_assertion": "not_asserted",
        }
    if current_release > previous_release:
        trend = "increasing"
    elif current_release < previous_release:
        trend = "decreasing"
    else:
        trend = "stable"
    return {
        "status": trend,
        "caution": "하류 영향 검토 필요",
        "causality_assertion": "not_asserted",
    }


def gate_backflow_check(river_level: float | None, inner_water_level: float | None) -> dict:
    """
    Suggest a review direction for drainage gates. This never issues an operation order.
    """
    if river_level is None or inner_water_level is None:
        return {
            "status": "not_available",
            "reason": "missing_river_or_inner_water_level",
            "recommended_action": "field_operator_review_required",
            "operation_order": False,
        }
    if river_level > inner_water_level:
        return {
            "status": "backflow_possible",
            "recommended_action": "keep_or_check_closed",
            "operation_order": False,
        }
    return {
        "status": "drainage_possible",
        "recommended_action": "inspect_opening_condition",
        "operation_order": False,
    }
