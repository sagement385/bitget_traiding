from __future__ import annotations
from src.models import Signal
from src.strategies.base import Strategy
from src.indicators.library import ema, rsi as calc_rsi, atr as calc_atr, vwap as calc_vwap, macd as calc_macd, sma


class ScalpVwapRsiStrategy(Strategy):
    """
    Intraday scalping candidate strategy.

    Design intent:
    - Use VWAP as session/value filter.
    - Use EMA 9/21 as short-term trend filter.
    - Use RSI and MACD histogram as momentum confirmation.
    - Use ATR for volatility floor and stop/take-profit exits.
    - Use relative volume to avoid dead zones.

    This is not a guaranteed-profitable strategy. It is a systematic candidate
    meant to be backtested, paper-traded, and tuned per symbol/timeframe.
    """
    name = 'scalp_vwap_rsi'

    def __init__(
        self,
        symbol='BTCUSDT',
        allow_short=False,
        rsi_period=14,
        min_rel_volume=0.80,
        min_atr_pct=0.03,
        stop_atr=1.20,
        take_atr=1.80,
        use_supertrend=True,
    ):
        self.symbol = symbol
        self.allow_short = allow_short
        self.rsi_period = int(rsi_period)
        self.min_rel_volume = float(min_rel_volume)
        self.min_atr_pct = float(min_atr_pct)
        self.stop_atr = float(stop_atr)
        self.take_atr = float(take_atr)
        self.use_supertrend = bool(use_supertrend)

    def _pos(self, position):
        if isinstance(position, dict):
            return int(position.get('target_position') or position.get('pos') or 0), float(position.get('entry_price') or 0)
        return 0, 0.0

    def generate(self, candles, portfolio=None, position=None):
        if candles is None or len(candles) < 80:
            return Signal(self.symbol, 0, 'not_enough_data', 0)

        window = candles.tail(120).copy()
        close_s = window['close']
        ema9 = ema(close_s, 9)
        ema21 = ema(close_s, 21)
        ema50 = ema(close_s, 50)
        rsi_s = calc_rsi(close_s, self.rsi_period)
        atr_s = calc_atr(window, 14)
        vwap_s = calc_vwap(window, window=None)
        _, _, macd_hist_s = calc_macd(close_s)
        vol_sma = sma(window.get('volume', close_s * 0), 20)
        if len(window) < 60 or any(x.empty for x in [ema9, ema21, rsi_s, atr_s, vwap_s, macd_hist_s]):
            return Signal(self.symbol, 0, 'not_enough_indicator_data', 0)

        pos, entry = self._pos(position)
        close = float(window['close'].iloc[-1])
        atr = float(atr_s.iloc[-1]) if float(atr_s.iloc[-1]) > 0 else close * 0.001
        rsi = float(rsi_s.iloc[-1])
        rel_vol = float(window.get('volume', close_s*0).iloc[-1]) / max(float(vol_sma.iloc[-1] or 0), 1e-12)
        atr_pct = atr / close * 100
        supertrend_up = close >= float(ema50.iloc[-1])
        r = {'vwap': float(vwap_s.iloc[-1]), 'ema9': float(ema9.iloc[-1]), 'ema21': float(ema21.iloc[-1]), 'macd_hist': float(macd_hist_s.iloc[-1])}
        prev = {'ema9': float(ema9.iloc[-2]), 'ema21': float(ema21.iloc[-2])}

        long_setup = (
            close > float(r['vwap'])
            and float(r['ema9']) > float(r['ema21'])
            and float(prev['ema9']) <= float(prev['ema21']) or (
                close > float(r['vwap'])
                and float(r['ema9']) > float(r['ema21'])
                and 48 <= rsi <= 72
                and float(r['macd_hist']) > 0
            )
        )
        long_filter = rel_vol >= self.min_rel_volume and atr_pct >= self.min_atr_pct and (supertrend_up or not self.use_supertrend)
        short_setup = (
            close < float(r['vwap'])
            and float(r['ema9']) < float(r['ema21'])
            and 28 <= rsi <= 52
            and float(r['macd_hist']) < 0
        )
        short_filter = rel_vol >= self.min_rel_volume and atr_pct >= self.min_atr_pct and ((not supertrend_up) or not self.use_supertrend)

        meta = {
            'close': close,
            'vwap': float(r['vwap']),
            'ema9': float(r['ema9']),
            'ema21': float(r['ema21']),
            'rsi14': rsi,
            'macd_hist': float(r['macd_hist']),
            'atr14': atr,
            'atr_pct': atr_pct,
            'rel_volume': rel_vol,
            'stop_atr': self.stop_atr,
            'take_atr': self.take_atr,
        }

        if pos == 1:
            if entry > 0 and close <= entry - atr * self.stop_atr:
                return Signal(self.symbol, 0, 'atr_stop_long', 0.95, meta)
            if entry > 0 and close >= entry + atr * self.take_atr:
                return Signal(self.symbol, 0, 'atr_take_profit_long', 0.90, meta)
            if close < float(r['ema21']) or float(r['macd_hist']) < 0 or rsi > 78:
                return Signal(self.symbol, 0, 'momentum_exit_long', 0.75, meta)
            return Signal(self.symbol, 1, 'hold_long', 0.60, meta)

        if pos == -1:
            if entry > 0 and close >= entry + atr * self.stop_atr:
                return Signal(self.symbol, 0, 'atr_stop_short', 0.95, meta)
            if entry > 0 and close <= entry - atr * self.take_atr:
                return Signal(self.symbol, 0, 'atr_take_profit_short', 0.90, meta)
            if close > float(r['ema21']) or float(r['macd_hist']) > 0 or rsi < 22:
                return Signal(self.symbol, 0, 'momentum_exit_short', 0.75, meta)
            return Signal(self.symbol, -1, 'hold_short', 0.60, meta)

        if long_setup and long_filter:
            return Signal(self.symbol, 1, 'scalp_long_vwap_ema_rsi_macd', 0.78, meta)
        if self.allow_short and short_setup and short_filter:
            return Signal(self.symbol, -1, 'scalp_short_vwap_ema_rsi_macd', 0.78, meta)
        return Signal(self.symbol, 0, 'no_scalp_setup', 0.35, meta)
