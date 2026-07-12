from fastapi.testclient import TestClient

from backend.main import app
from backend.services.data_validator import validate_observation
from backend.services.hrfco_client import HrfcoClient
from backend.services.kwater_client import KwaterClient
from backend.services.llm_decision import sanitize_decision
from backend.services.mechanical_calc import (
    discharge_trend,
    gate_backflow_check,
    release_trend,
    water_level_risk,
)
from backend.services.replay_service import build_replay, load_assets, load_official_event


OFFICIAL_DESIGN_FLOOD_LEVEL_EL_M = 29.02


def test_official_osong_timeline_loads_with_source():
    scenario = load_official_event()
    assert scenario["event_id"] == "osong_2023_07_15"
    assert scenario["source"]["source_type"] == "official_report"
    assert scenario["source"]["source_url"].startswith("https://www.opm.go.kr/")
    assert any(event["event_type"] == "confirmed_levee_collapse" for event in scenario["timeline"])


def test_mechanical_calc_uses_official_design_flood_level_only():
    result = water_level_risk(OFFICIAL_DESIGN_FLOOD_LEVEL_EL_M, OFFICIAL_DESIGN_FLOOD_LEVEL_EL_M)
    assert result["status"] == "critical"
    assert result["ratio"] == 1.0
    assert water_level_risk(None, OFFICIAL_DESIGN_FLOOD_LEVEL_EL_M)["status"] == "not_available"
    assert discharge_trend(None, None)["status"] == "unknown"
    assert release_trend(None, None)["causality_assertion"] == "not_asserted"
    assert gate_backflow_check(None, None)["operation_order"] is False


def test_assets_without_verified_coordinates_are_list_only():
    assets = load_assets()
    assert assets
    assert all(asset["coordinates"] is None for asset in assets)
    assert all(asset["coordinate_status"] == "source_missing" for asset in assets)
    assert all(asset["display_policy"] == "list_only_until_verified_coordinate_available" for asset in assets)


def test_replay_does_not_confirm_collapse_before_official_event():
    replay = build_replay("2023-07-15T07:50:00+09:00")
    statuses = [item["status"] for item in replay["decision_result"]["decision"]["map_updates"]]
    assert "confirmed_levee_collapse" not in statuses
    assert all("source_basis" in action for action in replay["decision_result"]["decision"]["actions"])


def test_replay_confirms_collapse_only_after_official_event():
    replay = build_replay("2023-07-15T08:09:00+09:00")
    statuses = [item["status"] for item in replay["decision_result"]["decision"]["map_updates"]]
    assert "confirmed_levee_collapse" in statuses


def test_llm_guardrail_removes_unallowed_actions_and_rewrites_unofficial_collapse():
    input_payload = {
        "allowed_actions": ["monitor_cctv_recommendation"],
        "official_events": [
            {
                "time": "2023-07-15T04:10:00+09:00",
                "event_type": "flood_warning_issued",
                "source": {"source_name": "official", "source_url": "https://example.invalid"},
            }
        ],
    }
    decision = {
        "risk_level": "critical",
        "summary": "test",
        "data_gaps": [],
        "actions": [
            {
                "action": "operate_dam",
                "target": "댐",
                "priority": 1,
                "reason": "forbidden",
                "source_basis": ["official"],
                "confidence": "low",
            },
            {
                "action": "monitor_cctv_recommendation",
                "target": "궁평2지하차도",
                "priority": 1,
                "reason": "allowed",
                "source_basis": ["official"],
                "confidence": "low",
            },
        ],
        "map_updates": [
            {
                "target": "미호천교 부근 임시제방",
                "status": "confirmed_levee_collapse",
                "color": "red",
                "source_basis": ["official"],
            }
        ],
    }
    sanitized = sanitize_decision(decision, input_payload)
    assert [action["action"] for action in sanitized["actions"]] == ["monitor_cctv_recommendation"]
    assert sanitized["map_updates"][0]["status"] == "field_confirmation_required"


def test_data_validator_excludes_missing_source_url():
    result = validate_observation(
        {
            "value": OFFICIAL_DESIGN_FLOOD_LEVEL_EL_M,
            "unit": "EL.m",
            "observed_at": "2023-07-15T06:40:00+09:00",
            "source_name": "국무조정실 오송 궁평2지하차도 침수사고 감찰조사 결과",
        }
    )
    assert result == {
        "valid": False,
        "reason": "missing_source_url",
        "action": "exclude_from_risk_calculation",
    }


def test_api_clients_return_errors_without_keys_and_no_fake_data():
    hrfco = HrfcoClient(api_key="")
    hrfco_result = hrfco.get_water_level(
        "source_missing",
        "2023-07-15T06:40:00+09:00",
        "2023-07-15T06:40:00+09:00",
    )
    assert hrfco_result["status"] == "error"
    assert hrfco_result["reason"] == "missing_api_key"
    assert hrfco_result["data"] == []

    kwater = KwaterClient(api_key="")
    kwater_result = kwater.get_dam_codes()
    assert kwater_result["status"] == "error"
    assert kwater_result["reason"] == "missing_api_key"
    assert kwater_result["data"] == []


def test_fastapi_replay_contract():
    client = TestClient(app)
    response = client.get("/api/replay/osong_2023_07_15", params={"at": "2023-07-15T06:40:00+09:00"})
    data = response.json()["data"]
    assert response.status_code == 200
    assert data["scenario"]["event_id"] == "osong_2023_07_15"
    assert data["mechanical_analysis"]["water_level_risk"]["source_basis"]
    assert data["map_policy"]["only_verified_coordinates_rendered"] is True
