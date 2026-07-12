from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.data_engine.canonical import find_market_gaps
from src.data_engine.validator import DataValidationError, find_gaps
from src.markets import CRYPTO, is_regular_session_timestamp, normalize_market_type


REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
FINGERPRINT_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "turnover")


@dataclass(frozen=True)
class BacktestDataset:
    """Validated immutable input snapshot for one backtest run."""

    candles: pd.DataFrame
    metadata: dict[str, Any]


def build_backtest_dataset(
    candles: pd.DataFrame,
    *,
    symbol: str,
    category: str,
    interval: str,
    market_type: str = CRYPTO,
) -> BacktestDataset:
    """Validate and fingerprint the candle set independently of the chart.

    A strategy receives a growing prefix of this frozen snapshot. Chart range,
    indicator panes, and browser state are never an input to the backtest.
    """
    market = normalize_market_type(market_type, category)
    if candles is None or candles.empty:
        raise DataValidationError("empty candle data")
    missing = [column for column in REQUIRED_COLUMNS if column not in candles.columns]
    if missing:
        raise DataValidationError(f"missing candle columns: {missing}")

    work = candles.copy(deep=True)
    for column in REQUIRED_COLUMNS + ("turnover",):
        if column in work.columns:
            work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=list(REQUIRED_COLUMNS)).sort_values("timestamp").reset_index(drop=True)
    if len(work) < 3:
        raise DataValidationError("Backtest needs at least 3 validated candle rows")
    work["timestamp"] = work["timestamp"].astype("int64")
    if work["timestamp"].duplicated().any():
        raise DataValidationError("duplicated timestamp")
    if (work["high"] < work[["open", "close", "low"]].max(axis=1)).any():
        raise DataValidationError("high is below an OHLC value")
    if (work["low"] > work[["open", "close", "high"]].min(axis=1)).any():
        raise DataValidationError("low is above an OHLC value")
    if (work["volume"] < 0).any():
        raise DataValidationError("negative volume")

    if market != CRYPTO:
        outside_session = ~work["timestamp"].map(lambda ts: is_regular_session_timestamp(int(ts), market))
        if outside_session.any():
            raise DataValidationError("stock dataset contains candles outside the configured regular session")
        gaps = [gap for gap in find_market_gaps(work, interval, market) if gap[2] > 0]
    else:
        gaps = [gap for gap in find_gaps(work, interval) if gap[2] > 0]
    if gaps:
        missing_count = sum(int(gap[2]) for gap in gaps)
        raise DataValidationError(f"missing/nonuniform candles: {len(gaps)} gaps, {missing_count} missing bars")

    for column in ("symbol", "category", "interval", "market_type"):
        if column not in work.columns:
            work[column] = {"symbol": symbol.upper(), "category": category, "interval": interval, "market_type": market}[column]
    fingerprint = work.reindex(columns=[column for column in FINGERPRINT_COLUMNS if column in work.columns]).copy()
    fingerprint.insert(0, "market_type", market)
    fingerprint.insert(1, "symbol", symbol.upper())
    fingerprint.insert(2, "category", category)
    fingerprint.insert(3, "interval", interval)
    digest = hashlib.sha256(pd.util.hash_pandas_object(fingerprint, index=False).values.tobytes()).hexdigest()
    metadata = {
        "dataset_id": digest,
        "symbol": symbol.upper(),
        "category": category,
        "market_type": market,
        "interval": interval,
        "rows": int(len(work)),
        "first_timestamp": int(work["timestamp"].iloc[0]),
        "last_timestamp": int(work["timestamp"].iloc[-1]),
        "validation": "strict_market_aware",
        "execution_model": "signal_on_completed_bar_execute_next_bar_open",
    }
    return BacktestDataset(candles=work, metadata=metadata)
