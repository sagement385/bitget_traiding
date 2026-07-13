from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, Body, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.backtest.engine import BacktestEngine
from src.bitget.public_client import BitgetPublicClient
from src.bitget.private_client import BitgetPrivateClient
from src.data_engine.canonical import CANONICAL_INTERVALS, bucket_start_ms, find_market_gaps, load_or_fetch_canonical_candles, market_to_ms
from src.data_engine.demo_data import make_demo_candles
from src.data_engine.historical_loader import download_to_csv, load_or_fetch_candles
from src.data_engine.storage import (
    ensure_stock_symbol,
    load_csv,
    load_candles,
    save_csv,
    search_stock_symbols,
    stock_universe_status,
    upsert_candles,
    upsert_stock_symbols,
)
from src.data_engine.candle_stream import LiveCandleStream, LiveSyntheticCandleStream, StreamConfig
from src.data_engine.tick_engine import LiveTickCandleStream, TickStreamConfig, SECOND_INTERVALS
from src.data_engine.validator import find_gaps
from src.utils.time import INTERVAL_MS, now_ms, to_ms
from src.strategies.factory import make_strategy
from src.indicators.library import add_common_indicators, surge_snapshot
from src.execution.market_state_manager import MarketStateManager
from src.markets import CRYPTO, KOR_STOCK, US_STOCK, normalize_market_type
from src.toss.private_client import TossPrivateClient
from src.toss.public_client import TossApiConfigurationError, TossPublicClient
from src.scanner.surge_scanner import MAX_BATCH_SIZE, SurgeScanner
from src.ui.account_state import build_notifications, build_risk_snapshot, normalize_account, normalize_fills, normalize_positions

app = FastAPI(title="Multi-Market Quant Trading System")
ROOT = Path.cwd()
RAW = ROOT / "data" / "raw"
OUT = ROOT / "results" / "backtests"
SETTINGS_PATH = ROOT / "results" / "ui_settings.json"
UI_STATIC = Path(__file__).resolve().parent / "static"
RAW.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(UI_STATIC)), name="static")
MARKET_STATE = MarketStateManager(risk_poll_seconds=20)
CSV_COMPAT_EXPORT = os.getenv("CSV_COMPAT_EXPORT", "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Keep the existing stock API contract available when no environment flag is
# present, while the browser stays Bitget-only unless the flag is explicit.
ENABLE_STOCK_MARKETS = _env_bool("ENABLE_STOCK_MARKETS", True)
UI_STOCK_MARKETS = _env_bool("ENABLE_STOCK_MARKETS", False)
ENABLE_SURGE_SCANNER = _env_bool("ENABLE_SURGE_SCANNER", False)
ENABLE_DEMO_TRADING = _env_bool("ENABLE_DEMO_TRADING", False)
ENABLE_LIVE_TRADING = _env_bool("ENABLE_LIVE_TRADING", False)

DEFAULT_RISK_SETTINGS = {
    "daily_loss_limit": 2000.0,
    "max_consecutive_losses": 3,
    "max_leverage": 3.0,
    "max_position_size": 0.0,
    "max_total_exposure": 0.0,
    "max_symbol_exposure": 0.0,
    "risk_per_trade_pct": 1.0,
    "kill_switch": False,
    "halt_on_connection_issue": True,
    "halt_on_data_delay": True,
}


def _read_ui_settings() -> dict[str, Any]:
    defaults = {"risk": dict(DEFAULT_RISK_SETTINGS)}
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    if not isinstance(payload, dict):
        return defaults
    risk = payload.get("risk")
    if isinstance(risk, dict):
        defaults["risk"].update({key: risk[key] for key in DEFAULT_RISK_SETTINGS if key in risk})
    return defaults


def _validate_risk_settings(payload: dict[str, Any]) -> dict[str, Any]:
    values = dict(DEFAULT_RISK_SETTINGS)
    source = payload.get("risk", payload)
    if not isinstance(source, dict):
        raise ValueError("risk settings must be an object")
    numeric_ranges = {
        "daily_loss_limit": (0.0, None),
        "max_consecutive_losses": (1.0, 100.0),
        "max_leverage": (0.1, 125.0),
        "max_position_size": (0.0, None),
        "max_total_exposure": (0.0, None),
        "max_symbol_exposure": (0.0, None),
        "risk_per_trade_pct": (0.0, 100.0),
    }
    for key, (minimum, maximum) in numeric_ranges.items():
        if key not in source:
            continue
        try:
            value = float(source[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} is outside the allowed range")
        values[key] = int(value) if key == "max_consecutive_losses" else value
    for key in ("kill_switch", "halt_on_connection_issue", "halt_on_data_delay"):
        if key in source:
            if not isinstance(source[key], bool):
                raise ValueError(f"{key} must be boolean")
            values[key] = source[key]
    return values


def _write_ui_settings(risk: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps({"risk": risk}, ensure_ascii=False, indent=2), encoding="utf-8")


def _position_count(payload: Any) -> int:
    """Count non-zero position records across provider-specific response shapes."""
    if isinstance(payload, dict):
        payload = payload.get("result", payload.get("data", payload.get("positions", payload.get("items", []))))
    if isinstance(payload, dict):
        payload = payload.get("items", payload.get("positions", []))
    if not isinstance(payload, list):
        return 0
    count = 0
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw = row.get("qty", row.get("quantity", row.get("size", row.get("positionQty", 0))))
        try:
            count += int(float(raw) != 0)
        except (TypeError, ValueError):
            continue
    return count


async def _toss_position_count(market_type: str) -> int | None:
    client = TossPrivateClient()
    if not client.has_credentials():
        return None
    try:
        return _position_count(await asyncio.to_thread(client.get_positions, market_type))
    except (TossApiConfigurationError, RuntimeError):
        # A stock chart must not keep a position poller alive until an account
        # has been selected and Toss can return its holdings.
        return None


async def _crypto_position_count() -> int | None:
    client = BitgetPrivateClient()
    if not client.has_credentials():
        return None
    return _position_count(await asyncio.to_thread(client.get_positions))


async def _toss_risk_poll(market_type: str) -> None:
    # Position polling is deliberately separate from heavy chart/quote streams.
    # Strategy-specific stop/target actions can be bound to this hook only after
    # a verified Toss order contract and explicit live-trading approval exist.
    await _toss_position_count(market_type)


MARKET_STATE.configure_market(KOR_STOCK, position_probe=lambda: _toss_position_count(KOR_STOCK), risk_check=lambda: _toss_risk_poll(KOR_STOCK))
MARKET_STATE.configure_market(US_STOCK, position_probe=lambda: _toss_position_count(US_STOCK), risk_check=lambda: _toss_risk_poll(US_STOCK))
MARKET_STATE.configure_market(CRYPTO, position_probe=_crypto_position_count)


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _normalize_category(value: Any) -> str:
    text = str(value or "USDT-FUTURES").upper().strip()
    aliases = {"USDT-FUTURE": "USDT-FUTURES", "USDC-FUTURE": "USDC-FUTURES", "COIN-FUTURE": "COIN-FUTURES"}
    return aliases.get(text, text)


def _is_stock_market(market_type: str) -> bool:
    return market_type in {KOR_STOCK, US_STOCK}


def _require_stock_feature() -> None:
    if not ENABLE_STOCK_MARKETS:
        raise RuntimeError("Stock markets are disabled. Set ENABLE_STOCK_MARKETS=true to enable them.")


def _require_surge_feature() -> None:
    if not ENABLE_SURGE_SCANNER:
        raise RuntimeError("Surge scanner is disabled. Set ENABLE_SURGE_SCANNER=true to enable it.")


def _stock_candle_category(market_type: str, exchange: str | None = None) -> str:
    """Keep candle-cache keys stable when Toss returns KOSPI/KOSDAQ metadata."""
    return "KRX" if market_type == KOR_STOCK else str(exchange or "NASDAQ").upper().strip()


def _is_direct_stock_symbol(value: str, market_type: str) -> bool:
    text = str(value or "").upper().strip()
    if market_type == KOR_STOCK:
        return bool(re.fullmatch(r"\d{6}", text))
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", text))


def _coerce_end_date(value: Any) -> str | None:
    # Empty end means: use current server time, not a stale fixed date.
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _df_from_chart_candles(candles: list[dict[str, Any]], symbol: str = "BTCUSDT", category: str = "USDT-FUTURES", interval: str = "1m", market_type: str = CRYPTO) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        try:
            t = int(float(c.get("time")))
            # Lightweight Charts uses seconds; some callers may already pass ms.
            ts = t if t > 10_000_000_000 else t * 1000
            rows.append({
                "timestamp": ts,
                "open": float(c.get("open")),
                "high": float(c.get("high")),
                "low": float(c.get("low")),
                "close": float(c.get("close")),
                "volume": float(c.get("volume", 0) or 0),
                "turnover": float(c.get("turnover", 0) or 0),
                "symbol": symbol.upper(),
                "category": category,
                "interval": interval,
                "market_type": normalize_market_type(market_type, category),
            })
        except Exception:
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


INTERVAL_TO_SECONDS = {
    "1s": 1,
    "3s": 3,
    "5s": 5,
    "15s": 15,
    "30s": 30,
    "1m": 60,
    "2m": 120,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "2H": 7200,
    "4H": 14400,
    "6H": 21600,
    "12H": 43200,
    "1D": 86400,
    "1W": 604800,
    "1M": 2592000,
}


def _csv_path(symbol: str, interval: str, market_type: str = CRYPTO) -> Path:
    return RAW / f"{normalize_market_type(market_type)}_{symbol.upper()}_{interval}.csv"


def _maybe_save_csv(df: pd.DataFrame, path: Path) -> None:
    """Keep CSV export opt-in; SQLite is the normal runtime cache."""
    if CSV_COMPAT_EXPORT:
        save_csv(df, str(path))


def _cached_window(
    symbol: str,
    interval: str,
    category: str = "USDT-FUTURES",
    start: str | int | None = None,
    end: str | int | None = None,
    limit: int = 1000,
    market_type: str = CRYPTO,
) -> tuple[pd.DataFrame, int, int]:
    step = INTERVAL_MS.get(interval)
    if not step:
        return pd.DataFrame(), 0, 0
    end_ms = min(market_to_ms(end, market_type, end_of_day=True), now_ms()) if end is not None else now_ms()
    requested_start = market_to_ms(start, market_type) if start is not None else end_ms - step * int(limit)
    start_ms = max(0, max(int(requested_start), int(end_ms - step * int(limit))))
    if interval in CANONICAL_INTERVALS:
        start_ms = bucket_start_ms(start_ms, interval, market_type)
    try:
        db = load_candles(symbol.upper(), category, interval, start_ms, end_ms, limit=None, market_type=market_type)
    except Exception:
        return pd.DataFrame(), start_ms, end_ms
    if db.empty:
        return db, start_ms, end_ms
    db = db.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    return db.tail(int(limit)).reset_index(drop=True), start_ms, end_ms


def _load_cached_range(
    symbol: str,
    interval: str,
    category: str = "USDT-FUTURES",
    start: str | int | None = None,
    end: str | int | None = None,
    limit: int = 1000,
    market_type: str = CRYPTO,
) -> pd.DataFrame:
    db, start_ms, end_ms = _cached_window(symbol, interval, category, start, end, limit, market_type)
    step = INTERVAL_MS.get(interval)
    if not step or db.empty:
        return pd.DataFrame()
    gaps = [g for g in find_market_gaps(db, interval, market_type) if g[2] > 0]
    if gaps:
        return pd.DataFrame()
    if market_type != CRYPTO:
        # Stock sessions have expected overnight, weekend, and holiday gaps.
        # The canonical loader performs session-aware repair before this
        # fallback is used, so cached stock rows remain usable when the provider
        # is temporarily unavailable.
        return db.tail(int(limit)).reset_index(drop=True)
    first = int(db["timestamp"].iloc[0])
    last = int(db["timestamp"].iloc[-1])
    if first > start_ms + step:
        return pd.DataFrame()
    # Allow the currently-forming candle to be absent, but do not serve an
    # obviously stale tail when the caller asked for data up to now.
    if end is None and last < end_ms - step * 2:
        return pd.DataFrame()
    return db.tail(int(limit)).reset_index(drop=True)


def _refresh_cached_canonical_window(
    symbol: str,
    category: str,
    interval: str,
    start: str | int | None,
    end: str | int | None,
    limit: int,
    market_type: str = CRYPTO,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Repair only the missing or stale part of a cached chart window.

    A stale cache tail used to fall through to a full canonical aggregation.
    That could read hundreds of thousands of 1m rows for a 1H/1D chart even
    when only the latest exchange bucket was missing.
    """
    market_type = normalize_market_type(market_type, category)
    cached, start_ms, end_ms = _cached_window(symbol, interval, category, start, end, limit, market_type)
    if market_type != CRYPTO:
        # Stock cache repair is always delegated to the canonical 1m pipeline.
        # It requests only regular-session holes, persists successful patches,
        # and never treats nights/weekends as chart gaps.
        _, report = load_or_fetch_canonical_candles(
            symbol.upper(), category, interval, start_ms, end_ms,
            limit=None, fetch=True, persist_derived=True,
            refresh_recent_tail=end is None, market_type=market_type,
        )
        cached, _, _ = _cached_window(symbol, interval, category, start, end, limit, market_type)
        return cached, {
            "source": "stock_cache_repaired",
            "rows": int(len(cached)),
            "fetched_1m": int(report.fetched_1m),
            "gaps_found": int(report.gaps_found),
        }
    step = INTERVAL_MS[interval]
    refresh_start: int | None = None
    force_refresh_start: int | None = None
    if cached.empty:
        refresh_start = start_ms
    else:
        gaps = [g for g in find_market_gaps(cached, interval, market_type) if g[2] > 0]
        first = int(cached["timestamp"].iloc[0])
        last = int(cached["timestamp"].iloc[-1])
        if first > start_ms + step:
            refresh_start = start_ms
        elif gaps:
            refresh_start = max(start_ms, bucket_start_ms(int(gaps[0][0]), interval, market_type))
        elif end is None and last < end_ms - step * 2:
            refresh_start = max(start_ms, bucket_start_ms(last, interval, market_type))
        elif end is None:
            # A candle with the current bucket timestamp may still contain an
            # old in-progress value. Refresh only the active bucket's recent
            # 1m source data, never the complete chart history.
            refresh_start = max(start_ms, bucket_start_ms(last, interval, market_type))
            force_refresh_start = max(refresh_start, end_ms - 5 * 60 * 1000)

    if refresh_start is not None:
        _, report = load_or_fetch_canonical_candles(
            symbol.upper(), category, interval, refresh_start, end_ms,
            limit=None, fetch=True, persist_derived=True,
            force_refresh_start_ms=force_refresh_start,
            refresh_recent_tail=False,
            market_type=market_type,
        )
        cached, _, _ = _cached_window(symbol, interval, category, start, end, limit, market_type)
        return cached, {
            "source": "db_cache_current_bucket_refreshed" if force_refresh_start is not None else "db_cache_tail_repaired",
            "rows": int(len(cached)),
            "refresh_start": int(refresh_start),
            "fetched_1m": int(report.fetched_1m),
            "gaps_found": int(report.gaps_found),
        }
    return cached, {"source": "db_cache", "rows": int(len(cached))}


def _chart_time_ms(value: Any) -> int | None:
    """Accept the chart's seconds timestamp without trusting its unit."""
    try:
        stamp = int(float(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp > 10_000_000_000 else stamp * 1000


def _repair_live_gap(
    symbol: str,
    category: str,
    interval: str,
    client_last_time: Any,
    market_type: str = CRYPTO,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fill only the interval from the displayed chart tail to now.

    The full historical cache has already been validated. When a live session
    starts later, this function deliberately looks only at the client cursor
    through the current time and asks Bitget for missing 1m rows in that span.
    """
    if interval.endswith("s") or interval not in CANONICAL_INTERVALS:
        return pd.DataFrame(), {"source": "live_no_history", "rows": 0}
    cursor_ms = _chart_time_ms(client_last_time)
    if cursor_ms is None:
        return pd.DataFrame(), {"source": "live_no_cursor", "rows": 0}
    end_ms = now_ms()
    if cursor_ms > end_ms:
        return pd.DataFrame(), {"source": "live_future_cursor", "rows": 0}
    market_type = normalize_market_type(market_type, category)
    start_ms = bucket_start_ms(cursor_ms, interval, market_type)
    force_refresh_start = max(start_ms, end_ms - 5 * 60 * 1000)
    df, report = load_or_fetch_canonical_candles(
        symbol.upper(), category, interval, start_ms, end_ms,
        limit=None, fetch=True, persist_derived=True,
        force_refresh_start_ms=force_refresh_start,
        refresh_recent_tail=False,
        market_type=market_type,
    )
    return df, {
        "source": "live_gap_repair",
        "rows": int(len(df)),
        "from": int(start_ms),
        "to": int(end_ms),
        "fetched_1m": int(report.fetched_1m),
        "gaps_found": int(report.gaps_found),
    }


def _load_local(symbol: str, interval: str, category: str = "USDT-FUTURES", limit: int = 1000, market_type: str = CRYPTO) -> pd.DataFrame:
    # Prefer the interval-specific SQLite cache so opening the UI does not
    # re-aggregate a large 1m history range on every request.
    db = _load_cached_range(symbol, interval, category, limit=limit, market_type=market_type)
    if not db.empty:
        return db
    # If the interval cache is missing, derive a bounded contiguous slice from
    # the canonical 1m cache. CSV fallback is kept for compatibility with older runs.
    if interval in CANONICAL_INTERVALS:
        try:
            end_ms = now_ms()
            start_ms = max(0, end_ms - INTERVAL_MS[interval] * int(limit))
            can, report = load_or_fetch_canonical_candles(
                symbol.upper(),
                category,
                interval,
                start_ms,
                end_ms,
                limit=limit,
                fetch=False,
                persist_derived=False,
                market_type=market_type,
            )
            if not can.empty and int(report.gaps_found) == 0:
                return can.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        except Exception:
            pass
    p = _csv_path(symbol, interval, market_type)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(p)
        if "timestamp" in df:
            df = df.drop_duplicates("timestamp").sort_values("timestamp").tail(limit).reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def _ensure_data(symbol: str = "BTCUSDT", category: str = "USDT-FUTURES", interval: str = "1H", limit: int = 1000, market_type: str = CRYPTO) -> pd.DataFrame:
    df = _load_local(symbol, interval, category, limit=limit, market_type=market_type)
    if df.empty:
        # Demo fallback only for first-run UI. It is never used when source=bitget.
        df = make_demo_candles(symbol, category, interval)
        df["market_type"] = normalize_market_type(market_type, category)
        _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
        try:
            upsert_candles(df, market_type=market_type)
        except Exception:
            pass
    return df.tail(limit).reset_index(drop=True)


def _num(v: Any, default=0.0) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _candles_json(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    use = df.copy()
    for c in ["timestamp", "open", "high", "low", "close", "volume", "turnover"]:
        if c in use:
            use[c] = pd.to_numeric(use[c], errors="coerce")
    use = use.dropna(subset=["timestamp", "open", "high", "low", "close"])
    if use.empty:
        return []
    use["time"] = (use["timestamp"].astype("int64") // 1000).astype("int64")
    if "volume" not in use:
        use["volume"] = 0.0
    if "turnover" not in use:
        use["turnover"] = 0.0
    use["volume"] = use["volume"].fillna(0.0)
    use["turnover"] = use["turnover"].fillna(0.0)
    return use[["time", "open", "high", "low", "close", "volume", "turnover"]].to_dict(orient="records")


def _line_json(df: pd.DataFrame, key: str) -> list[dict[str, Any]]:
    if df.empty or "timestamp" not in df or key not in df:
        return []
    use = df[["timestamp", key]].copy()
    use["timestamp"] = pd.to_numeric(use["timestamp"], errors="coerce")
    use[key] = pd.to_numeric(use[key], errors="coerce")
    use = use.dropna()
    if use.empty:
        return []
    use["time"] = (use["timestamp"].astype("int64") // 1000).astype("int64")
    return use[["time", key]].rename(columns={key: "value"}).to_dict(orient="records")


def _ma_json(df: pd.DataFrame, period: int) -> list[dict[str, Any]]:
    if df.empty or len(df) < period or "close" not in df:
        return []
    use = df[["timestamp", "close"]].copy()
    use["value"] = pd.to_numeric(use["close"], errors="coerce").rolling(period).mean()
    return _line_json(use, "value")




def _hist_json(df: pd.DataFrame, key: str) -> list[dict[str, Any]]:
    if df.empty or "timestamp" not in df or key not in df:
        return []
    use=df[["timestamp", key]].copy()
    use["timestamp"]=pd.to_numeric(use["timestamp"], errors="coerce")
    use[key]=pd.to_numeric(use[key], errors="coerce")
    use=use.dropna()
    if use.empty:
        return []
    use["time"] = (use["timestamp"].astype("int64") // 1000).astype("int64")
    return use[["time", key]].rename(columns={key: "value"}).to_dict(orient="records")


def _quote_turnover(df: pd.DataFrame) -> pd.Series:
    """Return quote-currency turnover, falling back to close * base volume."""
    index = df.index
    close = pd.to_numeric(df.get("close", pd.Series(0.0, index=index)), errors="coerce").fillna(0.0)
    volume = pd.to_numeric(df.get("volume", pd.Series(0.0, index=index)), errors="coerce").fillna(0.0)
    turnover = pd.to_numeric(df.get("turnover", pd.Series(0.0, index=index)), errors="coerce").fillna(0.0)
    fallback = close.mul(volume)
    return turnover.where(turnover > 0, fallback).fillna(0.0)


def _volume_json(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty or "timestamp" not in df or "volume" not in df:
        return []
    use=df.copy()
    for c in ["timestamp", "volume", "turnover", "open", "close"]:
        if c in use: use[c]=pd.to_numeric(use[c], errors="coerce")
    use=use.dropna(subset=["timestamp","volume"])
    if use.empty:
        return []
    use["time"] = (use["timestamp"].astype("int64") // 1000).astype("int64")
    use["value"] = _quote_turnover(use)
    use["color"] = (use["close"].fillna(0.0) >= use["open"].fillna(0.0)).map(
        {True: "rgba(34,197,94,.45)", False: "rgba(239,68,68,.45)"}
    )
    return use[["time", "value", "color"]].to_dict(orient="records")


def _indicator_payload(df: pd.DataFrame, enabled: set[str] | None = None, market_type: str | None = None) -> dict[str, Any]:
    if df.empty:
        return {"overlay": {}, "lower": {}, "surge": {"ready": False, "bars": 0}}
    overlay_keys = {"ema9", "ema21", "ema50", "sma50", "vwap", "bbUpper", "bbMid", "bbLower", "donchianHigh", "donchianLow", "supertrend"}
    lower_keys = {"volume", "volumeSma20", "rsi14", "macd", "macdSignal", "macdHist", "atr14", "obv", "stochK", "stochD"}
    active = set(enabled) if enabled is not None else overlay_keys | lower_keys
    column_keys = {
        "bbUpper": "bb_upper", "bbMid": "bb_mid", "bbLower": "bb_lower",
        "donchianHigh": "donchian_high", "donchianLow": "donchian_low",
        "volumeSma20": "ui_turnover_sma20", "macdSignal": "macd_signal",
        "macdHist": "macd_hist", "stochK": "stoch_k", "stochD": "stoch_d",
    }
    requested_columns = {column_keys.get(key, key) for key in active if key != "volume"}
    d = add_common_indicators(df, include=requested_columns)
    if "volumeSma20" in active:
        d["ui_turnover_sma20"] = _quote_turnover(d).rolling(20, min_periods=1).mean()
    resolved_market = normalize_market_type(
        market_type or (df.get("market_type", pd.Series([CRYPTO])).iloc[-1] if "market_type" in df else CRYPTO)
    )
    return {
        "overlay": {
            key: _line_json(d, column_keys.get(key, key))
            for key in overlay_keys if key in active
        },
        "lower": {
            key: (_volume_json(d) if key == "volume" else _hist_json(d, column_keys[key]) if key == "macdHist" else _line_json(d, column_keys.get(key, key)))
            for key in lower_keys if key in active
        },
        "surge": surge_snapshot(df, resolved_market),
    }

def _levels_json(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for lv in levels or []:
        try:
            start = int(int(lv.get("start_ts")) / 1000)
            end_raw = lv.get("end_ts") or lv.get("start_ts")
            end = int(int(end_raw) / 1000)
            if end <= start:
                end = start + 1
            item = {"start": start, "end": end, "side": str(lv.get("side", "")), "setup": str(lv.get("setup", ""))}
            for k in ("entry", "stop", "target1", "target2", "event_high", "event_low"):
                if lv.get(k) is not None:
                    item[k] = _num(lv.get(k))
            out.append(item)
        except Exception:
            continue
    return out


def _trades_json(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty or "timestamp" not in trades:
        return []
    out = []
    for _, r in trades.iterrows():
        side = str(r.get("side", "")).lower()
        out.append(
            {
                "time": int(int(_num(r.get("timestamp"))) / 1000),
                "position": "belowBar" if side == "buy" else "aboveBar",
                "color": "#22c55e" if side == "buy" else "#ef4444",
                "shape": "arrowUp" if side == "buy" else "arrowDown",
                "text": f"{side.upper()} {float(_num(r.get('price'))):.2f}",
                "side": side,
                "price": _num(r.get("price")),
                "qty": _num(r.get("qty")),
                "pnl": _num(r.get("pnl")),
                "reason": str(r.get("reason", "")),
            }
        )
    return out


def _drawdown_json(eq: pd.DataFrame) -> list[dict[str, Any]]:
    if eq.empty or "equity" not in eq:
        return []
    s = pd.to_numeric(eq["equity"], errors="coerce").ffill().bfill()
    dd = (s / s.cummax() - 1) * 100
    d = pd.DataFrame({"timestamp": eq["timestamp"], "value": dd})
    return _line_json(d, "value")


def _monthly_returns(eq: pd.DataFrame) -> list[dict[str, Any]]:
    if eq.empty or "timestamp" not in eq or "equity" not in eq:
        return []
    d = eq.copy()
    d["date"] = pd.to_datetime(d["timestamp"], unit="ms")
    d = d.set_index("date")
    m = d["equity"].resample("ME").last().pct_change().dropna() * 100
    rows = []
    for ts, v in m.items():
        rows.append({"year": int(ts.year), "month": int(ts.month), "value": float(v)})
    return rows


def _trade_rows(trades: pd.DataFrame, limit: int = 80) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    rows = []
    for _, r in trades.tail(limit).iloc[::-1].iterrows():
        rows.append(
            {
                "timestamp": int(_num(r.get("timestamp"))),
                "date": pd.to_datetime(int(_num(r.get("timestamp"))), unit="ms").strftime("%Y-%m-%d %H:%M"),
                "side": str(r.get("side", "")),
                "symbol": str(r.get("symbol", "")),
                "price": _num(r.get("price")),
                "qty": _num(r.get("qty")),
                "pnl": _num(r.get("pnl")),
                "cumPnl": _num(r.get("equity_after", 0)),
                "reason": str(r.get("reason", "")),
            }
        )
    return rows


def _validate_backtest_payload(payload: dict[str, Any]) -> None:
    """Reject invalid UI input before touching the candle cache or strategy engine."""
    allowed_strategies = {
        "trend_pullback", "donchian_atr_breakout", "chart_ai_consensus",
        # Compatibility-only names for saved jobs and older API clients.
        "sma_cross", "rsi", "rsi_reversal", "breakout", "scalp_vwap_rsi", "scalp",
        "price_action_volume", "pa_volume", "multi_timeframe_momentum", "mtf_momentum",
    }
    strategy = str(payload.get("strategy") or "trend_pullback").strip().lower()
    if strategy not in allowed_strategies:
        raise ValueError(f"Unsupported strategy: {strategy}")
    interval = str(payload.get("interval") or "1H").strip()
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    try:
        initial_cash = float(10000 if payload.get("initialCash") in (None, "") else payload.get("initialCash"))
        fee_rate = float(0.0006 if payload.get("feeRate") in (None, "") else payload.get("feeRate"))
        slippage_rate = float(0.0002 if payload.get("slippageRate") in (None, "") else payload.get("slippageRate"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Initial cash, fee rate, and slippage must be numeric") from exc
    if initial_cash <= 0:
        raise ValueError("Initial cash must be greater than zero")
    if fee_rate < 0 or slippage_rate < 0:
        raise ValueError("Fee rate and slippage cannot be negative")
    start = str(payload.get("start") or "2024-01-01").strip()
    end = str(payload.get("end") or "").strip()
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end) if end else None
    except Exception as exc:
        raise ValueError("Start and end dates must be valid dates") from exc
    if end_ts is not None and start_ts > end_ts:
        raise ValueError("Start date must be before the end date")
    positive_fields = ("volumeMult", "pullbackBars", "minRelVolume", "minAtrPct", "stopAtr", "takeAtr", "breakoutWindow", "rsiPeriod", "trendFast", "trendSlow", "entryWindow", "exitWindow", "minTurnoverRatio", "aiThreshold")
    for field in positive_fields:
        if payload.get(field) in (None, ""):
            continue
        try:
            if float(payload[field]) <= 0:
                raise ValueError(f"{field} must be greater than zero")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(field):
                raise
            raise ValueError(f"{field} must be numeric") from exc


def _run_backtest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _validate_backtest_payload(payload)
    symbol = str(payload.get("symbol") or "BTCUSDT").upper()
    category = _normalize_category(payload.get("category"))
    market_type = normalize_market_type(payload.get("market_type"), category)
    interval = str(payload.get("interval") or "1H")
    strategy_name = str(payload.get("strategy") or "trend_pullback").strip().lower()
    fast = int(payload.get("fast") or 20)
    slow = int(payload.get("slow") or 60)
    initial_cash = float(10000 if payload.get("initialCash") in (None, "") else payload.get("initialCash"))
    fee_rate = float(0.0006 if payload.get("feeRate") in (None, "") else payload.get("feeRate"))
    slippage_rate = float(0.0002 if payload.get("slippageRate") in (None, "") else payload.get("slippageRate"))
    allow_short = bool(payload.get("allowShort") or False)
    source = str(payload.get("source") or "local")
    start = str(payload.get("start") or "2024-01-01")
    end = _coerce_end_date(payload.get("end"))

    if source == "bitget" and market_type == CRYPTO:
        if interval.endswith("s"):
            # Bitget does not provide historical second candles. Seconds are only from saved live ticks.
            df = _ensure_data(symbol, category, interval, limit=1000)
        else:
            # UI backtest uses a bounded, cache-first window to avoid thousands of API calls.
            # For full offline history use: python -m src.main backfill-full.
            df, report = load_or_fetch_candles(symbol, category, interval, start, end, limit=int(payload.get("limit") or 600), source='bitget', repair=True, strict_backtest=True)
            _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
    elif source == "toss":
        if market_type == CRYPTO:
            raise ValueError("source=toss requires KOR_STOCK or US_STOCK")
        if interval.endswith("s"):
            raise ValueError("Toss stock backtests support 1m and larger intervals")
        df, _ = load_or_fetch_canonical_candles(
            symbol, category, interval, start, end,
            limit=1000, fetch=True, persist_derived=True, market_type=market_type,
        )
        _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
    elif source == "demo":
        df = make_demo_candles(symbol, category, interval)
        df["market_type"] = market_type
        _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
        try: upsert_candles(df, market_type=market_type)
        except Exception: pass
    else:
        df = _ensure_data(symbol, category, interval, limit=1000, market_type=market_type)

    if strategy_name == "trend_pullback":
        strat = make_strategy("trend_pullback", symbol=symbol, allow_short=allow_short,
                              fast=int(payload.get("trendFast") or 21), slow=int(payload.get("trendSlow") or 55),
                              min_turnover_ratio=float(payload.get("minTurnoverRatio") or 0.80),
                              stop_atr=float(payload.get("stopAtr") or 1.60), take_r=float(payload.get("takeAtr") or 2.00))
    elif strategy_name == "donchian_atr_breakout":
        strat = make_strategy("donchian_atr_breakout", symbol=symbol, allow_short=allow_short,
                              entry_window=int(payload.get("entryWindow") or 20), exit_window=int(payload.get("exitWindow") or 10),
                              min_turnover_ratio=float(payload.get("minTurnoverRatio") or 1.00),
                              stop_atr=float(payload.get("stopAtr") or 2.00), take_r=float(payload.get("takeAtr") or 2.50))
    elif strategy_name == "chart_ai_consensus":
        strat = make_strategy("chart_ai_consensus", symbol=symbol, allow_short=allow_short,
                              threshold=float(payload.get("aiThreshold") or 0.62),
                              min_atr_pct=float(payload.get("minAtrPct") or 0.12),
                              stop_atr=float(payload.get("stopAtr") or 1.80), take_r=float(payload.get("takeAtr") or 2.20))
    elif strategy_name == "sma_cross":
        strat = make_strategy("sma_cross", symbol=symbol, fast=fast, slow=slow, allow_short=allow_short)
    elif strategy_name in ("rsi", "rsi_reversal"):
        strat = make_strategy("rsi", symbol=symbol, period=int(payload.get("rsiPeriod") or 14), lower=float(payload.get("rsiLower") or 30), upper=float(payload.get("rsiUpper") or 70), allow_short=allow_short)
    elif strategy_name in ("scalp", "scalp_vwap_rsi"):
        strat = make_strategy("scalp_vwap_rsi", symbol=symbol, allow_short=allow_short,
                              min_rel_volume=float(payload.get("minRelVolume") or 0.80),
                              min_atr_pct=float(payload.get("minAtrPct") or 0.03),
                              stop_atr=float(payload.get("stopAtr") or 1.20),
                              take_atr=float(payload.get("takeAtr") or 1.80))
    elif strategy_name in ("price_action_volume", "pa_volume"):
        strat = make_strategy("price_action_volume", symbol=symbol, allow_short=allow_short,
                              volume_mult=float(payload.get("volumeMult") or 1.8),
                              pullback_bars=int(payload.get("pullbackBars") or 13))
    elif strategy_name in ("multi_timeframe_momentum", "mtf_momentum"):
        strat = make_strategy("multi_timeframe_momentum", symbol=symbol, allow_short=allow_short,
                              min_rel_volume=float(payload.get("minRelVolume") or 0.90),
                              min_atr_pct=float(payload.get("minAtrPct") or 0.035),
                              stop_atr=float(payload.get("stopAtr") or 1.30),
                              take_r=float(payload.get("takeAtr") or 2.00))
    else:
        strat = make_strategy("breakout", symbol=symbol, window=int(payload.get("breakoutWindow") or 20), allow_short=allow_short)

    result = BacktestEngine(
        df,
        strat,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        allow_short=allow_short,
        symbol=symbol,
        category=category,
        market_type=market_type,
    ).run()
    eq = result["equity_curve"]
    trades = result["trades"]
    metrics = result["metrics"]
    dataset = dict(result.get("dataset") or {})
    if dataset.get("first_timestamp") is not None:
        dataset.update(
            {
                "requested_start": start,
                "requested_end": end or "now",
                "actually_used_start": pd.to_datetime(int(dataset["first_timestamp"]), unit="ms").isoformat(),
                "actually_used_end": pd.to_datetime(int(dataset["last_timestamp"]), unit="ms").isoformat(),
                "limited": bool(len(df) >= int(payload.get("limit") or 600)),
            }
        )
    return {
        "ok": True,
        "symbol": symbol,
        "category": category,
        "market_type": market_type,
        "interval": interval,
        "candles": _candles_json(df),
        "maFast": _ma_json(df, fast),
        "maSlow": _ma_json(df, slow),
        "markers": _trades_json(trades),
        "tradeLevels": _levels_json(result.get("trade_levels", [])),
        "equity": _line_json(eq, "equity"),
        "drawdown": _drawdown_json(eq),
        "monthly": _monthly_returns(eq),
        "trades": _trade_rows(trades),
        "metrics": metrics,
        "dataset": dataset,
        "rows": int(len(df)),
        "indicators": _indicator_payload(df),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    flags = json.dumps({"stockMarkets": UI_STOCK_MARKETS, "surgeScanner": ENABLE_SURGE_SCANNER})
    return HTMLResponse(
        HTML.replace("__FEATURE_FLAGS__", flags),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/candles")
def api_candles(
    symbol: str = Query("BTCUSDT"),
    category: str = Query("USDT-FUTURES"),
    interval: str = Query("1H"),
    source: str = Query("local"),
    start: str = Query("2024-01-01"),
    end: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=6000),
    include_indicators: bool = Query(False),
    market_type: str = Query(CRYPTO),
):
    try:
        category = _normalize_category(category)
        market_type = normalize_market_type(market_type, category)
        if market_type != CRYPTO and not ENABLE_STOCK_MARKETS:
            raise RuntimeError("Stock markets are disabled. Set ENABLE_STOCK_MARKETS=true to enable them.")
        end = _coerce_end_date(end)
        if market_type == CRYPTO and source == "bitget":
            if interval.endswith("s"):
                df = _ensure_data(symbol, category, interval, limit=1000)
                report = None
            elif interval in CANONICAL_INTERVALS:
                df, report = _refresh_cached_canonical_window(symbol, category, interval, start, end, limit, market_type)
            else:
                df = _load_cached_range(symbol, interval, category, start=start, end=end, limit=limit, market_type=market_type)
                if not df.empty:
                    report = {"source": "db_cache", "rows": int(len(df))}
                else:
                    df, report = load_or_fetch_candles(symbol, category, interval, start, end, limit=limit, source='bitget', repair=True, strict_backtest=False)
                    _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
        elif source == "toss":
            _require_stock_feature()
            if interval.endswith("s"):
                raise ValueError("Toss stock charts currently support 1m and larger intervals")
            if market_type == CRYPTO:
                raise ValueError("source=toss requires KOR_STOCK or US_STOCK")
            df, report = _refresh_cached_canonical_window(symbol, category, interval, start, end, limit, market_type)
        elif source == "demo":
            df = make_demo_candles(symbol, category, interval)
            df["market_type"] = market_type
            _maybe_save_csv(df, _csv_path(symbol, interval, market_type))
            try: upsert_candles(df, market_type=market_type)
            except Exception: pass
            report = None
        else:
            df = _ensure_data(symbol, category, interval, limit=limit, market_type=market_type)
            report = None
        return {
            "ok": True,
            "stale": False,
            "symbol": symbol.upper(),
            "category": category,
            "market_type": market_type,
            "interval": interval,
            "rows": int(len(df)),
            "last_timestamp": int(df["timestamp"].iloc[-1]) if not df.empty else None,
            "candles": _candles_json(df),
            "indicators": _indicator_payload(df) if include_indicators else {"overlay": {}, "lower": {}},
            "report": report.__dict__ if hasattr(report, "__dict__") else (report or {}),
        }
    except Exception as e:
        cached = _load_local(symbol, interval, category, limit=limit, market_type=market_type)
        if not cached.empty:
            return {
                "ok": True,
                "stale": True,
                "error": str(e),
                "symbol": symbol.upper(),
                "category": category,
                "market_type": market_type,
                "interval": interval,
                "rows": int(len(cached)),
                "last_timestamp": int(cached["timestamp"].iloc[-1]) if not cached.empty else None,
                "candles": _candles_json(cached),
                "indicators": _indicator_payload(cached),
                "report": {"fallback": "cache"},
            }
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/indicators")
def api_indicators_snapshot(
    symbol: str = Query("BTCUSDT"),
    category: str = Query("USDT-FUTURES"),
    interval: str = Query("1m"),
    market_type: str = Query(CRYPTO),
    limit: int = Query(500, ge=50, le=6000),
    enabled: str = Query(""),
):
    """Calculate indicators from the server-side bounded SQLite tail.

    Live browsers no longer send the complete 6,000-candle chart back to the
    server on every update. The POST endpoint below remains as a compatibility
    fallback for browser-only historical data.
    """
    try:
        category = _normalize_category(category)
        market_type = normalize_market_type(market_type, category)
        if market_type != CRYPTO and not ENABLE_STOCK_MARKETS:
            raise RuntimeError("Stock markets are disabled on this server.")
        df = _load_cached_range(symbol, interval, category, limit=limit, market_type=market_type)
        if df.empty:
            df = _load_local(symbol, interval, category, limit=limit, market_type=market_type)
        if df.empty:
            raise ValueError("no server-side candle data is available")
        keys = {item.strip() for item in str(enabled or "").split(",") if item.strip()}
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "category": category,
            "market_type": market_type,
            "interval": interval,
            "rows": int(len(df)),
            "last_timestamp": int(df["timestamp"].iloc[-1]),
            "source": "sqlite",
            "indicators": _indicator_payload(df.tail(limit), keys or None, market_type),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/indicators")
def api_indicators(payload: dict[str, Any] = Body(...)):
    """Recompute indicators from the exact candle set currently shown on the chart.

    This keeps overlay indicators, volume, and lower panes tied to the same
    interval/range after lazy-loading older candles or receiving live updates.
    """
    try:
        symbol = str(payload.get("symbol") or "BTCUSDT").upper()
        category = _normalize_category(payload.get("category"))
        market_type = normalize_market_type(payload.get("market_type"), category)
        interval = str(payload.get("interval") or "1m")
        candles = payload.get("candles") or []
        enabled = {str(key) for key in (payload.get("enabled") or [])}
        df = _df_from_chart_candles(candles[-6000:], symbol=symbol, category=category, interval=interval, market_type=market_type)
        return {"ok": True, "symbol": symbol, "category": category, "market_type": market_type, "interval": interval, "rows": int(len(df)), "indicators": _indicator_payload(df, enabled or None)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/stocks/universe/status")
def api_stock_universe_status():
    """Return local catalog counts without contacting Toss."""
    _require_stock_feature()
    return {"ok": True, **stock_universe_status()}


@app.get("/api/stocks/search")
def api_stock_search(
    q: str = Query("", max_length=80),
    market_type: str = Query(KOR_STOCK),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        _require_stock_feature()
        market_type = normalize_market_type(market_type)
        if not _is_stock_market(market_type):
            raise ValueError("Stock search requires KOR_STOCK or US_STOCK")
        query = str(q or "").strip()
        return {
            "ok": True,
            "market_type": market_type,
            "query": query,
            "results": search_stock_symbols(query, market_type, limit=limit),
            "direct_entry": query.upper() if _is_direct_stock_symbol(query, market_type) else None,
            "universe": stock_universe_status(),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/stocks/select")
def api_stock_select(payload: dict[str, Any] = Body(...)):
    """Select a ticker and enrich its local catalog entry from Toss when possible."""
    try:
        _require_stock_feature()
        market_type = normalize_market_type(payload.get("market_type"))
        if not _is_stock_market(market_type):
            raise ValueError("Stock selection requires KOR_STOCK or US_STOCK")
        symbol = str(payload.get("symbol") or "").upper().strip()
        if not _is_direct_stock_symbol(symbol, market_type):
            raise ValueError("Invalid stock symbol format")
        exchange = str(payload.get("exchange") or "").upper().strip() or None
        stock = ensure_stock_symbol(
            symbol,
            market_type,
            exchange=exchange,
            name=str(payload.get("name") or "").strip() or None,
        )
        # The official Toss API supports metadata lookup for up to 200 known
        # tickers, not an all-market listing. Enrich direct selections here
        # without downloading a whole universe during chart loading.
        try:
            provider_rows = TossPublicClient().get_stock_info([symbol], market_type)
            if provider_rows:
                upsert_stock_symbols(provider_rows, source="toss_stock_info")
                stock = provider_rows[0]
        except Exception:
            # Keep a direct ticker usable when the provider is temporarily
            # unavailable. Chart loading will expose the specific API error.
            pass
        return {
            "ok": True,
            "stock": stock,
            "symbol": stock["symbol"],
            "category": _stock_candle_category(market_type, stock.get("exchange")),
            "market_type": market_type,
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/stocks/universe/refresh")
def api_stock_universe_refresh(payload: dict[str, Any] = Body(default={})):
    """Explicitly import a full symbol catalog after Toss contract setup.

    Chart opening never calls this route. That avoids an expensive complete
    universe API request each time the UI starts.
    """
    try:
        _require_stock_feature()
        market_type = normalize_market_type(payload.get("market_type") or KOR_STOCK)
        if not _is_stock_market(market_type):
            raise ValueError("Stock universe refresh requires KOR_STOCK or US_STOCK")
        rows = TossPublicClient().download_stock_universe(market_type)
        inserted = upsert_stock_symbols(rows, source="toss_universe")
        return {"ok": True, "market_type": market_type, "inserted": inserted, "universe": stock_universe_status()}
    except TossApiConfigurationError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "hint": "The official Toss API supports lookup for known tickers but does not publish a full-universe endpoint. Use ticker search or import a separately licensed catalog.",
            },
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/stocks/surge-rankings")
def api_surge_rankings(
    market_type: str = Query(KOR_STOCK),
    limit: int = Query(100, ge=1, le=100),
):
    """Read the persisted local-universe price/volume watchlist without network I/O."""
    try:
        _require_stock_feature()
        _require_surge_feature()
        market = normalize_market_type(market_type)
        if not _is_stock_market(market):
            raise ValueError("Surge rankings require KOR_STOCK or US_STOCK")
        return {"ok": True, **SurgeScanner().snapshot(market, top_n=limit)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/stocks/surge-rankings/refresh")
def api_refresh_surge_rankings(payload: dict[str, Any] = Body(default={})):
    """Refresh one bounded candidate batch, then persist its latest ranking.

    This route is deliberately separate from chart loading.  The browser calls
    it only while a stock tab is active, and the scanner rotates through a
    local catalog instead of pretending that Toss exposes a global Top 100.
    """
    try:
        _require_stock_feature()
        _require_surge_feature()
        market = normalize_market_type(payload.get("market_type") or KOR_STOCK)
        if not _is_stock_market(market):
            raise ValueError("Surge rankings require KOR_STOCK or US_STOCK")
        max_symbols = max(1, min(int(payload.get("max_symbols", 25)), MAX_BATCH_SIZE))
        top_n = max(1, min(int(payload.get("top_n", 100)), 100))
        result = SurgeScanner().refresh(market, max_symbols=max_symbols, top_n=top_n)
        return {"ok": True, **result.payload(top_n=top_n)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/latest-candles")
def api_latest_candles(
    symbol: str = Query("BTCUSDT"),
    category: str = Query("USDT-FUTURES"),
    interval: str = Query("1m"),
    market_type: str = Query(CRYPTO),
):
    category = _normalize_category(category)
    market_type = normalize_market_type(market_type, category)
    limit = 220 if interval == "2m" else 200 if interval in ("1m", "3m", "5m", "15m", "30m") else 160
    try:
        if market_type == CRYPTO:
            data = BitgetPublicClient().get_recent_candles(symbol, category, interval, limit=limit)
        else:
            data = TossPublicClient().get_recent_candles(symbol, category, interval, market_type=market_type, limit=limit)
        if not data.empty:
            try:
                upsert_candles(data, market_type=market_type)
            except Exception:
                pass
            _maybe_save_csv(data, _csv_path(symbol, interval, market_type))
        try:
            tail = load_candles(symbol, category, interval, limit=1800, market_type=market_type)
        except Exception:
            tail = pd.DataFrame()
        if tail.empty:
            tail = data
        return {"ok": True, "stale": False, "symbol": symbol.upper(), "category": category, "market_type": market_type, "interval": interval, "rows": int(len(tail)), "candles": _candles_json(tail), "indicators": _indicator_payload(tail)}
    except Exception as e:
        cached = _load_local(symbol, interval, category, limit=1800, market_type=market_type)
        if not cached.empty:
            return {"ok": True, "stale": True, "error": str(e), "symbol": symbol.upper(), "category": category, "market_type": market_type, "interval": interval, "rows": int(len(cached)), "candles": _candles_json(cached), "indicators": _indicator_payload(cached)}
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/backtest")
def api_backtest(payload: dict[str, Any] = Body(...)):
    try:
        return _run_backtest_payload(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/settings/status")
def api_settings_status():
    """Return non-secret configuration status for the dashboard settings panel."""
    private_client = BitgetPrivateClient()
    return {
        "ok": True,
        "mode_flags": {
            "paper": True,
            "demo": ENABLE_DEMO_TRADING,
            "live": ENABLE_LIVE_TRADING,
        },
        "credentials_configured": bool(private_client.has_credentials()),
        "api_base_url": os.getenv("BITGET_API_BASE_URL", "https://api.bitget.com"),
        "risk": _read_ui_settings()["risk"],
        "storage": {
            "settings": "results/ui_settings.json",
            "database": bool(os.getenv("DATABASE_URL") or os.getenv("DB_PATH")),
        },
    }


@app.get("/api/settings/risk")
def api_get_risk_settings():
    return {"ok": True, "risk": _read_ui_settings()["risk"]}


@app.post("/api/settings/risk")
def api_save_risk_settings(payload: dict[str, Any] = Body(...)):
    try:
        risk = _validate_risk_settings(payload)
        _write_ui_settings(risk)
        return {"ok": True, "risk": risk}
    except (OSError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/account/snapshot")
async def api_account_snapshot(
    mode: str = Query("PAPER"),
    symbol: str = Query("BTCUSDT"),
    product_type: str = Query("USDT-FUTURES"),
    market_type: str = Query(CRYPTO),
    limit: int = Query(50, ge=1, le=100),
):
    """Return a normalized, read-only account snapshot for the dashboard.

    Paper mode deliberately reports a not-started state until a paper session
    exists. Demo/live account reads never place orders and remain unavailable
    when the corresponding feature flag or credentials are absent.
    """
    normalized_mode = str(mode or "PAPER").upper().strip()
    market_type = normalize_market_type(market_type, product_type)
    symbol = str(symbol or "BTCUSDT").upper().strip()
    product_type = _normalize_category(product_type)
    risk_settings = _read_ui_settings()["risk"]
    mode_available = {
        "PAPER": True,
        "DEMO": ENABLE_DEMO_TRADING,
        "LIVE": ENABLE_LIVE_TRADING,
    }
    if normalized_mode not in mode_available:
        return JSONResponse({"ok": False, "error": "mode must be PAPER, DEMO, or LIVE"}, status_code=400)
    if market_type != CRYPTO:
        return {
            "ok": True,
            "mode": normalized_mode,
            "market_type": market_type,
            "status": "market_not_connected",
            "mode_available": mode_available,
            "account": {"status": "market_not_connected", "equity": None, "available": None, "used_margin": None, "unrealized_pnl": None},
            "positions": [],
            "fills": [],
            "risk": build_risk_snapshot({"status": "market_not_connected"}, [], [], risk_settings),
            "notifications": [{"level": "info", "code": "market_account_pending", "message": "이 시장의 계좌 연결은 별도 인증이 필요합니다."}],
        }
    if normalized_mode == "PAPER":
        account = {"status": "paper_not_started", "equity": None, "available": None, "used_margin": None, "unrealized_pnl": None, "margin_coin": "USDT"}
        return {
            "ok": True,
            "mode": normalized_mode,
            "market_type": market_type,
            "symbol": symbol,
            "product_type": product_type,
            "status": "paper_not_started",
            "credentials_configured": False,
            "mode_available": mode_available,
            "account": account,
            "positions": [],
            "fills": [],
            "risk": build_risk_snapshot(account, [], [], risk_settings),
            "notifications": [{"level": "info", "code": "paper_not_started", "message": "페이퍼 거래 세션이 아직 시작되지 않았습니다."}],
            "last_synced_at": now_ms(),
        }
    if not mode_available[normalized_mode]:
        return {
            "ok": True,
            "mode": normalized_mode,
            "market_type": market_type,
            "symbol": symbol,
            "product_type": product_type,
            "status": "mode_disabled",
            "credentials_configured": False,
            "mode_available": mode_available,
            "account": {"status": "mode_disabled", "equity": None, "available": None, "used_margin": None, "unrealized_pnl": None},
            "positions": [],
            "fills": [],
            "risk": build_risk_snapshot({"status": "mode_disabled"}, [], [], risk_settings),
            "notifications": [{"level": "warning", "code": "mode_disabled", "message": f"{normalized_mode} 모드는 서버 설정에서 비활성화되어 있습니다."}],
            "last_synced_at": now_ms(),
        }
    client = BitgetPrivateClient()
    if not client.has_credentials():
        account = {"status": "credentials_missing", "equity": None, "available": None, "used_margin": None, "unrealized_pnl": None}
        return {
            "ok": True,
            "mode": normalized_mode,
            "market_type": market_type,
            "symbol": symbol,
            "product_type": product_type,
            "status": "credentials_missing",
            "credentials_configured": False,
            "mode_available": mode_available,
            "account": account,
            "positions": [],
            "fills": [],
            "risk": build_risk_snapshot(account, [], [], risk_settings),
            "notifications": [{"level": "warning", "code": "credentials_missing", "message": "Bitget API 키가 설정되지 않았습니다."}],
            "last_synced_at": now_ms(),
        }
    error: str | None = None
    account_payload: Any = {}
    positions_payload: Any = {}
    fills_payload: Any = {}
    try:
        account_payload = await asyncio.to_thread(client.get_account, symbol, product_type, "USDT")
        positions_payload = await asyncio.to_thread(client.get_positions, product_type, "USDT")
        try:
            fills_payload = await asyncio.to_thread(client.get_fill_history, product_type, symbol, None, None, limit)
        except Exception:
            # Account and position data remain useful when fill-history access
            # is restricted by the current account or API permission scope.
            fills_payload = {}
    except Exception as exc:
        error = str(exc)
    account = normalize_account(account_payload)
    positions = normalize_positions(positions_payload, symbol=symbol)
    fills = normalize_fills(fills_payload, symbol=symbol, limit=limit)
    if error:
        account["status"] = "error"
    return {
        "ok": True,
        "mode": normalized_mode,
        "market_type": market_type,
        "symbol": symbol,
        "product_type": product_type,
        "status": "error" if error else account["status"],
        "error": error,
        "credentials_configured": True,
        "mode_available": mode_available,
        "account": account,
        "positions": positions,
        "fills": fills,
        "risk": build_risk_snapshot(account, positions, fills, risk_settings),
        "notifications": build_notifications(account=account, positions=positions, fills=fills, error=error),
        "last_synced_at": now_ms(),
    }


@app.get("/api/market/state")
def api_market_state():
    return {"ok": True, **MARKET_STATE.snapshot()}


@app.post("/api/market/switch")
async def api_market_switch(payload: dict[str, Any] = Body(...)):
    try:
        market_type = normalize_market_type(payload.get("market_type"))
        if market_type != CRYPTO:
            _require_stock_feature()
        return {"ok": True, **(await MARKET_STATE.switch_active(market_type))}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/market/positions")
async def api_market_positions(payload: dict[str, Any] = Body(...)):
    """Accept a broker/position-sync count without starting heavy market data."""
    try:
        market_type = normalize_market_type(payload.get("market_type"))
        return {"ok": True, **(await MARKET_STATE.update_known_position_count(market_type, int(payload.get("count") or 0)))}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def _ws_live_toss(
    websocket: WebSocket,
    *,
    symbol: str,
    category: str,
    interval: str,
    market_type: str,
    client_last_time: Any,
) -> None:
    """Serve Toss stock live candles using the official REST-only API."""
    if interval.endswith("s"):
        await websocket.send_json({"type": "fatal", "message": "Stock live charts support 1m and larger intervals"})
        return
    cached, _, _ = _cached_window(symbol, interval, category, limit=1800, market_type=market_type)
    catchup = pd.DataFrame()
    warning = ""
    try:
        catchup, _ = _repair_live_gap(symbol, category, interval, client_last_time, market_type)
    except Exception as exc:
        warning = str(exc)
    initial = cached
    client = TossPublicClient()
    try:
        recent = await asyncio.to_thread(
            client.get_recent_candles,
            symbol,
            category,
            "1m",
            market_type=market_type,
            limit=200,
        )
        if not recent.empty:
            upsert_candles(recent, market_type=market_type, source="toss_rest")
            initial, _, _ = _cached_window(symbol, interval, category, limit=1800, market_type=market_type)
    except Exception as exc:
        warning = warning or str(exc)
    if not catchup.empty:
        await websocket.send_json({"type": "catchup", "transport": "db_gap_repair", "market_type": market_type, "symbol": symbol, "category": category, "interval": interval, "rows": int(len(catchup)), "candles": _candles_json(catchup)})
    await websocket.send_json({
        "type": "snapshot",
        "transport": "toss_rest_poll",
        "market_type": market_type,
        "symbol": symbol,
        "category": category,
        "interval": interval,
        "rows": int(len(initial)),
        "candles": _candles_json(initial),
        "indicators": {"overlay": {}, "lower": {}},
        "warning": warning or None,
    })

    try:
        # Toss currently exposes REST rather than a market-data WebSocket.
        # Two seconds stays well below the 5 TPS candle limit while keeping a
        # forming one-minute bar responsive enough for the live chart.
        poll_seconds = max(2.0, float(os.getenv("TOSS_LIVE_POLL_SECONDS", "2")))
        last_snapshot: tuple[Any, ...] | None = None
        while True:
            latest_1m = await asyncio.to_thread(
                client.get_recent_candles,
                symbol,
                category,
                "1m",
                market_type=market_type,
                limit=2,
            )
            if not latest_1m.empty:
                upsert_candles(latest_1m, market_type=market_type, source="toss_rest_live")
                span = max(INTERVAL_MS.get(interval, INTERVAL_MS["1D"]) * 2, 2 * INTERVAL_MS["1m"])
                latest, _ = load_or_fetch_canonical_candles(
                    symbol,
                    category,
                    interval,
                    now_ms() - span,
                    now_ms(),
                    fetch=False,
                    persist_derived=True,
                    market_type=market_type,
                )
                if latest.empty:
                    await asyncio.sleep(poll_seconds)
                    continue
                row = latest.iloc[-1]
                snapshot = tuple(row[key] for key in ("timestamp", "open", "high", "low", "close", "volume"))
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    await websocket.send_json({
                        "type": "candle_update",
                        "transport": "toss_rest_poll",
                        "market_type": market_type,
                        "candle": _candles_json(latest.tail(1))[0],
                    })
            await asyncio.sleep(poll_seconds)
    except TossApiConfigurationError as exc:
        await websocket.send_json({"type": "status", "transport": "toss_configuration_required", "market_type": market_type, "message": str(exc)})
    except asyncio.CancelledError:
        raise
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await websocket.send_json({"type": "fatal", "message": str(exc)})


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    params = websocket.query_params
    symbol = str(params.get("symbol") or "BTCUSDT").upper()
    category = _normalize_category(params.get("category"))
    interval = str(params.get("interval") or "1m")
    market_type = normalize_market_type(params.get("market_type"), category)
    if market_type != CRYPTO and not ENABLE_STOCK_MARKETS:
        await websocket.send_json({"type": "fatal", "message": "Stock markets are disabled on this server."})
        await websocket.close()
        return
    await MARKET_STATE.switch_active(market_type)
    current_task = asyncio.current_task()
    if not await MARKET_STATE.attach_heavy_task(market_type, current_task):
        return
    if market_type != CRYPTO:
        try:
            await _ws_live_toss(
                websocket,
                symbol=symbol,
                category=category,
                interval=interval,
                market_type=market_type,
                client_last_time=params.get("from"),
            )
        finally:
            await MARKET_STATE.detach_heavy_task(market_type, current_task)
        return
    cache_path = str(_csv_path(symbol, interval, market_type))
    tick_path = str(RAW / f"{symbol.upper()}_ticks.csv")
    engine = str(params.get("engine") or "auto").lower()
    catchup = pd.DataFrame()
    catchup_report: dict[str, Any] = {"source": "live_no_cursor", "rows": 0}
    catchup_warning = ""
    try:
        catchup, catchup_report = _repair_live_gap(symbol, category, interval, params.get("from"), market_type)
    except Exception as exc:
        # The existing chart is still valid; a Kline stream can continue even
        # when a temporary REST repair is unavailable.
        catchup_warning = str(exc)
    if interval not in SECOND_INTERVALS and interval != "2m":
        # Native Kline updates are exchange-authored and therefore remain
        # timestamp-identical to the Bitget chart. Trade ticks are retained
        # only for second candles, which Bitget does not publish historically.
        cached, _, _ = _cached_window(symbol, interval, category, limit=1800, market_type=market_type)
        stream = LiveCandleStream(StreamConfig(symbol=symbol, category=category, interval=interval, cache_path=cache_path, initial_limit=1200, max_rows=6000))
        try:
            initial = stream.initial(cached=cached)
        except Exception:
            initial = cached
    elif interval == "2m":
        cached, _, _ = _cached_window(symbol, interval, category, limit=1800, market_type=market_type)
        stream = LiveSyntheticCandleStream(StreamConfig(symbol=symbol, category=category, interval=interval, cache_path=cache_path, initial_limit=1200, max_rows=6000))
        try:
            initial = stream.initial(cached=cached)
        except Exception:
            initial = cached
    else:
        cached = _load_local(symbol, interval, category, limit=1800, market_type=market_type).tail(1800)
        stream = LiveTickCandleStream(TickStreamConfig(symbol=symbol, category=category, interval=interval, cache_path=cache_path, tick_path=tick_path, initial_limit=1200))
        initial = stream.initial(cached=cached)
    try:
        if not catchup.empty:
            await websocket.send_json({"type": "catchup", "transport": "db_gap_repair", "symbol": symbol, "category": category, "interval": interval, "rows": int(len(catchup)), "candles": _candles_json(catchup), "report": catchup_report})
        elif catchup_warning:
            await websocket.send_json({"type": "status", "transport": "gap_repair_failed", "message": catchup_warning, "rows": 0})
        await websocket.send_json({"type": "snapshot", "engine": engine, "transport": "rest_or_cache", "symbol": symbol.upper(), "category": category, "interval": interval, "rows": int(len(initial)), "candles": _candles_json(initial), "indicators": {"overlay": {}, "lower": {}}})
    except Exception as e:
        cached = _load_local(symbol, interval, category, limit=1800, market_type=market_type).tail(1800)
        await websocket.send_json({"type": "snapshot", "transport": "cache", "symbol": symbol.upper(), "category": category, "interval": interval, "rows": int(len(cached)), "candles": _candles_json(cached), "indicators": {"overlay": {}, "lower": {}}, "warning": str(e)})

    try:
        async for event in stream.stream():
            await websocket.send_json(event)
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await websocket.send_json({"type": "fatal", "message": str(e)})
        except Exception:
            return
    finally:
        await MARKET_STATE.detach_heavy_task(market_type, current_task)


HTML = (Path(__file__).resolve().parent / "templates" / "index.html").read_text(encoding="utf-8")
