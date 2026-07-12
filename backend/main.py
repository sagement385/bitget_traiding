from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .services.hrfco_client import HrfcoClient
from .services.kwater_client import KwaterClient
from .services.replay_service import build_replay, load_official_event, scenario_summary


app = FastAPI(
    title="Chungbuk Flood Disaster Digital Twin API",
    version="0.1.0",
    description="Official-data-first replay API for the 2023 Osong underpass flood disaster.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "strict_data_mode": True,
        "fake_data_policy": "forbidden",
    }


@app.get("/api/scenarios")
def scenarios() -> dict[str, Any]:
    return {"status": "success", "data": [scenario_summary()]}


@app.get("/api/scenarios/osong_2023_07_15")
def osong_scenario() -> dict[str, Any]:
    return {"status": "success", "data": load_official_event()}


@app.get("/api/replay/osong_2023_07_15")
def osong_replay(at: str | None = Query(default=None, description="ISO-8601 replay time")) -> dict[str, Any]:
    return {"status": "success", "data": build_replay(at)}


@app.get("/api/hrfco/stations")
def hrfco_stations() -> dict[str, Any]:
    return HrfcoClient().get_station_metadata()


@app.get("/api/hrfco/rainfall")
def hrfco_rainfall(station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
    return HrfcoClient().get_rainfall(station_code, start_time, end_time)


@app.get("/api/hrfco/water-level")
def hrfco_water_level(station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
    return HrfcoClient().get_water_level(station_code, start_time, end_time)


@app.get("/api/hrfco/discharge")
def hrfco_discharge(station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
    return HrfcoClient().get_discharge(station_code, start_time, end_time)


@app.get("/api/hrfco/flood-warning")
def hrfco_flood_warning(
    start_time: str,
    end_time: str,
    station_code: str | None = None,
) -> dict[str, Any]:
    return HrfcoClient().get_flood_warning(station_code, start_time, end_time)


@app.get("/api/kwater/dam-codes")
def kwater_dam_codes() -> dict[str, Any]:
    return KwaterClient().get_dam_codes()


@app.get("/api/kwater/dams/{dam_code}/observations")
def kwater_dam_observations(dam_code: str, start_time: str, end_time: str) -> dict[str, Any]:
    return KwaterClient().get_dam_observations(dam_code, start_time, end_time)


@app.get("/api/kwater/dams/{dam_code}/latest")
def kwater_dam_latest(dam_code: str) -> dict[str, Any]:
    return KwaterClient().get_dam_latest(dam_code)
