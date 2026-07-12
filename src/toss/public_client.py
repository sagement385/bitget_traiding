from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd
import requests

from src.markets import KOR_STOCK, US_STOCK, normalize_market_type
from src.toss.auth import TOSS_API_BASE_URL, TossApiConfigurationError, TossApiError, TossOAuthClient, _raise_for_toss_error
from src.utils.time import now_ms, to_ms


@dataclass(frozen=True)
class TossPublicEndpoints:
    base_url: str = TOSS_API_BASE_URL
    candles_path: str = "/api/v1/candles"
    stocks_path: str = "/api/v1/stocks"
    prices_path: str = "/api/v1/prices"

    @classmethod
    def from_env(cls) -> "TossPublicEndpoints":
        # Optional override exists for an approved test/sandbox environment.
        return cls(base_url=(os.getenv("TOSS_API_BASE_URL") or TOSS_API_BASE_URL).rstrip("/"))


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    text = str(value or "").strip()
    if text.isdigit():
        return _timestamp_ms(int(text))
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def normalize_toss_candles(rows: Iterable[dict[str, Any]], symbol: str, category: str, interval: str, market_type: str) -> pd.DataFrame:
    """Normalize the official Toss ``result.candles`` response into OHLCV."""
    normalized_market = normalize_market_type(market_type, category)
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            close = float(_first(row, "closePrice", "close", "closingPrice", "c"))
            volume = float(_first(row, "volume", "tradeVolume", "v", default=0) or 0)
            out.append({
                "timestamp": _timestamp_ms(_first(row, "timestamp", "time", "tradeTime", "dateTime", "t")),
                "open": float(_first(row, "openPrice", "open", "o", default=close)),
                "high": float(_first(row, "highPrice", "high", "h", default=close)),
                "low": float(_first(row, "lowPrice", "low", "l", default=close)),
                "close": close,
                "volume": volume,
                "turnover": float(_first(row, "turnover", "tradeAmount", "amount", default=close * volume) or 0),
                "symbol": symbol.upper(),
                "category": category,
                "interval": interval,
                "market_type": normalized_market,
            })
        except (TypeError, ValueError, KeyError):
            continue
    columns = ["timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval", "market_type"]
    return pd.DataFrame(out, columns=columns).drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True) if out else pd.DataFrame(columns=columns)


def normalize_toss_symbols(rows: Iterable[dict[str, Any]], market_type: str) -> list[dict[str, Any]]:
    """Normalize the official ``GET /api/v1/stocks`` response."""
    market_type = normalize_market_type(market_type)
    if market_type not in {KOR_STOCK, US_STOCK}:
        raise ValueError("Toss symbol catalog supports KOR_STOCK and US_STOCK only")
    default_exchange = "KRX" if market_type == KOR_STOCK else "NASDAQ"
    default_currency = "KRW" if market_type == KOR_STOCK else "USD"
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(_first(row, "symbol", default="") or "").upper().strip()
        if not symbol:
            continue
        status = str(_first(row, "status", default="ACTIVE") or "ACTIVE").lower()
        out.append({
            "market_type": market_type,
            "symbol": symbol,
            "exchange": str(_first(row, "market", "exchange", default=default_exchange) or default_exchange).upper(),
            "name": str(_first(row, "name", "stockName", default=symbol) or symbol),
            "name_en": str(_first(row, "englishName", "nameEn", default="") or ""),
            "currency": str(_first(row, "currency", default=default_currency) or default_currency).upper(),
            "status": status,
            "is_tradeable": status == "active",
            "source": "toss_stock_info",
            "metadata": {"provider_row": row},
        })
    return out


class TossPublicClient:
    """Official Toss Securities REST market-data client.

    The provider currently exposes 1-minute and daily candles only. The
    canonical engine derives all other chart intervals from cached 1-minute
    candles, so those derived candles still remain provider-consistent.
    """

    _rate_lock = threading.Lock()
    _last_chart_request = 0.0

    def __init__(
        self,
        endpoints: TossPublicEndpoints | None = None,
        *,
        timeout: int = 15,
        session: requests.Session | None = None,
        auth: TossOAuthClient | None = None,
    ):
        self.endpoints = endpoints or TossPublicEndpoints.from_env()
        self.timeout = int(timeout)
        self.session = session or requests.Session()
        self.auth = auth or TossOAuthClient(base_url=self.endpoints.base_url, timeout=self.timeout)

    def has_credentials(self) -> bool:
        return self.auth.has_credentials()

    @staticmethod
    def _validate_market(market_type: str) -> str:
        market = normalize_market_type(market_type)
        if market not in {KOR_STOCK, US_STOCK}:
            raise ValueError("TossPublicClient supports KOR_STOCK and US_STOCK only")
        return market

    @staticmethod
    def _iso_before(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")

    def _chart_rate_limit(self) -> None:
        # MARKET_DATA_CHART is limited to 5 TPS. Keep a little headroom for
        # the UI's live polling and concurrent cache-hole repair.
        with self._rate_lock:
            wait = 0.23 - (time.monotonic() - self._last_chart_request)
            if wait > 0:
                time.sleep(wait)
            self.__class__._last_chart_request = time.monotonic()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, retry_token: bool = True) -> dict[str, Any]:
        headers = self.auth.authorization_headers()
        response = self.session.request(method, f"{self.endpoints.base_url}{path}", params=params, headers=headers, timeout=self.timeout)
        if response.status_code == 401 and retry_token:
            headers = self.auth.authorization_headers(force_refresh=True)
            response = self.session.request(method, f"{self.endpoints.base_url}{path}", params=params, headers=headers, timeout=self.timeout)
        _raise_for_toss_error(response, f"Toss {method} {path} request failed")
        payload = response.json()
        if not isinstance(payload, dict):
            raise TossApiError(f"Toss {method} {path} response must be a JSON object")
        return payload

    def _candle_page(self, symbol: str, interval: str, *, count: int, before: str | None, adjusted: bool = True) -> tuple[list[dict[str, Any]], str | None]:
        if interval not in {"1m", "1D", "1d"}:
            raise ValueError("Official Toss candle API supports only 1m and 1D")
        self._chart_rate_limit()
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": "1d" if interval in {"1D", "1d"} else "1m",
            "count": max(1, min(200, int(count))),
            "adjusted": "true" if adjusted else "false",
        }
        if before:
            params["before"] = before
        payload = self._request("GET", self.endpoints.candles_path, params=params)
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise TossApiError("Toss candle response did not include result")
        rows = result.get("candles", [])
        if not isinstance(rows, list):
            raise TossApiError("Toss candle response did not include candles")
        next_before = result.get("nextBefore")
        return rows, str(next_before) if next_before else None

    def get_recent_candles(self, symbol: str, category: str, interval: str, *, market_type: str, limit: int = 200) -> pd.DataFrame:
        self._validate_market(market_type)
        # The API returns at most 200. The local cache owns the longer chart.
        rows, _ = self._candle_page(symbol, interval, count=min(200, int(limit)), before=None)
        return normalize_toss_candles(rows, symbol, category, interval, market_type).tail(min(200, int(limit))).reset_index(drop=True)

    def download_candles(
        self,
        symbol: str,
        category: str,
        interval: str,
        start: str | int,
        end: str | int,
        *,
        market_type: str | None = None,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        market = self._validate_market(market_type or normalize_market_type(None, category))
        start_ms, end_ms = to_ms(start), min(to_ms(end), now_ms())
        if start_ms >= end_ms:
            return normalize_toss_candles([], symbol, category, interval, market)

        cursor = self._iso_before(end_ms + 1)
        pages: list[dict[str, Any]] = []
        while cursor:
            rows, next_before = self._candle_page(symbol, interval, count=200, before=cursor, adjusted=adjusted)
            if not rows:
                break
            pages.extend(rows)
            normalized = normalize_toss_candles(rows, symbol, category, interval, market)
            if normalized.empty or int(normalized["timestamp"].min()) <= start_ms or not next_before:
                break
            cursor = next_before

        frame = normalize_toss_candles(pages, symbol, category, interval, market)
        return frame[(frame["timestamp"] >= start_ms) & (frame["timestamp"] <= end_ms)].reset_index(drop=True)

    def get_stock_info(self, symbols: Iterable[str], market_type: str) -> list[dict[str, Any]]:
        market = self._validate_market(market_type)
        requested = [str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()]
        if not requested:
            return []
        payload = self._request("GET", self.endpoints.stocks_path, params={"symbols": ",".join(requested[:200])})
        rows = payload.get("result", [])
        if not isinstance(rows, list):
            raise TossApiError("Toss stock response did not include result list")
        return normalize_toss_symbols(rows, market)

    def get_prices(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        requested = [str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()]
        if not requested:
            return []
        payload = self._request("GET", self.endpoints.prices_path, params={"symbols": ",".join(requested[:200])})
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise TossApiError("Toss prices response did not include result list")
        return result

    def download_stock_universe(self, market_type: str) -> list[dict[str, Any]]:
        self._validate_market(market_type)
        # The official API supports up to 200 explicitly supplied symbols, but
        # does not publish an all-market symbol dump. Keeping this explicit
        # avoids pretending that a partial lookup is a complete universe.
        raise TossApiConfigurationError(
            "The official Toss Open API does not provide a full stock-universe endpoint. "
            "Search by ticker or import a separately licensed symbol catalog into the local database."
        )
