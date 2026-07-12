from __future__ import annotations

import os
from typing import Any

import requests

from .api_snapshot import now_kst, save_snapshot
from .time_utils import to_ymdhm, ymdhm_to_iso


SOURCE_NAME = "한강홍수통제소 표준수문DB"
SOURCE_URL = "https://www.data.go.kr/data/3040409/openapi.do"
API_BASE_URL = "https://api.hrfco.go.kr"


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _items_from_response(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    content = payload.get("content")
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    response = payload.get("response")
    if isinstance(response, dict):
        body = response.get("body") or {}
        items = body.get("items") or {}
        item = items.get("item") if isinstance(items, dict) else items
        if isinstance(item, list):
            return [row for row in item if isinstance(row, dict)]
        if isinstance(item, dict):
            return [item]
    return []


class HrfcoClient:
    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = os.getenv("HRFCO_API_KEY", "") if api_key is None else api_key
        self.timeout = timeout

    def _error(
        self,
        *,
        reason: str,
        message: str,
        request_params: dict[str, Any],
        snapshot_prefix: str,
        raw_response: Any = None,
    ) -> dict[str, Any]:
        save_snapshot(
            prefix=snapshot_prefix,
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            request_params=request_params,
            raw_response=raw_response,
            status="error",
            reason=reason,
        )
        return {
            "status": "error",
            "reason": reason,
            "source": SOURCE_NAME,
            "message": message,
            "data": [],
            "fetched_at": now_kst(),
        }

    def _request_json(self, path: str, request_params: dict[str, Any], snapshot_prefix: str) -> dict[str, Any]:
        if not self.api_key:
            return self._error(
                reason="missing_api_key",
                message="HRFCO_API_KEY가 없어 실제 데이터를 불러오지 않았습니다. 임의 데이터로 대체하지 않습니다.",
                request_params=request_params,
                snapshot_prefix=snapshot_prefix,
            )

        url = f"{API_BASE_URL}/{self.api_key}{path}"
        try:
            response = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            return self._error(
                reason="external_api_failed",
                message="실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.",
                request_params={**request_params, "url": url.replace(self.api_key, "***")},
                snapshot_prefix=snapshot_prefix,
                raw_response={"exception": str(exc)},
            )

        redacted_url = url.replace(self.api_key, "***")
        request_params = {**request_params, "url": redacted_url, "http_status": response.status_code}
        try:
            raw = response.json()
        except ValueError:
            raw = {"text": response.text}

        status = "success" if response.ok else "error"
        save_snapshot(
            prefix=snapshot_prefix,
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            request_params=request_params,
            raw_response=raw,
            status=status,
            reason=None if response.ok else "external_api_failed",
        )
        if not response.ok:
            return {
                "status": "error",
                "reason": "external_api_failed",
                "source": SOURCE_NAME,
                "message": "실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.",
                "data": [],
                "fetched_at": now_kst(),
            }
        return {"status": "success", "raw": raw, "fetched_at": now_kst()}

    def get_rainfall(self, station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
        start = to_ymdhm(start_time)
        end = to_ymdhm(end_time)
        path = f"/rainfall/list/10M/{station_code}/{start}/{end}.json"
        result = self._request_json(
            path,
            {
                "hydro_type": "rainfall",
                "station_code": station_code,
                "start_time": start_time,
                "end_time": end_time,
            },
            f"hrfco_rainfall_{station_code}_{start}_{end}",
        )
        if result.get("status") != "success":
            return result
        data = []
        for item in _items_from_response(result["raw"]):
            observed_at = ymdhm_to_iso(item.get("ymdhm"))
            data.append(
                {
                    "station_code": item.get("rfobscd") or station_code,
                    "observed_at": observed_at,
                    "value": _safe_float(item.get("rf")),
                    "unit": "mm",
                    "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_type": "public_api",
                    "fetched_at": result["fetched_at"],
                    "raw": item,
                }
            )
        return {"status": "success", "data": data, "source": SOURCE_NAME, "fetched_at": result["fetched_at"]}

    def get_water_level(self, station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
        start = to_ymdhm(start_time)
        end = to_ymdhm(end_time)
        path = f"/waterlevel/list/10M/{station_code}/{start}/{end}.json"
        result = self._request_json(
            path,
            {
                "hydro_type": "waterlevel",
                "station_code": station_code,
                "start_time": start_time,
                "end_time": end_time,
            },
            f"hrfco_water_level_{station_code}_{start}_{end}",
        )
        if result.get("status") != "success":
            return result
        data = []
        for item in _items_from_response(result["raw"]):
            observed_at = ymdhm_to_iso(item.get("ymdhm"))
            data.append(
                {
                    "station_code": item.get("wlobscd") or station_code,
                    "observed_at": observed_at,
                    "value": _safe_float(item.get("wl")),
                    "unit": "m",
                    "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_type": "public_api",
                    "fetched_at": result["fetched_at"],
                    "raw": item,
                }
            )
        return {"status": "success", "data": data, "source": SOURCE_NAME, "fetched_at": result["fetched_at"]}

    def get_discharge(self, station_code: str, start_time: str, end_time: str) -> dict[str, Any]:
        water_result = self.get_water_level(station_code, start_time, end_time)
        if water_result.get("status") != "success":
            water_result["message"] = water_result.get("message") or "수위자료의 유량 필드를 불러오지 못했습니다."
            return water_result
        data = []
        for row in water_result["data"]:
            item = row.get("raw") or {}
            data.append(
                {
                    "station_code": row.get("station_code") or station_code,
                    "observed_at": row.get("observed_at"),
                    "value": _safe_float(item.get("fw")),
                    "unit": "m3/s",
                    "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_type": "public_api",
                    "fetched_at": row.get("fetched_at"),
                    "raw": item,
                }
            )
        return {
            "status": "success",
            "data": data,
            "source": f"{SOURCE_NAME} 수위자료 유량필드",
            "fetched_at": water_result.get("fetched_at"),
        }

    def get_flood_warning(self, station_code: str | None, start_time: str, end_time: str) -> dict[str, Any]:
        end = to_ymdhm(end_time)
        path = f"/fldfct/list/{end}.json"
        result = self._request_json(
            path,
            {
                "hydro_type": "fldfct",
                "station_code": station_code,
                "start_time": start_time,
                "end_time": end_time,
            },
            f"hrfco_flood_warning_{station_code or 'all'}_{end}",
        )
        if result.get("status") != "success":
            return result
        data = []
        for item in _items_from_response(result["raw"]):
            observed_at = ymdhm_to_iso(item.get("ymdhm") or item.get("fctdt") or item.get("anncdt"))
            if station_code and station_code not in str(item):
                continue
            data.append(
                {
                    "station_code": station_code,
                    "observed_at": observed_at,
                    "value": item.get("fctwtr") or item.get("kind") or item.get("warn_kind"),
                    "unit": "warning",
                    "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_type": "public_api",
                    "fetched_at": result["fetched_at"],
                    "raw": item,
                }
            )
        return {"status": "success", "data": data, "source": SOURCE_NAME, "fetched_at": result["fetched_at"]}

    def get_station_metadata(self) -> dict[str, Any]:
        station_types = ("rainfall", "waterlevel", "dam", "bo")
        all_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for station_type in station_types:
            path = f"/{station_type}/info.json"
            result = self._request_json(
                path,
                {"hydro_type": station_type, "request": "station_metadata"},
                f"hrfco_{station_type}_metadata",
            )
            if result.get("status") != "success":
                errors.append({"station_type": station_type, **result})
                continue
            for item in _items_from_response(result["raw"]):
                all_rows.append(
                    {
                        "station_type": station_type,
                        "source_name": SOURCE_NAME,
                        "source_url": SOURCE_URL,
                        "source_type": "public_api",
                        "fetched_at": result["fetched_at"],
                        "raw": item,
                    }
                )
        if not all_rows and errors:
            return errors[0]
        return {
            "status": "success",
            "data": all_rows,
            "errors": errors,
            "source": SOURCE_NAME,
            "fetched_at": now_kst(),
        }
