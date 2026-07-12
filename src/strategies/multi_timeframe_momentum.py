from __future__ import annotations

import pandas as pd

from src.indicators.library import atr as calc_atr, ema, macd, rsi as calc_rsi, sma, vwap
from src.models import Signal
from src.strategies.base import Strategy


class MultiTimeframeMomentumStrategy(Strategy):
    """No-lookahead multi-timeframe momentum strategy.

    Intended workflow:
    - Backfill 1m candles.
    - Run the strategy on 1m data.
    - The strategy derives only completed 5m, 15m, and 1H candles from the
      historical rows it has already received from the backtest engine.
    """

    name = "multi_timeframe_momentum"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        allow_short: bool = True,
        min_rel_volume: float = 0.9,
        min_atr_pct: float = 0.035,
        stop_atr: float = 1.3,
        take_r: float = 2.0,
    ):
        self.symbol = symbol
        self.allow_short = bool(allow_short)
        self.min_rel_volume = float(min_rel_volume)
        self.min_atr_pct = float(min_atr_pct)
        self.stop_atr = float(stop_atr)
        self.take_r = float(take_r)

    def _prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        d = candles.copy().sort_values("timestamp").reset_index(drop=True)
        for c in ["timestamp", "open", "high", "low", "close", "volume"]:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d.dropna(subset=["timestamp", "open", "high", "low", "close"]).reset_index(drop=True)

    def _completed_groups(self, d: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if d.empty:
            return d
        step = pd.to_numeric(d["timestamp"], errors="coerce").diff().dropna().median()
        if pd.isna(step) or step <= 0:
            return pd.DataFrame()
        group = max(1, int(round((minutes * 60_000) / float(step))))
        if len(d) < group * 8:
            return pd.DataFrame()
        work = d.copy()
        work["bucket"] = (work["timestamp"].astype("int64") // (minutes * 60_000)) * (minutes * 60_000)
        count = work.groupby("bucket")["timestamp"].transform("count")
        work = work[count >= group].copy()
        if work.empty:
            return pd.DataFrame()
        out = (
            work.groupby("bucket", sort=True)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "timestamp": "last"})
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index(drop=True)
        )
        return out

    def _trend_vote(self, frame: pd.DataFrame, fast: int, slow: int) -> int:
        if frame is None or len(frame) < slow + 5:
            return 0
        close = frame["close"]
        f = ema(close, fast)
        s = ema(close, slow)
        if f.iloc[-1] > s.iloc[-1] and s.iloc[-1] >= s.iloc[-4]:
            return 1
        if f.iloc[-1] < s.iloc[-1] and s.iloc[-1] <= s.iloc[-4]:
            return -1
        return 0

    def _position(self, position) -> tuple[int, float]:
        if isinstance(position, dict):
            return int(position.get("target_position") or 0), float(position.get("entry_price") or 0)
        return 0, 0.0

    def generate(self, candles, portfolio=None, position=None) -> Signal:
        d = self._prepare(candles)
        if len(d) < 240:
            return Signal(self.symbol, 0, "warmup", 0.0, {})

        pos, entry = self._position(position)
        close = d["close"]
        last_close = float(close.iloc[-1])
        ema9 = ema(close, 9)
        ema21 = ema(close, 21)
        ema50 = ema(close, 50)
        rsi14 = calc_rsi(close, 14)
        atr14 = calc_atr(d, 14)
        vwap_s = vwap(d.tail(min(len(d), 1440)), window=None)
        _, _, hist = macd(close)
        vol_sma20 = sma(d["volume"], 20)
        atr = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) and atr14.iloc[-1] > 0 else last_close * 0.001
        atr_pct = atr / last_close * 100
        rel_vol = float(d["volume"].iloc[-1]) / max(float(vol_sma20.iloc[-1] or 0), 1e-12)

        m5 = self._completed_groups(d, 5)
        m15 = self._completed_groups(d, 15)
        h1 = self._completed_groups(d, 60)
        vote5 = self._trend_vote(m5, 9, 21)
        vote15 = self._trend_vote(m15, 9, 21)
        vote60 = self._trend_vote(h1, 20, 50)

        meta = {
            "close": last_close,
            "ema9": float(ema9.iloc[-1]),
            "ema21": float(ema21.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "rsi14": float(rsi14.iloc[-1]),
            "macd_hist": float(hist.iloc[-1]),
            "atr14": atr,
            "atr_pct": atr_pct,
            "rel_volume": rel_vol,
            "mtf_vote_5m": vote5,
            "mtf_vote_15m": vote15,
            "mtf_vote_1h": vote60,
        }

        if pos != 0:
            if pos == 1 and (vote15 < 0 or last_close < float(ema21.iloc[-1]) or float(hist.iloc[-1]) < 0):
                return Signal(self.symbol, 0, "mtf_exit_long_momentum_loss", 0.75, meta)
            if pos == -1 and (vote15 > 0 or last_close > float(ema21.iloc[-1]) or float(hist.iloc[-1]) > 0):
                return Signal(self.symbol, 0, "mtf_exit_short_momentum_loss", 0.75, meta)
            return Signal(self.symbol, pos, "mtf_hold", 0.55, meta)

        long_setup = (
            vote5 >= 1
            and vote15 >= 1
            and vote60 >= 0
            and last_close > float(vwap_s.iloc[-1])
            and float(ema9.iloc[-1]) > float(ema21.iloc[-1]) > float(ema50.iloc[-1])
            and float(hist.iloc[-1]) > 0
            and 48 <= float(rsi14.iloc[-1]) <= 72
            and rel_vol >= self.min_rel_volume
            and atr_pct >= self.min_atr_pct
        )
        if long_setup:
            swing = float(d["low"].tail(12).min())
            stop = min(swing, last_close - atr * self.stop_atr)
            return Signal(
                self.symbol,
                1,
                "mtf_long_5m_15m_1h_aligned",
                0.82,
                {
                    **meta,
                    "setup": "mtf_long",
                    "initial_stop": stop,
                    "r_management": True,
                    "partial_take_r": self.take_r,
                    "breakeven_r": 1.0,
                    "lock_r": 1.5,
                    "lock_profit_r": 0.5,
                    "trail_after_r": 2.0,
                    "trail_ema": 21,
                    "trail_swing_lookback": 8,
                },
            )

        short_setup = (
            self.allow_short
            and vote5 <= -1
            and vote15 <= -1
            and vote60 <= 0
            and last_close < float(vwap_s.iloc[-1])
            and float(ema9.iloc[-1]) < float(ema21.iloc[-1]) < float(ema50.iloc[-1])
            and float(hist.iloc[-1]) < 0
            and 28 <= float(rsi14.iloc[-1]) <= 52
            and rel_vol >= self.min_rel_volume
            and atr_pct >= self.min_atr_pct
        )
        if short_setup:
            swing = float(d["high"].tail(12).max())
            stop = max(swing, last_close + atr * self.stop_atr)
            return Signal(
                self.symbol,
                -1,
                "mtf_short_5m_15m_1h_aligned",
                0.80,
                {
                    **meta,
                    "setup": "mtf_short",
                    "initial_stop": stop,
                    "r_management": True,
                    "partial_take_r": self.take_r,
                    "breakeven_r": 1.0,
                    "lock_r": 1.5,
                    "lock_profit_r": 0.5,
                    "trail_after_r": 2.0,
                    "trail_ema": 21,
                    "trail_swing_lookback": 8,
                },
            )

        return Signal(self.symbol, 0, "mtf_no_setup", 0.30, meta)
