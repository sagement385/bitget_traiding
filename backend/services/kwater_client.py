from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .api_snapshot import now_kst, save_snapshot
from .time_utils import parse_datetime


SOURCE_NAME = "한국수자원공사_수문 운영 정보"
SOURCE_URL = "https://www.data.go.kr/data/15099110/openapi.do"
DAM_CODE_SOURCE_NAME = "한국수자원공사_댐코드 조회"
DAM_CODE_SOURCE_URL = "https://www.data.go.kr/data/15099105/openapi.do"
DAM_OPERATION_URL = "http://apis.data.go.kr/B500001/dam/sluicePresentCondition/hourlist"
DAM_CODE_URL = "http://apis.data.go.kr/B500001/dam/damCode/damCodelist"
KST = ZoneInfo("Asia/Seoul")


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _items_from_data_go(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    response = payload.get("response")
    if not isinstance(response, dict):
        return []
    body = response.get("body") or {}
    items = body.get("items") or {}
    item = items.get("item") if isinstance(items, dict) else items
    if isinstance(item, list):
        return [row for row in item if isinstance(row, dict)]
    if isinstance(item, dict):
        return [item]
    return []


def _operation_observed_at(raw: str | None, requested_start: str) -> str | None:
    if not raw:
        return None
    start = parse_datetime(requested_start)
    if start is None:
        return None
    match = re.search(r"(?P<month>\d{1,2})-(?P<day>\d{1,2})\s+(?P<hour>\d{1,2})", raw)
    if not match:
        return None
    parsed = datetime(
        start.year,
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        0,
        tzinfo=KST,
    )
    return parsed.isoformat(timespec="minutes")


class KwaterClient:
    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = os.getenv("KWATER_API_KEY", "") if api_key is None else api_key
        self.timeout = timeout

    def _error(
        self,
        *,
        reason: str,
        message: str,
        source_name: str,
        source_url: str,
        request_params: dict[str, Any],
        snapshot_prefix: str,
        raw_response: Any = None,
    ) -> dict[str, Any]:
        save_snapshot(
            prefix=snapshot_prefix,
            source_name=source_name,
            source_url=source_url,
            request_params=request_params,
            raw_response=raw_response,
            status="error",
            reason=reason,
        )
        return {
            "status": "error",
            "reason": reason,
            "source": source_name,
            "message": message,
            "data": [],
            "fetched_at": now_kst(),
        }

    def _request(self, url: str, source_name: str, source_url: str, params: dict[str, Any], prefix: str) -> dict[str, Any]:
        if not self.api_key:
            return self._error(
                reason="missing_api_key",
                message="KWATER_API_KEY가 없어 실제 데이터를 불러오지 않았습니다. 임의 데이터로 대체하지 않습니다.",
                source_name=source_name,
                source_url=source_url,
                request_params={k: v for k, v in params.items() if k != "serviceKey"},
                snapshot_prefix=prefix,
            )
        request_params = {**params, "serviceKey": self.api_key, "_type": "json"}
        try:
            response = requests.get(url, params=request_params, timeout=self.timeout)
        except requests.RequestException as exc:
            return self._error(
                reason="external_api_failed",
                message="실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.",
                source_name=source_name,
                source_url=source_url,
                request_params={**params, "serviceKey": "***"},
                snapshot_prefix=prefix,
                raw_response={"exception": str(exc)},
            )

        try:
            raw = response.json()
        except ValueError:
            raw = {"text": response.text}
        safe_params = {**params, "serviceKey": "***", "http_status": response.status_code}
        save_snapshot(
            prefix=prefix,
            source_name=source_name,
            source_url=source_url,
            request_params=safe_params,
            raw_response=raw,
            status="success" if response.ok else "error",
            reason=None if response.ok else "external_api_failed",
        )
        if not response.ok:
            return self._error(
                reason="external_api_failed",
                message="실제 데이터를 불러오지 못했습니다. 임의 데이터로 대체하지 않습니다.",
                source_name=source_name,
                source_url=source_url,
                request_params=safe_params,
                snapshot_prefix=prefix,
                raw_response=raw,
            )
        return {"status": "success", "raw": raw, "fetched_at": now_kst()}

    def get_dam_codes(self) -> dict[str, Any]:
        result = self._request(
            DAM_CODE_URL,
            DAM_CODE_SOURCE_NAME,
            DAM_CODE_SOURCE_URL,
            {},
            "kwater_dam_codes",
        )
        if result.get("status") != "success":
            return result
        data = [
            {
                "dam_code": item.get("damcode"),
                "dam_name": item.get("damnm"),
                "source_name": DAM_CODE_SOURCE_NAME,
                "source_url": DAM_CODE_SOURCE_URL,
                "source_type": "public_api",
                "fetched_at": result["fetched_at"],
                "raw": item,
            }
            for item in _items_from_data_go(result["raw"])
        ]
        return {"status": "success", "data": data, "source": DAM_CODE_SOURCE_NAME, "fetched_at": result["fetched_at"]}

    def _require_verified_dam_code(self, dam_code: str) -> dict[str, Any] | None:
        codes = self.get_dam_codes()
        if codes.get("status") != "success":
            return codes
        if any(str(item.get("dam_code")) == str(dam_code) for item in codes.get("data", [])):
            return None
        return {
            "status": "unavailable",
            "reason": "dam_code_not_verified",
            "source": DAM_CODE_SOURCE_NAME,
            "message": "공식 댐코드 조회 API에서 확인되지 않은 댐코드입니다. 임의 코드로 조회하지 않습니다.",
            "data": [],
            "fetched_at": now_kst(),
        }

    def get_dam_observations(self, dam_code: str, start_time: str, end_time: str) -> dict[str, Any]:
        verified_error = self._require_verified_dam_code(dam_code)
        if verified_error:
            return verified_error
        start = parse_datetime(start_time)
        end = parse_datetime(end_time)
        if start is None or end is None:
            return {
                "status": "error",
                "reason": "invalid_time_range",
                "source": SOURCE_NAME,
                "message": "조회 시간이 ISO-8601 형식이 아닙니다.",
                "data": [],
                "fetched_at": now_kst(),
            }
        params = {
            "pageNo": 1,
            "numOfRows": 1000,
            "damcode": dam_code,
            "stdt": start.strftime("%Y-%m-%d"),
            "eddt": end.strftime("%Y-%m-%d"),
        }
        result = self._request(
            DAM_OPERATION_URL,
            SOURCE_NAME,
            SOURCE_URL,
            params,
            f"kwater_dam_{dam_code}_{params['stdt']}_{params['eddt']}",
        )
        if result.get("status") != "success":
            return result
        data = []
        for item in _items_from_data_go(result["raw"]):
            observed_at = _operation_observed_at(item.get("obsrdt"), start_time)
            data.append(
                {
                    "dam_code": dam_code,
                    "observed_at": observed_at,
                    "dam_level": {
                        "value": _safe_float(item.get("lowlevel")),
                        "unit": "EL.m",
                        "source_name": SOURCE_NAME,
                        "source_url": SOURCE_URL,
                        "source_type": "public_api",
                    },
                    "rainfall": {"value": _safe_float(item.get("rf")), "unit": "mm"},
                    "inflow": {"value": _safe_float(item.get("inflowqy")), "unit": "m3/s"},
                    "total_release": {"value": _safe_float(item.get("totdcwtrqy")), "unit": "m3/s"},
                    "storage": {"value": _safe_float(item.get("rsvwtqy")), "unit": "million_m3"},
                    "storage_rate": {"value": _safe_float(item.get("rsvwtrt")), "unit": "%"},
                    "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_type": "public_api",
                    "fetched_at": result["fetched_at"],
                    "raw": item,
                }
            )
        return {"status": "success", "data": data, "source": SOURCE_NAME, "fetched_at": result["fetched_at"]}

    def get_dam_latest(self, dam_code: str) -> dict[str, Any]:
        today = datetime.now(KST).isoformat(timespec="minutes")
        result = self.get_dam_observations(dam_code, today, today)
        if result.get("status") != "success":
            return result
        rows = result.get("data", [])
        return {**result, "data": rows[-1:] if rows else []}
