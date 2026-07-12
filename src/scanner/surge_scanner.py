"""Cache-first price/volume ranking for a local stock universe.

The score is a watchlist priority, not an entry signal or a profitability
forecast.  It deliberately uses only the most recently completed/current
candle and its preceding rows.  A complete Korean or US Top 100 requires a
complete licensed symbol catalog to be imported into ``stock_symbols`` first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from src.data_engine.storage import (
    DEFAULT_DB,
    list_stock_symbols,
    load_candles,
    load_surge_rankings,
    load_surge_scanner_state,
    save_surge_rankings,
    save_surge_scanner_state,
    upsert_candles,
)
from src.indicators.library import surge_snapshot
from src.markets import KOR_STOCK, US_STOCK, is_regular_session_timestamp, market_local_datetime, market_spec, normalize_market_type
from src.toss.public_client import TossPublicClient
from src.utils.time import now_ms


DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 50
LOOKBACK_BARS = 80


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def score_surge_snapshot(snapshot: dict[str, Any]) -> float | None:
    """Return a deterministic, non-predictive attention score from past/current bars."""
    if not snapshot or not snapshot.get("ready"):
        return None
    relative_volume = _number(snapshot.get("relative_volume20"))
    volume_zscore = _number(snapshot.get("volume_zscore20"))
    move_1 = _number(snapshot.get("price_change_1_pct"))
    move_5 = _number(snapshot.get("price_change_5_pct"))
    move_15 = _number(snapshot.get("price_change_15_pct"))
    breakout = _number(snapshot.get("breakout_20_pct"))
    vwap_deviation = _number(snapshot.get("vwap_deviation_pct"))
    # A positive-only rank keeps this an "unusual upward activity" watchlist.
    # It is intentionally not a signal threshold and is never sent to a broker.
    return round(
        max(0.0, move_1) * 0.60
        + max(0.0, move_5) * 0.90
        + max(0.0, move_15) * 0.45
        + max(0.0, relative_volume - 1.0) * 3.00
        + max(0.0, volume_zscore) * 0.75
        + max(0.0, breakout) * 1.50
        + max(0.0, vwap_deviation) * 0.25,
        6,
    )


def _regular_session_rows(frame: pd.DataFrame, market_type: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out["timestamp"] = pd.to_numeric(out.get("timestamp"), errors="coerce")
    out = out.dropna(subset=["timestamp"])
    out["timestamp"] = out["timestamp"].astype("int64")
    return out[out["timestamp"].map(lambda value: is_regular_session_timestamp(int(value), market_type))].copy()


def _candle_category(market_type: str, exchange: str) -> str:
    """Use the same stable candle key that the chart/backtest data engine uses."""
    return "KRX" if market_type == KOR_STOCK else str(exchange or "NASDAQ").upper()


def _last_expected_session_window(timestamp_ms: int, market_type: str, bars: int) -> tuple[int, int]:
    """Return the latest weekday regular-session tail for one bounded repair."""
    spec = market_spec(market_type)
    if spec.session_open is None or spec.session_close is None:
        raise ValueError("Stock market session configuration is missing")
    local = market_local_datetime(timestamp_ms, market_type)
    day = local.date()
    local_time = local.timetz().replace(tzinfo=None)
    if local.weekday() >= 5 or local_time < spec.session_open:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    session_start = datetime.combine(day, spec.session_open, tzinfo=spec.timezone)
    session_end = datetime.combine(day, spec.session_close, tzinfo=spec.timezone)
    tail_start = max(session_start, session_end - timedelta(minutes=max(25, int(bars))))
    return (
        int(tail_start.astimezone(timezone.utc).timestamp() * 1000),
        int(session_end.astimezone(timezone.utc).timestamp() * 1000),
    )


@dataclass
class SurgeScanResult:
    market_type: str
    rankings: list[dict[str, Any]]
    catalog_total: int
    scanned_this_run: int
    cycle_scanned: int
    cycle_complete: bool
    next_cursor: int
    errors: list[str]
    last_run_at: int

    def payload(self, *, top_n: int) -> dict[str, Any]:
        return {
            "market_type": self.market_type,
            "scope": "local_catalog",
            "is_global_universe": False,
            "top_n": int(top_n),
            "catalog_total": self.catalog_total,
            "scanned_this_run": self.scanned_this_run,
            "cycle_scanned": self.cycle_scanned,
            "cycle_complete": self.cycle_complete,
            "next_cursor": self.next_cursor,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "rankings": self.rankings[:top_n],
        }


class SurgeScanner:
    """Rotate through a local universe while preserving prior cached rankings."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB,
        client: TossPublicClient | None = None,
        lookback_bars: int = LOOKBACK_BARS,
    ):
        self.db_path = db_path
        self.client = client or TossPublicClient()
        self.lookback_bars = max(25, min(int(lookback_bars), 200))

    @staticmethod
    def _validate_market(market_type: str) -> str:
        market = normalize_market_type(market_type)
        if market not in {KOR_STOCK, US_STOCK}:
            raise ValueError("Surge scanner requires KOR_STOCK or US_STOCK")
        return market

    def snapshot(self, market_type: str, *, top_n: int = 100) -> dict[str, Any]:
        market = self._validate_market(market_type)
        state = load_surge_scanner_state(market, self.db_path)
        catalog_total = len(list_stock_symbols(market, tradeable_only=True, db_path=self.db_path))
        rankings = load_surge_rankings(market, limit=top_n, db_path=self.db_path)
        return {
            "market_type": market,
            "scope": "local_catalog",
            "is_global_universe": False,
            "top_n": int(top_n),
            "catalog_total": catalog_total,
            "scanned_this_run": state["last_scan_count"],
            "cycle_scanned": state["cycle_scanned"],
            "cycle_complete": bool(catalog_total and state["cycle_scanned"] >= catalog_total),
            "next_cursor": state["cursor"],
            "errors": [state["last_error"]] if state["last_error"] else [],
            "last_run_at": state["last_run_at"],
            "rankings": rankings,
        }

    def _candidate_frame(self, candidate: dict[str, Any], market_type: str, *, fetch_current: bool) -> pd.DataFrame:
        symbol = str(candidate["symbol"]).upper()
        exchange = str(candidate["exchange"]).upper()
        category = _candle_category(market_type, exchange)
        cached = _regular_session_rows(load_candles(
            symbol,
            category,
            "1m",
            limit=self.lookback_bars,
            db_path=self.db_path,
            market_type=market_type,
        ), market_type)
        frames = [cached] if cached is not None and not cached.empty else []
        current = now_ms()
        session_start, session_end = _last_expected_session_window(current, market_type, self.lookback_bars)
        cached_last = int(cached["timestamp"].max()) if cached is not None and not cached.empty else None
        needs_session_repair = cached_last is None or cached_last < session_start
        # Toss is REST-only.  During regular hours fetch only the rolling
        # provider tail; database rows remain the source for older bars.  When
        # first run after close, repair only the final regular-session window
        # so the initial scan can still populate without a whole-history pull.
        if fetch_current and (is_regular_session_timestamp(current, market_type) or needs_session_repair):
            if needs_session_repair:
                latest = self.client.download_candles(
                    symbol,
                    category,
                    "1m",
                    session_start,
                    session_end,
                    market_type=market_type,
                )
            else:
                latest = self.client.get_recent_candles(
                    symbol,
                    category,
                    "1m",
                    market_type=market_type,
                    limit=200,
                )
            latest = _regular_session_rows(latest, market_type)
            if not latest.empty:
                upsert_candles(latest, self.db_path, source="toss_surge_scan", market_type=market_type)
                frames.append(latest)
        if not frames:
            return pd.DataFrame()
        return (
            _regular_session_rows(pd.concat(frames, ignore_index=True), market_type)
            .drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .tail(self.lookback_bars)
            .reset_index(drop=True)
        )

    def refresh(
        self,
        market_type: str,
        *,
        max_symbols: int = DEFAULT_BATCH_SIZE,
        top_n: int = 100,
        fetch_current: bool = True,
    ) -> SurgeScanResult:
        market = self._validate_market(market_type)
        top_n = max(1, min(int(top_n), 100))
        batch_size = max(1, min(int(max_symbols), MAX_BATCH_SIZE))
        candidates = list_stock_symbols(market, tradeable_only=True, db_path=self.db_path)
        total = len(candidates)
        current = now_ms()
        state = load_surge_scanner_state(market, self.db_path)
        if not total:
            empty_state = {
                "cursor": 0,
                "cycle_started_at": None,
                "cycle_scanned": 0,
                "catalog_total": 0,
                "last_run_at": current,
                "last_scan_count": 0,
                "last_error": "No verified tradeable symbols exist in the local catalog.",
            }
            save_surge_scanner_state(market, empty_state, self.db_path)
            return SurgeScanResult(market, [], 0, 0, 0, False, 0, [empty_state["last_error"]], current)

        cursor = int(state.get("cursor", 0) or 0)
        reset_cycle = cursor >= total or state.get("catalog_total") != total or state.get("cycle_scanned", 0) >= total
        if reset_cycle:
            cursor = 0
            cycle_scanned = 0
            cycle_started_at = current
        else:
            cycle_scanned = min(int(state.get("cycle_scanned", 0) or 0), total)
            cycle_started_at = state.get("cycle_started_at") or current
        batch = candidates[cursor:cursor + batch_size]
        next_cursor = cursor + len(batch)
        cycle_complete = next_cursor >= total
        if cycle_complete:
            next_cursor = 0
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for candidate in batch:
            symbol = str(candidate["symbol"]).upper()
            try:
                frame = self._candidate_frame(candidate, market, fetch_current=fetch_current)
                metrics = surge_snapshot(frame, market)
                score = score_surge_snapshot(metrics)
                if score is None:
                    errors.append(f"{symbol}: requires at least 20 regular-session 1m candles")
                    continue
                records.append({
                    "symbol": symbol,
                    "exchange": str(candidate["exchange"]).upper(),
                    "score": score,
                    "snapshot_at": int(frame["timestamp"].iloc[-1]),
                    "metrics": metrics,
                    "source": "local_catalog",
                })
            except Exception as exc:
                errors.append(f"{symbol}: {str(exc)[:180]}")
        save_surge_rankings(records, market, self.db_path)
        cycle_scanned = min(total, cycle_scanned + len(batch))
        save_surge_scanner_state(
            market,
            {
                "cursor": next_cursor,
                "cycle_started_at": cycle_started_at,
                "cycle_scanned": cycle_scanned,
                "catalog_total": total,
                "last_run_at": current,
                "last_scan_count": len(batch),
                "last_error": "; ".join(errors[:3]),
            },
            self.db_path,
        )
        rankings = load_surge_rankings(market, limit=top_n, db_path=self.db_path)
        return SurgeScanResult(
            market,
            rankings,
            total,
            len(batch),
            cycle_scanned,
            cycle_complete,
            next_cursor,
            errors,
            current,
        )
