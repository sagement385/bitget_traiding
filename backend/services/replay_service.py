from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import BACKEND_ROOT
from .llm_decision import build_llm_input, generate_decision
from .mechanical_calc import discharge_trend, gate_backflow_check, release_trend, water_level_risk
from .time_utils import parse_datetime


EVENT_PATH = BACKEND_ROOT / "data" / "events" / "osong_2023_official.json"
ASSET_PATH = BACKEND_ROOT / "data" / "assets" / "osong_assets.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_official_event() -> dict[str, Any]:
    return _load_json(EVENT_PATH)


def load_assets() -> list[dict[str, Any]]:
    payload = _load_json(ASSET_PATH)
    return payload.get("assets", [])


def _with_source(event: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return {**event, "source": source}


def official_events_until(at: str | None) -> list[dict[str, Any]]:
    scenario = load_official_event()
    source = scenario["source"]
    if at is None:
        return [_with_source(event, source) for event in scenario.get("timeline", [])]
    at_dt = parse_datetime(at)
    if at_dt is None:
        return []
    events = []
    for event in scenario.get("timeline", []):
        event_dt = parse_datetime(event.get("time"))
        if event_dt and event_dt <= at_dt:
            events.append(_with_source(event, source))
    return events


def _official_design_level_observation(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == "design_flood_level_reached":
            value = event.get("design_flood_level_el_m")
            return {
                "value": value,
                "unit": "EL.m",
                "observed_at": event.get("time"),
                "source_name": event.get("source", {}).get("source_name"),
                "source_url": event.get("source", {}).get("source_url"),
                "source_type": event.get("source", {}).get("source_type"),
                "location": event.get("location"),
                "basis_event_type": event.get("event_type"),
            }
    return None


def build_observations(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    water_level = []
    design_level = _official_design_level_observation(events)
    if design_level:
        water_level.append(design_level)
    warnings = [
        {
            "value": event.get("event_type"),
            "unit": "warning",
            "observed_at": event.get("time"),
            "source_name": event.get("source", {}).get("source_name"),
            "source_url": event.get("source", {}).get("source_url"),
            "source_type": event.get("source", {}).get("source_type"),
            "location": event.get("location"),
            "description": event.get("description"),
        }
        for event in events
        if event.get("event_type") in {"flood_advisory_issued", "flood_warning_issued"}
    ]
    return {
        "rainfall": [],
        "water_level": water_level,
        "discharge": [],
        "dam": [],
        "warnings": warnings,
    }


def build_mechanical_analysis(events: list[dict[str, Any]]) -> dict[str, Any]:
    design_level = _official_design_level_observation(events)
    if design_level and design_level.get("value") is not None:
        water_risk = water_level_risk(float(design_level["value"]), float(design_level["value"]))
        water_risk["basis"] = "official_event_design_flood_level_reached"
        water_risk["source_basis"] = [design_level["source_name"], design_level["source_url"]]
    else:
        water_risk = water_level_risk(None, None)
        water_risk["source_basis"] = []

    return {
        "water_level_risk": water_risk,
        "discharge_trend": discharge_trend(None, None),
        "release_trend": release_trend(None, None),
        "gate_backflow_check": gate_backflow_check(None, None),
        "data_policy": {
            "interpolation": "forbidden",
            "invented_values": "forbidden",
            "collapse_inference_by_llm": "forbidden",
        },
    }


def scenario_summary() -> dict[str, Any]:
    scenario = load_official_event()
    return {
        "event_id": scenario["event_id"],
        "event_name": scenario["event_name"],
        "source": scenario["source"],
        "event_count": len(scenario.get("timeline", [])),
    }


def build_replay(at: str | None = None) -> dict[str, Any]:
    scenario = load_official_event()
    events = official_events_until(at)
    assets = load_assets()
    observations = build_observations(events)
    mechanical_analysis = build_mechanical_analysis(events)
    replay_time = at or scenario["timeline"][-1]["time"]
    llm_input = build_llm_input(
        scenario_id=scenario["event_id"],
        time=replay_time,
        observations=observations,
        official_events=events,
        mechanical_analysis=mechanical_analysis,
        assets=assets,
    )
    decision_result = generate_decision(llm_input)
    return {
        "scenario": scenario_summary(),
        "time": replay_time,
        "official_events": events,
        "observations": observations,
        "mechanical_analysis": mechanical_analysis,
        "assets": assets,
        "llm_input": llm_input,
        "decision_result": decision_result,
        "map_policy": {
            "only_verified_coordinates_rendered": True,
            "missing_coordinates_listed_only": True,
        },
        "comparison": {
            "basis": scenario["source"],
            "actual_outcome_note": "국무조정실 감찰조사 결과에 기록된 공식 사고 경과와 시스템 권고를 비교합니다.",
        },
    }
