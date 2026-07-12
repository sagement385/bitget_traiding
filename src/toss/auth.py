from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from src.utils.env import load_dotenv


TOSS_API_BASE_URL = "https://openapi.tossinvest.com"


class TossApiConfigurationError(RuntimeError):
    """Raised when the local Toss Open API configuration is incomplete."""


class TossApiError(RuntimeError):
    """Raised when Toss returns a non-successful API response."""


@dataclass(frozen=True)
class TossCredentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> "TossCredentials":
        load_dotenv()
        # The WTS UI labels these as API Key / Secret Key. The official REST
        # contract calls the same values client_id / client_secret.
        return cls(
            client_id=os.getenv("TOSS_CLIENT_ID", "") or os.getenv("TOSS_API_KEY", ""),
            client_secret=os.getenv("TOSS_CLIENT_SECRET", "") or os.getenv("TOSS_SECRET_KEY", ""),
        )


class TossOAuthClient:
    """OAuth2 Client Credentials token cache shared by all Toss adapters.

    Toss permits one active token per client. A process-wide cache prevents
    chart, portfolio, and risk polling clients from invalidating each other's
    token by requesting a new one for every request.
    """

    _cache: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()

    def __init__(self, *, base_url: str = TOSS_API_BASE_URL, session: requests.Session | None = None, timeout: int = 15):
        load_dotenv()
        self.base_url = (base_url or TOSS_API_BASE_URL).rstrip("/")
        self.credentials = TossCredentials.from_env()
        self.session = session or requests.Session()
        self.timeout = int(timeout)

    def has_credentials(self) -> bool:
        return bool(self.credentials.client_id and self.credentials.client_secret)

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if not self.has_credentials():
            raise TossApiConfigurationError("Toss credentials are missing: set TOSS_API_KEY and TOSS_SECRET_KEY")
        key = self.credentials.client_id
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if not force_refresh and cached and cached[1] > now:
                return cached[0]

            response = self.session.post(
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            _raise_for_toss_error(response, "OAuth token request failed")
            payload = response.json()
            token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
            if not token:
                raise TossApiError("OAuth token response did not include access_token")
            try:
                expires_in = max(60, int(payload.get("expires_in") or 86400))
            except (TypeError, ValueError):
                expires_in = 86400
            # Keep a one-minute margin so callers never use a token at expiry.
            self._cache[key] = (token, time.monotonic() + max(30, expires_in - 60))
            return token

    def authorization_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token(force_refresh=force_refresh)}"}

    @classmethod
    def reset_cache_for_testing(cls) -> None:
        with cls._lock:
            cls._cache.clear()


def _response_error_message(response: Any, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        return ": ".join(part for part in (fallback, code, message) if part)
    message = payload.get("message")
    return f"{fallback}: {message}" if message else fallback


def _raise_for_toss_error(response: Any, fallback: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise TossApiError(_response_error_message(response, fallback)) from exc

