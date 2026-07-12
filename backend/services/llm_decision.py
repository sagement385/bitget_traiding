from __future__ import annotations

import json
import os
from typing import Any

import requests

from .api_snapshot import now_kst
from .config import env_bool, strict_data_mode


ALLOWED_ACTIONS = [
    "close_underpass_recommendation",
    "inspect_levee_recommendation",
    "dispatch_field_team_recommendation",
    "notify_police_recommendation",
    "prepare_pump_station_recommendation",
    "inspect_drainage_gate_recommendation",
    "issue_driver_alert_recommendation",
    "monitor_cctv_recommendation",
]

FORBIDDEN_ACTIONS = [
    "operate_dam",
    "operate_gate",
    "declare_levee_collapse_without_official_event",
    "invent_missing_data",
]

SYSTEM_PROMPT = """너는 재난 대응 의사결정 보조 모듈이다.
제공된 데이터와 계산 결과만 사용한다.
제공되지 않은 강수량, 수위, 유량, 방류량, 좌표, 붕괴 여부는 절대 추정하지 않는다.
제방 붕괴는 official_events에 confirmed_levee_collapse가 있을 때만 확정 표현한다.
실시간 데이터만 있는 경우에는 "붕괴", "파괴", "유실"이라고 표현하지 말고 "점검 필요", "월류 위험", "현장 확인 필요"라고 표현한다.
허용된 allowed_actions 중에서만 조치를 선택한다.
실제 댐, 수문, 배수문을 작동시키는 명령은 생성하지 않는다.
출력은 반드시 지정된 JSON Schema를 따른다."""

DECISION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk_level", "summary", "data_gaps", "actions", "map_updates"],
    "properties": {
        "risk_level": {"type": "string", "enum": ["normal", "watch", "danger", "critical", "unknown"]},
        "summary": {"type": "string"},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "target", "priority", "reason", "source_basis", "confidence"],
                "properties": {
                    "action": {"type": "string", "enum": ALLOWED_ACTIONS},
                    "target": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string"},
                    "source_basis": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        },
        "map_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "status", "color", "source_basis"],
                "properties": {
                    "target": {"type": "string"},
                    "status": {"type": "string"},
                    "color": {"type": "string", "enum": ["green", "yellow", "orange", "red", "gray"]},
                    "source_basis": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def build_llm_input(
    *,
    scenario_id: str,
    time: str,
    observations: dict[str, Any],
    official_events: list[dict[str, Any]],
    mechanical_analysis: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "time": time,
        "observations": {
            "rainfall": observations.get("rainfall", []),
            "water_level": observations.get("water_level", []),
            "discharge": observations.get("discharge", []),
            "dam": observations.get("dam", []),
            "warnings": observations.get("warnings", []),
        },
        "official_events": official_events,
        "mechanical_analysis": mechanical_analysis,
        "assets": assets,
        "allowed_actions": ALLOWED_ACTIONS,
        "forbidden_actions": FORBIDDEN_ACTIONS,
    }


def _has_confirmed_collapse(input_payload: dict[str, Any]) -> bool:
    return any(event.get("event_type") == "confirmed_levee_collapse" for event in input_payload.get("official_events", []))


def _basis_for_event(event: dict[str, Any]) -> str:
    return f"{event.get('time')} {event.get('event_type')} {event.get('source', {}).get('source_name', '')}".strip()


def deterministic_decision(input_payload: dict[str, Any]) -> dict[str, Any]:
    events = input_payload.get("official_events", [])
    event_types = {event.get("event_type") for event in events}
    basis = [_basis_for_event(event) for event in events]
    data_gaps = []
    observations = input_payload.get("observations", {})
    if not observations.get("rainfall"):
        data_gaps.append("강수량 API 또는 검증 관측값 없음")
    if not observations.get("water_level"):
        data_gaps.append("실시간 수위 API 또는 검증 관측값 없음")
    if not observations.get("discharge"):
        data_gaps.append("실시간 유량 API 또는 검증 관측값 없음")
    if not observations.get("dam"):
        data_gaps.append("K-water 댐 관측값 없음")
    if any(asset.get("coordinate_status") != "verified" for asset in input_payload.get("assets", [])):
        data_gaps.append("일부 시설 공식 좌표 없음")

    mechanical = input_payload.get("mechanical_analysis", {})
    water_risk = mechanical.get("water_level_risk", {}).get("status", "not_available")
    if "confirmed_levee_collapse" in event_types or "underpass_inflow_started" in event_types:
        risk_level = "critical"
    elif "overflow_started" in event_types or water_risk == "critical":
        risk_level = "critical"
    elif "design_flood_level_reached" in event_types or water_risk == "danger":
        risk_level = "danger"
    elif "flood_warning_issued" in event_types:
        risk_level = "danger"
    elif "flood_advisory_issued" in event_types or water_risk == "watch":
        risk_level = "watch"
    else:
        risk_level = "unknown"

    actions: list[dict[str, Any]] = []
    if event_types & {"flood_warning_issued", "design_flood_level_reached", "overflow_started", "confirmed_levee_collapse"}:
        actions.append(
            {
                "action": "close_underpass_recommendation",
                "target": "궁평2지하차도",
                "priority": 1,
                "reason": "공식 이벤트 또는 계산 결과상 통제 기준 검토가 필요한 단계입니다.",
                "source_basis": basis,
                "confidence": "high" if basis else "low",
            }
        )
    if event_types & {"overflow_started", "confirmed_levee_collapse"}:
        actions.append(
            {
                "action": "inspect_levee_recommendation",
                "target": "미호천교 부근 임시제방",
                "priority": 2,
                "reason": "공식 이벤트에 월류 또는 붕괴 관련 사실이 포함되어 있습니다.",
                "source_basis": basis,
                "confidence": "high",
            }
        )
    if event_types & {"flood_warning_issued", "overflow_started", "confirmed_levee_collapse", "underpass_inflow_started"}:
        actions.append(
            {
                "action": "dispatch_field_team_recommendation",
                "target": "미호천교 및 궁평2지하차도 주변",
                "priority": 3,
                "reason": "현장 확인이 필요한 공식 위험 이벤트가 존재합니다.",
                "source_basis": basis,
                "confidence": "medium",
            }
        )
    if event_types & {"flood_warning_issued", "design_flood_level_reached"}:
        actions.append(
            {
                "action": "issue_driver_alert_recommendation",
                "target": "궁평2지하차도 접근 도로",
                "priority": 4,
                "reason": "공식 경보 및 계획홍수위 도달 이벤트에 따른 운전자 경보 권고입니다.",
                "source_basis": basis,
                "confidence": "medium",
            }
        )
    if not actions:
        actions.append(
            {
                "action": "monitor_cctv_recommendation",
                "target": "궁평2지하차도",
                "priority": 1,
                "reason": "현재 시점에서 공식 이벤트와 실시간 관측 데이터가 부족합니다.",
                "source_basis": basis or ["source_missing"],
                "confidence": "low",
            }
        )

    map_updates = []
    if "underpass_inflow_started" in event_types:
        map_updates.append({"target": "궁평2지하차도", "status": "underpass_inflow_started", "color": "red", "source_basis": basis})
    elif event_types & {"flood_warning_issued", "design_flood_level_reached", "overflow_started"}:
        map_updates.append({"target": "궁평2지하차도", "status": "closure_review_required", "color": "orange", "source_basis": basis})
    else:
        map_updates.append({"target": "궁평2지하차도", "status": "unknown", "color": "gray", "source_basis": basis or ["source_missing"]})

    if "confirmed_levee_collapse" in event_types:
        map_updates.append({"target": "미호천교 부근 임시제방", "status": "confirmed_levee_collapse", "color": "red", "source_basis": basis})
    elif "overflow_started" in event_types:
        map_updates.append({"target": "미호천교 부근 임시제방", "status": "overflow_risk_or_started_official", "color": "orange", "source_basis": basis})
    else:
        map_updates.append({"target": "미호천교 부근 임시제방", "status": "field_check_required_if_realtime_risk", "color": "gray", "source_basis": basis or ["source_missing"]})

    return {
        "risk_level": risk_level,
        "summary": "공식 이벤트와 검증된 계산 결과만 반영했습니다.",
        "data_gaps": data_gaps,
        "actions": actions,
        "map_updates": map_updates,
    }


def sanitize_decision(decision: dict[str, Any], input_payload: dict[str, Any]) -> dict[str, Any]:
    allowed = set(input_payload.get("allowed_actions", ALLOWED_ACTIONS))
    has_collapse = _has_confirmed_collapse(input_payload)
    clean_actions = []
    for action in decision.get("actions", []):
        if action.get("action") not in allowed:
            continue
        if strict_data_mode() and not action.get("source_basis"):
            continue
        clean_actions.append(action)

    clean_updates = []
    for update in decision.get("map_updates", []):
        status_text = str(update.get("status", ""))
        if not has_collapse and any(word in status_text for word in ("collapse", "붕괴", "파괴", "유실")):
            update = {
                **update,
                "status": "field_confirmation_required",
                "color": "orange" if update.get("color") == "red" else update.get("color", "gray"),
            }
        if strict_data_mode() and not update.get("source_basis"):
            continue
        clean_updates.append(update)

    risk_level = decision.get("risk_level")
    if risk_level not in {"normal", "watch", "danger", "critical", "unknown"}:
        risk_level = "unknown"

    return {
        "risk_level": risk_level,
        "summary": str(decision.get("summary") or "데이터 부족"),
        "data_gaps": [str(item) for item in decision.get("data_gaps", [])],
        "actions": clean_actions,
        "map_updates": clean_updates,
    }


def _extract_response_text(payload: dict[str, Any]) -> str | None:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks) if chunks else None


def generate_decision(input_payload: dict[str, Any]) -> dict[str, Any]:
    fallback = deterministic_decision(input_payload)
    if not env_bool("ENABLE_LLM", True):
        return {"status": "fallback", "reason": "llm_disabled", "decision": sanitize_decision(fallback, input_payload)}
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return {"status": "fallback", "reason": "missing_openai_api_key", "decision": sanitize_decision(fallback, input_payload)}

    model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    request_payload = {
        "model": model,
        "store": False,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "disaster_decision",
                "strict": True,
                "schema": DECISION_OUTPUT_SCHEMA,
            }
        },
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_payload,
            timeout=30,
        )
        response.raise_for_status()
        raw = response.json()
        text = _extract_response_text(raw)
        if not text:
            raise ValueError("OpenAI response did not include output text")
        decision = json.loads(text)
        return {
            "status": "success",
            "model": model,
            "fetched_at": now_kst(),
            "decision": sanitize_decision(decision, input_payload),
        }
    except Exception as exc:
        return {
            "status": "fallback",
            "reason": "llm_api_failed",
            "message": str(exc),
            "decision": sanitize_decision(fallback, input_payload),
        }
