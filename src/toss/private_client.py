from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from src.toss.auth import TOSS_API_BASE_URL, TossApiConfigurationError, TossApiError, TossOAuthClient, _raise_for_toss_error
from src.utils.env import load_dotenv


@dataclass(frozen=True)
class TossPrivateEndpoints:
    base_url: str = TOSS_API_BASE_URL
    account_path: str = "/api/v1/accounts"
    positions_path: str = "/api/v1/holdings"
    order_path: str = "/api/v1/orders"

    @classmethod
    def from_env(cls) -> "TossPrivateEndpoints":
        return cls(base_url=(os.getenv("TOSS_API_BASE_URL") or TOSS_API_BASE_URL).rstrip("/"))


class TossPrivateClient:
    """Official Toss account, asset, and order REST client.

    Live order submission remains opt-in. Read-only account and holding calls
    use the same OAuth cache as the public market-data client.
    """

    def __init__(
        self,
        endpoints: TossPrivateEndpoints | None = None,
        *,
        timeout: int = 15,
        allow_order_submission: bool = False,
        session: requests.Session | None = None,
        auth: TossOAuthClient | None = None,
        account_seq: str | int | None = None,
    ):
        load_dotenv()
        self.endpoints = endpoints or TossPrivateEndpoints.from_env()
        self.timeout = int(timeout)
        self.allow_order_submission = bool(allow_order_submission)
        self.session = session or requests.Session()
        self.auth = auth or TossOAuthClient(base_url=self.endpoints.base_url, timeout=self.timeout)
        self.account_seq = str(account_seq or os.getenv("TOSS_ACCOUNT_SEQ", "")).strip()
        self.allowed_ip = os.getenv("TOSS_ALLOWED_IP", "")

    @property
    def api_key(self) -> str:
        return self.auth.credentials.client_id

    @property
    def secret_key(self) -> str:
        return self.auth.credentials.client_secret

    def has_credentials(self) -> bool:
        return self.auth.has_credentials()

    def _headers(self, *, account_required: bool, force_refresh: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.auth.authorization_headers(force_refresh=force_refresh)}
        if account_required:
            headers["X-Tossinvest-Account"] = self._resolve_account_seq()
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        account_required: bool = False,
        retry_token: bool = True,
    ) -> dict[str, Any]:
        headers = self._headers(account_required=account_required)
        response = self.session.request(
            method.upper(),
            f"{self.endpoints.base_url}{path}",
            params=params,
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code == 401 and retry_token:
            headers = self._headers(account_required=account_required, force_refresh=True)
            response = self.session.request(
                method.upper(),
                f"{self.endpoints.base_url}{path}",
                params=params,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        _raise_for_toss_error(response, f"Toss {method} {path} request failed")
        body = response.json()
        if not isinstance(body, dict):
            raise TossApiError(f"Toss {method} {path} response must be a JSON object")
        return body

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", self.endpoints.account_path, account_required=False)

    def _resolve_account_seq(self) -> str:
        if self.account_seq:
            return self.account_seq
        accounts = self.get_account().get("result", [])
        if not isinstance(accounts, list):
            raise TossApiError("Toss accounts response did not include result list")
        brokerage = next((row for row in accounts if isinstance(row, dict) and row.get("accountType") == "BROKERAGE"), None)
        if not isinstance(brokerage, dict) or brokerage.get("accountSeq") is None:
            raise TossApiConfigurationError("No Toss BROKERAGE account is available. Set TOSS_ACCOUNT_SEQ after checking GET /api/v1/accounts.")
        self.account_seq = str(brokerage["accountSeq"])
        return self.account_seq

    def get_positions(self, market_type: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        # Toss returns KR and US holdings together. Filtering market_type is
        # intentionally done by the caller from each item's marketCountry.
        return self._request("GET", self.endpoints.positions_path, params=params, account_required=True)

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_order_submission:
            raise RuntimeError("Toss order submission is disabled. Enable it only after an explicit live-trading decision.")
        return self._request("POST", self.endpoints.order_path, payload=payload, account_required=True)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.allow_order_submission:
            raise RuntimeError("Toss order cancellation is disabled.")
        if not str(order_id or "").strip():
            raise ValueError("Toss cancellation requires the server-issued order_id")
        return self._request("POST", f"{self.endpoints.order_path}/{order_id}/cancel", payload={}, account_required=True)
