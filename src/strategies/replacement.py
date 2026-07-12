"""The public strategy set used by the trading UI.

These strategies deliberately use completed OHLCV candles only. They are
candidate systems for backtesting and paper trading, not profitability claims.
"""

from __future__ import annotations

import math

import pandas as pd

from src.indicators.library import atr, ema, macd, rsi, sma, vwap
from src.models import Signal
from src.strategies.base import Strategy


def _prepare(candles: pd.DataFrame, minimum_rows: int) -> pd.DataFrame:
    if candles is None or len(candles) < minimum_rows:
        return pd.DataFrame()
    frame = candles.copy().sort_values("timestamp").reset_index(drop=True)
    for column in ("timestamp", "open", "high", "low", "close", "volume", "turnover"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    return frame.tail(max(minimum_rows * 4, 300)).reset_index(drop=True)


def _last(series: pd.Series, default: float = 0.0) -> float:
    value = series.iloc[-1] if len(series) else default
    return float(value) if pd.notna(value) and math.isfinite(float(value)) else default


def _position(position) -> tuple[int, float]:
    if isinstance(position, dict):
        return int(position.get("target_position") or position.get("pos") or 0), float(position.get("entry_price") or 0)
    return 0, 0.0


def _quote_turnover(frame: pd.DataFrame) -> pd.Series:
    close = frame["close"].fillna(0.0)
    volume = frame["volume"].fillna(0.0)
    turnover = frame["turnover"].fillna(0.0)
    return turnover.where(turnover > 0, close * volume)


def _risk_metadata(
    direction: int,
    close: float,
    atr_value: float,
    swing: float,
    setup: str,
    confidence: float,
    stop_atr: float,
    take_r: float,
    extra: dict | None = None,
) -> dict:
    risk = max(atr_value * stop_atr, close * 0.002)
    stop = min(swing, close - risk) if direction == 1 else max(swing, close + risk)
    if direction == 1 and stop >= close:
        stop = close - risk
    if direction == -1 and stop <= close:
        stop = close + risk
    return {
        **(extra or {}),
        "setup": setup,
        "confidence": float(confidence),
        "initial_stop": float(max(stop, close * 0.0001)),
        "r_management": True,
        "partial_take_r": float(take_r),
        "breakeven_r": 1.0,
        "lock_r": 1.5,
        "lock_profit_r": 0.5,
        "trail_after_r": 2.0,
        "trail_ema": 21,
        "trail_swing_lookback": 8,
    }


class TrendPullbackStrategy(Strategy):
    """EMA trend plus RSI/MACD pullback confirmation with ATR management."""

    name = "trend_pullback"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        allow_short: bool = False,
        fast: int = 21,
        slow: int = 55,
        min_turnover_ratio: float = 0.8,
        stop_atr: float = 1.6,
        take_r: float = 2.0,
    ):
        if fast >= slow or fast < 2:
            raise ValueError("trend pullback fast must be smaller than slow")
        self.symbol = symbol
        self.allow_short = bool(allow_short)
        self.fast = int(fast)
        self.slow = int(slow)
        self.min_turnover_ratio = float(min_turnover_ratio)
        self.stop_atr = float(stop_atr)
        self.take_r = float(take_r)

    def generate(self, candles, portfolio=None, position=None) -> Signal:
        frame = _prepare(candles, self.slow + 20)
        if frame.empty:
            return Signal(self.symbol, 0, "trend_pullback_warmup", 0.0)
        close = frame["close"]
        fast = ema(close, self.fast)
        slow = ema(close, self.slow)
        rsi_series = rsi(close, 14)
        atr_series = atr(frame, 14)
        _, _, macd_hist = macd(close)
        turnover = _quote_turnover(frame)
        turnover_sma = sma(turnover, 20)
        last_close = _last(close)
        atr_value = max(_last(atr_series), last_close * 0.002)
        turnover_ratio = _last(turnover) / max(_last(turnover_sma, _last(turnover)), 1e-12)
        last_rsi = _last(rsi_series, 50.0)
        last_hist = _last(macd_hist)
        trend_up = _last(fast) > _last(slow) and _last(slow.iloc[-4:]) > _last(slow.iloc[-5:-1])
        trend_down = _last(fast) < _last(slow) and _last(slow.iloc[-4:]) < _last(slow.iloc[-5:-1])
        crossed_fast_long = float(close.iloc[-2]) <= _last(fast.iloc[:-1]) and last_close > _last(fast)
        crossed_fast_short = float(close.iloc[-2]) >= _last(fast.iloc[:-1]) and last_close < _last(fast)
        pos, _ = _position(position)
        meta = {
            "close": last_close,
            "ema_fast": _last(fast),
            "ema_slow": _last(slow),
            "rsi14": last_rsi,
            "macd_hist": last_hist,
            "atr14": atr_value,
            "turnover_ratio": float(turnover_ratio),
        }
        if pos == 1:
            if not trend_up or last_close < _last(fast) or last_rsi < 42 or last_hist < 0:
                return Signal(self.symbol, 0, "trend_pullback_long_invalidated", 0.78, meta)
            return Signal(self.symbol, 1, "trend_pullback_hold_long", 0.58, meta)
        if pos == -1:
            if not trend_down or last_close > _last(fast) or last_rsi > 58 or last_hist > 0:
                return Signal(self.symbol, 0, "trend_pullback_short_invalidated", 0.78, meta)
            return Signal(self.symbol, -1, "trend_pullback_hold_short", 0.58, meta)

        long_setup = trend_up and crossed_fast_long and 48 <= last_rsi <= 70 and last_hist > 0 and turnover_ratio >= self.min_turnover_ratio
        if long_setup:
            confidence = min(0.95, 0.62 + (0.08 if turnover_ratio >= 1.0 else 0.0))
            return Signal(self.symbol, 1, "trend_pullback_long", confidence, _risk_metadata(1, last_close, atr_value, float(frame["low"].tail(12).min()), "trend_pullback_long", confidence, self.stop_atr, self.take_r, meta))
        short_setup = self.allow_short and trend_down and crossed_fast_short and 30 <= last_rsi <= 52 and last_hist < 0 and turnover_ratio >= self.min_turnover_ratio
        if short_setup:
            confidence = min(0.95, 0.62 + (0.08 if turnover_ratio >= 1.0 else 0.0))
            return Signal(self.symbol, -1, "trend_pullback_short", confidence, _risk_metadata(-1, last_close, atr_value, float(frame["high"].tail(12).max()), "trend_pullback_short", confidence, self.stop_atr, self.take_r, meta))
        return Signal(self.symbol, 0, "trend_pullback_no_setup", 0.30, meta)


class DonchianAtrBreakoutStrategy(Strategy):
    """Time-series momentum using a confirmed Donchian break and ATR trail."""

    name = "donchian_atr_breakout"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        allow_short: bool = False,
        entry_window: int = 20,
        exit_window: int = 10,
        min_turnover_ratio: float = 1.0,
        stop_atr: float = 2.0,
        take_r: float = 2.5,
    ):
        if entry_window <= exit_window or exit_window < 2:
            raise ValueError("Donchian entry window must be larger than exit window")
        self.symbol = symbol
        self.allow_short = bool(allow_short)
        self.entry_window = int(entry_window)
        self.exit_window = int(exit_window)
        self.min_turnover_ratio = float(min_turnover_ratio)
        self.stop_atr = float(stop_atr)
        self.take_r = float(take_r)

    def generate(self, candles, portfolio=None, position=None) -> Signal:
        frame = _prepare(candles, max(self.entry_window + 20, 60))
        if frame.empty:
            return Signal(self.symbol, 0, "donchian_breakout_warmup", 0.0)
        close = frame["close"]
        previous_high = frame["high"].shift(1).rolling(self.entry_window, min_periods=self.entry_window).max()
        previous_low = frame["low"].shift(1).rolling(self.entry_window, min_periods=self.entry_window).min()
        exit_high = frame["high"].shift(1).rolling(self.exit_window, min_periods=self.exit_window).max()
        exit_low = frame["low"].shift(1).rolling(self.exit_window, min_periods=self.exit_window).min()
        ema50 = ema(close, 50)
        atr_series = atr(frame, 14)
        turnover = _quote_turnover(frame)
        turnover_ratio = _last(turnover) / max(_last(sma(turnover, 20), _last(turnover)), 1e-12)
        last_close = _last(close)
        atr_value = max(_last(atr_series), last_close * 0.002)
        upper = _last(previous_high)
        lower = _last(previous_low)
        pos, _ = _position(position)
        meta = {"close": last_close, "donchian_high": upper, "donchian_low": lower, "atr14": atr_value, "turnover_ratio": float(turnover_ratio)}
        if not math.isfinite(upper) or not math.isfinite(lower):
            return Signal(self.symbol, pos, "donchian_channel_warmup", 0.0, meta)
        if pos == 1:
            if last_close < _last(exit_low) or last_close < _last(ema50):
                return Signal(self.symbol, 0, "donchian_long_exit", 0.80, meta)
            return Signal(self.symbol, 1, "donchian_hold_long", 0.55, meta)
        if pos == -1:
            if last_close > _last(exit_high) or last_close > _last(ema50):
                return Signal(self.symbol, 0, "donchian_short_exit", 0.80, meta)
            return Signal(self.symbol, -1, "donchian_hold_short", 0.55, meta)

        if last_close > upper and turnover_ratio >= self.min_turnover_ratio and last_close >= _last(ema50):
            confidence = min(0.95, 0.66 + (0.10 if turnover_ratio >= 1.2 else 0.0))
            return Signal(self.symbol, 1, "donchian_atr_long_breakout", confidence, _risk_metadata(1, last_close, atr_value, float(frame["low"].tail(self.exit_window).min()), "donchian_long", confidence, self.stop_atr, self.take_r, meta))
        if self.allow_short and last_close < lower and turnover_ratio >= self.min_turnover_ratio and last_close <= _last(ema50):
            confidence = min(0.95, 0.66 + (0.10 if turnover_ratio >= 1.2 else 0.0))
            return Signal(self.symbol, -1, "donchian_atr_short_breakout", confidence, _risk_metadata(-1, last_close, atr_value, float(frame["high"].tail(self.exit_window).max()), "donchian_short", confidence, self.stop_atr, self.take_r, meta))
        return Signal(self.symbol, 0, "donchian_inside_channel", 0.30, meta)


class ChartAiConsensusStrategy(Strategy):
    """An OHLCV-only, explainable ensemble for chart-style decision making.

    This is intentionally not a remote AI model. It combines independent
    trend, momentum, breakout, value, and flow votes so every decision can be
    shown in the UI and reproduced in a backtest without lookahead.
    """

    name = "chart_ai_consensus"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        allow_short: bool = False,
        threshold: float = 0.62,
        min_atr_pct: float = 0.12,
        max_atr_pct: float = 8.0,
        stop_atr: float = 1.8,
        take_r: float = 2.2,
    ):
        self.symbol = symbol
        self.allow_short = bool(allow_short)
        self.threshold = float(threshold)
        self.min_atr_pct = float(min_atr_pct)
        self.max_atr_pct = float(max_atr_pct)
        self.stop_atr = float(stop_atr)
        self.take_r = float(take_r)

    def _votes(self, frame: pd.DataFrame) -> tuple[dict[str, int], dict[str, float]]:
        close = frame["close"]
        ema21 = ema(close, 21)
        ema55 = ema(close, 55)
        rsi14 = rsi(close, 14)
        _, _, macd_hist = macd(close)
        atr_series = atr(frame, 14)
        channel_high = frame["high"].shift(1).rolling(20, min_periods=20).max()
        channel_low = frame["low"].shift(1).rolling(20, min_periods=20).min()
        turnover = _quote_turnover(frame)
        turnover_ratio = _last(turnover) / max(_last(sma(turnover, 20), _last(turnover)), 1e-12)
        vwap_value = _last(vwap(frame.tail(120))) or _last(close)
        last_close = _last(close)
        slow_rising = _last(ema55.iloc[-4:]) > _last(ema55.iloc[-5:-1])
        slow_falling = _last(ema55.iloc[-4:]) < _last(ema55.iloc[-5:-1])
        votes = {
            "trend": 1 if _last(ema21) > _last(ema55) and slow_rising else -1 if _last(ema21) < _last(ema55) and slow_falling else 0,
            "momentum": 1 if _last(macd_hist) > 0 and 50 <= _last(rsi14, 50) <= 74 else -1 if _last(macd_hist) < 0 and 26 <= _last(rsi14, 50) <= 50 else 0,
            "breakout": 1 if last_close > _last(channel_high) else -1 if last_close < _last(channel_low) else 0,
            "value": 1 if last_close > vwap_value and last_close > _last(ema21) else -1 if last_close < vwap_value and last_close < _last(ema21) else 0,
            "flow": 1 if turnover_ratio >= 1.05 and float(frame["close"].iloc[-1]) >= float(frame["open"].iloc[-1]) else -1 if turnover_ratio >= 1.05 and float(frame["close"].iloc[-1]) < float(frame["open"].iloc[-1]) else 0,
        }
        values = {
            "close": last_close,
            "ema21": _last(ema21),
            "ema55": _last(ema55),
            "rsi14": _last(rsi14, 50),
            "macd_hist": _last(macd_hist),
            "atr14": max(_last(atr_series), last_close * 0.002),
            "atr_pct": max(_last(atr_series), last_close * 0.002) / max(last_close, 1e-12) * 100,
            "turnover_ratio": float(turnover_ratio),
            "vwap": vwap_value,
        }
        return votes, values

    def generate(self, candles, portfolio=None, position=None) -> Signal:
        frame = _prepare(candles, 80)
        if frame.empty:
            return Signal(self.symbol, 0, "chart_ai_consensus_warmup", 0.0)
        votes, values = self._votes(frame)
        weights = {"trend": 0.28, "momentum": 0.24, "breakout": 0.22, "value": 0.16, "flow": 0.10}
        long_score = sum(weights[key] for key, vote in votes.items() if vote > 0)
        short_score = sum(weights[key] for key, vote in votes.items() if vote < 0)
        market_ready = self.min_atr_pct <= values["atr_pct"] <= self.max_atr_pct
        pos, _ = _position(position)
        meta = {**values, "score_long": float(long_score), "score_short": float(short_score), **{f"vote_{key}": value for key, value in votes.items()}}
        if pos == 1:
            if long_score < 0.35 or short_score > long_score + 0.15:
                return Signal(self.symbol, 0, "chart_ai_long_confidence_faded", 0.78, meta)
            return Signal(self.symbol, 1, "chart_ai_hold_long", float(long_score), meta)
        if pos == -1:
            if short_score < 0.35 or long_score > short_score + 0.15:
                return Signal(self.symbol, 0, "chart_ai_short_confidence_faded", 0.78, meta)
            return Signal(self.symbol, -1, "chart_ai_hold_short", float(short_score), meta)

        if market_ready and long_score >= self.threshold and long_score > short_score + 0.12:
            confidence = min(0.97, long_score)
            return Signal(self.symbol, 1, "chart_ai_consensus_long", confidence, _risk_metadata(1, values["close"], values["atr14"], float(frame["low"].tail(12).min()), "chart_ai_long", confidence, self.stop_atr, self.take_r, meta))
        if market_ready and self.allow_short and short_score >= self.threshold and short_score > long_score + 0.12:
            confidence = min(0.97, short_score)
            return Signal(self.symbol, -1, "chart_ai_consensus_short", confidence, _risk_metadata(-1, values["close"], values["atr14"], float(frame["high"].tail(12).max()), "chart_ai_short", confidence, self.stop_atr, self.take_r, meta))
        return Signal(self.symbol, 0, "chart_ai_consensus_wait", max(long_score, short_score), meta)
