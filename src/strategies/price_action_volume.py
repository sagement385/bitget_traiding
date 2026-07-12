from __future__ import annotations

import pandas as pd
import numpy as np
from src.models import Signal
from src.strategies.base import Strategy


def _ema(s: pd.Series, n: int) -> pd.Series:
    return pd.to_numeric(s, errors='coerce').ewm(span=n, adjust=False).mean()


class PriceActionVolumeStrategy(Strategy):
    """Volume + candle-structure price-action strategy.

    Long setup A: institutional impulse candle -> wait pullback -> enter at support.
    Long setup B: downtrend capitulation lower wick -> break high -> pullback -> enter.
    Short setup: high-volume upper-wick rejection bearish candle -> enter next confirmation.

    The strategy only returns entries. Initial stop / R-management metadata is handled by
    the backtest engine so the same signal can be reused by paper/live brokers.
    """

    name = 'price_action_volume'

    def __init__(
        self,
        symbol='BTCUSDT',
        allow_short=True,
        volume_window=20,
        volume_mult=1.8,
        body_mult=1.25,
        pullback_bars=13,
        retrace_min=0.30,
        retrace_max=0.50,
        wick_ratio=1.6,
        close_near_pct=0.30,
        trend_ema=50,
        htf_ema=50,
        min_rr=2.0,
    ):
        self.symbol = symbol
        self.allow_short = allow_short
        self.volume_window = int(volume_window)
        self.volume_mult = float(volume_mult)
        self.body_mult = float(body_mult)
        self.pullback_bars = int(pullback_bars)
        self.retrace_min = float(retrace_min)
        self.retrace_max = float(retrace_max)
        self.wick_ratio = float(wick_ratio)
        self.close_near_pct = float(close_near_pct)
        self.trend_ema = int(trend_ema)
        self.htf_ema = int(htf_ema)
        self.min_rr = float(min_rr)

    def _prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        d = candles.copy().sort_values('timestamp').reset_index(drop=True)
        for c in ['open', 'high', 'low', 'close', 'volume']:
            d[c] = pd.to_numeric(d[c], errors='coerce')
        d['body'] = (d['close'] - d['open']).abs()
        d['range'] = (d['high'] - d['low']).replace(0, np.nan)
        d['upper_wick'] = d['high'] - d[['open', 'close']].max(axis=1)
        d['lower_wick'] = d[['open', 'close']].min(axis=1) - d['low']
        d['vol_ma'] = d['volume'].rolling(self.volume_window, min_periods=max(5, self.volume_window // 2)).mean()
        d['body_ma'] = d['body'].rolling(self.volume_window, min_periods=max(5, self.volume_window // 2)).mean()
        d['ema_trend'] = _ema(d['close'], self.trend_ema)
        d['swing_high'] = d['high'].rolling(5, min_periods=3).max()
        d['swing_low'] = d['low'].rolling(5, min_periods=3).min()
        return d

    def _base_trend(self, d: pd.DataFrame) -> int:
        if len(d) < self.trend_ema + 5:
            return 0
        c = float(d['close'].iloc[-1])
        ema_now = float(d['ema_trend'].iloc[-1])
        ema_prev = float(d['ema_trend'].iloc[-6])
        recent_highs_up = d['high'].iloc[-5:].max() > d['high'].iloc[-12:-5].max() if len(d) >= 12 else False
        recent_lows_up = d['low'].iloc[-5:].min() > d['low'].iloc[-12:-5].min() if len(d) >= 12 else False
        recent_highs_dn = d['high'].iloc[-5:].max() < d['high'].iloc[-12:-5].max() if len(d) >= 12 else False
        recent_lows_dn = d['low'].iloc[-5:].min() < d['low'].iloc[-12:-5].min() if len(d) >= 12 else False
        if c > ema_now and ema_now >= ema_prev and (recent_highs_up or recent_lows_up):
            return 1
        if c < ema_now and ema_now <= ema_prev and (recent_highs_dn or recent_lows_dn):
            return -1
        return 0

    def _higher_timeframe_trend(self, d: pd.DataFrame) -> int:
        # Fast higher-timeframe approximation from the same OHLCV frame.
        # Avoids expensive datetime resampling on every backtest bar.
        if 'timestamp' not in d or len(d) < 80:
            return 0
        ts = pd.to_numeric(d['timestamp'], errors='coerce').dropna()
        if len(ts) < 3:
            return 0
        step_ms = float(ts.diff().dropna().median())
        if not step_ms or step_ms <= 0:
            return 0
        votes = []
        for tf_ms in (15 * 60_000, 60 * 60_000):
            group = max(1, int(round(tf_ms / step_ms)))
            if len(d) < group * 8:
                continue
            tail = d.tail(group * min(80, max(8, len(d)//group))).copy().reset_index(drop=True)
            gid = tail.index // group
            r = tail.groupby(gid).agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
            if len(r) < 8:
                continue
            ema_n = min(self.htf_ema, max(8, len(r)//2))
            ema = _ema(r['close'], ema_n)
            if r['close'].iloc[-1] > ema.iloc[-1] and ema.iloc[-1] >= ema.iloc[max(0, len(ema)-4)]:
                votes.append(1)
            elif r['close'].iloc[-1] < ema.iloc[-1] and ema.iloc[-1] <= ema.iloc[max(0, len(ema)-4)]:
                votes.append(-1)
            else:
                votes.append(0)
        if not votes:
            return 0
        if sum(votes) >= 1:
            return 1
        if sum(votes) <= -1:
            return -1
        return 0

    def _is_impulse_bull(self, r) -> bool:
        if pd.isna(r.vol_ma) or pd.isna(r.body_ma):
            return False
        return (
            r.close > r.open
            and r.volume >= r.vol_ma * self.volume_mult
            and r.body >= r.body_ma * self.body_mult
            and (r.close - r.low) / max(r.high - r.low, 1e-12) >= 0.60
        )

    def _is_lower_wick_reversal(self, d: pd.DataFrame, idx: int) -> bool:
        r = d.iloc[idx]
        if pd.isna(r.vol_ma):
            return False
        prior = d.iloc[max(0, idx-8):idx]
        downtrend = len(prior) >= 5 and prior['close'].iloc[-1] < prior['close'].iloc[0]
        return (
            downtrend
            and r.volume >= r.vol_ma * self.volume_mult
            and r.lower_wick >= max(r.body, 1e-12) * self.wick_ratio
            and r.close > r.low + r.range * 0.45
        )

    def _is_short_rejection(self, r) -> bool:
        if pd.isna(r.vol_ma):
            return False
        near_low = r.close <= r.low + r.range * self.close_near_pct
        return (
            r.close < r.open
            and r.volume >= r.vol_ma * self.volume_mult
            and r.upper_wick >= max(r.body, 1e-12) * self.wick_ratio
            and near_low
        )

    def _long_from_impulse(self, d: pd.DataFrame, htf: int) -> Signal | None:
        if htf == -1:
            return None
        last = d.iloc[-1]
        start = max(0, len(d) - self.pullback_bars - 1)
        for idx in range(len(d)-2, start-1, -1):
            ev = d.iloc[idx]
            if not self._is_impulse_bull(ev):
                continue
            after = d.iloc[idx+1:]
            if len(after) < 2 or len(after) > self.pullback_bars + 1:
                continue
            body_low = min(ev.open, ev.close)
            body_high = max(ev.open, ev.close)
            retrace_low = body_high - (body_high - body_low) * self.retrace_max
            retrace_high = body_high - (body_high - body_low) * self.retrace_min
            touched = after['low'].min() <= retrace_high and after['low'].min() >= ev.low * 0.999
            confirmed = last.close > last.open and last.close >= retrace_low and last.close > d['high'].iloc[-2]
            if touched and confirmed:
                stop = float(min(ev.low, after['low'].min()))
                entry_ref = float(last.close)
                if entry_ref <= stop:
                    continue
                return Signal(self.symbol, 1, 'pa_long_impulse_pullback', 0.78, {
                    'setup': 'long_impulse_pullback', 'event_ts': int(ev.timestamp), 'event_high': float(ev.high), 'event_low': float(ev.low),
                    'pullback_zone_low': float(retrace_low), 'pullback_zone_high': float(retrace_high), 'initial_stop': stop,
                    'r_management': True, 'partial_take_r': 2.0, 'breakeven_r': 1.0, 'lock_r': 1.5, 'lock_profit_r': 0.5,
                    'trail_after_r': 2.0, 'trail_ema': 21, 'trail_swing_lookback': 5,
                })
        return None

    def _long_from_capitulation(self, d: pd.DataFrame, htf: int) -> Signal | None:
        if htf == -1:
            return None
        last = d.iloc[-1]
        start = max(0, len(d) - self.pullback_bars - 3)
        for idx in range(len(d)-3, start-1, -1):
            ev = d.iloc[idx]
            if not self._is_lower_wick_reversal(d, idx):
                continue
            after = d.iloc[idx+1:]
            broke_high = after['high'].max() > ev.high
            if not broke_high:
                continue
            pullback_ok = after['low'].min() > ev.low and after['low'].min() <= ev.high
            confirmed = last.close > last.open and last.close > d['high'].iloc[-2]
            if pullback_ok and confirmed:
                stop = float(ev.low)
                if float(last.close) <= stop:
                    continue
                return Signal(self.symbol, 1, 'pa_long_lower_wick_break_pullback', 0.74, {
                    'setup': 'long_lower_wick_reversal', 'event_ts': int(ev.timestamp), 'event_high': float(ev.high), 'event_low': float(ev.low),
                    'initial_stop': stop, 'r_management': True, 'partial_take_r': 2.0, 'breakeven_r': 1.0,
                    'lock_r': 1.5, 'lock_profit_r': 0.5, 'trail_after_r': 2.0, 'trail_ema': 21, 'trail_swing_lookback': 5,
                })
        return None

    def _short_rejection(self, d: pd.DataFrame, htf: int) -> Signal | None:
        if not self.allow_short or htf == 1 or len(d) < self.volume_window + 5:
            return None
        ev = d.iloc[-2]
        last = d.iloc[-1]
        if not self._is_short_rejection(ev):
            return None
        confirmed = last.close < ev.close or last.low < ev.low
        if not confirmed:
            return None
        stop = float(ev.high)
        if float(last.close) >= stop:
            return None
        return Signal(self.symbol, -1, 'pa_short_upper_wick_rejection', 0.76, {
            'setup': 'short_upper_wick_rejection', 'event_ts': int(ev.timestamp), 'event_high': float(ev.high), 'event_low': float(ev.low),
            'initial_stop': stop, 'r_management': True, 'partial_take_r': 2.0, 'breakeven_r': 1.0,
            'lock_r': 1.5, 'lock_profit_r': 0.5, 'trail_after_r': 2.0, 'trail_ema': 21, 'trail_swing_lookback': 5,
        })

    def generate(self, candles, portfolio=None, position=None) -> Signal:
        if candles is None or len(candles) < max(60, self.volume_window + 20):
            return Signal(self.symbol, int((position or {}).get('target_position', 0) or 0), 'warmup', 0.0, {})
        pos = int((position or {}).get('target_position', 0) or 0)
        if pos != 0:
            return Signal(self.symbol, pos, 'position_managed_by_r_engine', 0.5, {})
        d = self._prepare(candles).dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(d) < max(60, self.volume_window + 20):
            return Signal(self.symbol, 0, 'warmup', 0.0, {})
        base_trend = self._base_trend(d)
        htf_trend = self._higher_timeframe_trend(d)
        # Do not fight both base and higher timeframe trend at the same time.
        long_blocked = base_trend == -1 and htf_trend == -1
        short_blocked = base_trend == 1 and htf_trend == 1
        sig = None if long_blocked else (self._long_from_impulse(d, htf_trend) or self._long_from_capitulation(d, htf_trend))
        if sig:
            sig.metadata = {**(sig.metadata or {}), 'base_trend': base_trend, 'htf_trend': htf_trend}
            return sig
        sig = None if short_blocked else self._short_rejection(d, htf_trend)
        if sig:
            sig.metadata = {**(sig.metadata or {}), 'base_trend': base_trend, 'htf_trend': htf_trend}
            return sig
        return Signal(self.symbol, 0, f'no_setup base={base_trend} htf={htf_trend}', 0.0, {'base_trend': base_trend, 'htf_trend': htf_trend})
