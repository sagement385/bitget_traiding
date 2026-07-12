from __future__ import annotations
import numpy as np
import pandas as pd

from src.markets import CRYPTO, market_local_datetime, normalize_market_type


def _s(x):
    return pd.to_numeric(x, errors='coerce')


def sma(series: pd.Series, period: int) -> pd.Series:
    return _s(series).rolling(int(period), min_periods=int(period)).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return _s(series).ewm(span=int(period), adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    close = _s(close)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def true_range(df: pd.DataFrame) -> pd.Series:
    high = _s(df['high']); low = _s(df['low']); close = _s(df['close'])
    prev_close = close.shift(1)
    return pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def vwap(df: pd.DataFrame, window: int | None = None) -> pd.Series:
    high = _s(df['high']); low = _s(df['low']); close = _s(df['close']); vol = _s(df.get('volume', 0)).fillna(0)
    tp = (high + low + close) / 3
    if window:
        pv = (tp * vol).rolling(window, min_periods=max(2, window//4)).sum()
        vv = vol.rolling(window, min_periods=max(2, window//4)).sum()
    else:
        pv = (tp * vol).cumsum(); vv = vol.cumsum()
    return (pv / vv.replace(0, np.nan)).ffill()


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    c = _s(close)
    mid = sma(c, period)
    sd = c.rolling(period, min_periods=period).std()
    return mid, mid + std_mult * sd, mid - std_mult * sd


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    c = _s(close)
    m = ema(c, fast) - ema(c, slow)
    sig = ema(m, signal)
    hist = m - sig
    return m, sig, hist


def obv(df: pd.DataFrame) -> pd.Series:
    close = _s(df['close']); vol = _s(df.get('volume', 0)).fillna(0)
    direction = np.sign(close.diff()).fillna(0)
    return (direction * vol).cumsum()


def donchian(df: pd.DataFrame, period: int = 20):
    return _s(df['high']).rolling(period, min_periods=period).max(), _s(df['low']).rolling(period, min_periods=period).min()


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = _s(df['low']).rolling(k_period, min_periods=k_period).min()
    high_max = _s(df['high']).rolling(k_period, min_periods=k_period).max()
    k = 100 * (_s(df['close']) - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period, min_periods=d_period).mean()
    return k.fillna(50), d.fillna(50)


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    # Iterative implementation suitable for chart overlay and signal filtering.
    high = _s(df['high']).reset_index(drop=True); low = _s(df['low']).reset_index(drop=True); close = _s(df['close']).reset_index(drop=True)
    a = atr(df.reset_index(drop=True), period).reset_index(drop=True)
    hl2 = (high + low) / 2
    upper = hl2 + multiplier * a
    lower = hl2 - multiplier * a
    trend = pd.Series(index=df.index, dtype='float64')
    direction = pd.Series(index=df.index, dtype='int64')
    if len(df) == 0:
        return trend, direction
    final_upper = upper.copy(); final_lower = lower.copy()
    direction.iloc[0] = 1
    trend.iloc[0] = final_lower.iloc[0]
    for i in range(1, len(df)):
        if pd.notna(final_upper.iloc[i-1]) and close.iloc[i-1] > final_upper.iloc[i-1]:
            direction.iloc[i] = 1
        elif pd.notna(final_lower.iloc[i-1]) and close.iloc[i-1] < final_lower.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i-1]
            if direction.iloc[i] == 1 and lower.iloc[i] < final_lower.iloc[i-1]:
                final_lower.iloc[i] = final_lower.iloc[i-1]
            if direction.iloc[i] == -1 and upper.iloc[i] > final_upper.iloc[i-1]:
                final_upper.iloc[i] = final_upper.iloc[i-1]
        trend.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]
    return trend, direction


def session_vwap(df: pd.DataFrame, market_type: str = CRYPTO) -> pd.Series:
    """Compute a VWAP reset at each market-local trading day.

    A cumulative VWAP across an arbitrary chart window changes when the user
    pans or loads older bars. For intraday momentum analysis, session-anchored
    VWAP is the stable reference used by the surge metrics.
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    market_type = normalize_market_type(market_type)
    d = df.copy()
    timestamp = pd.to_numeric(d.get("timestamp"), errors="coerce")
    high = _s(d["high"])
    low = _s(d["low"])
    close = _s(d["close"])
    volume = _s(d.get("volume", 0)).fillna(0)
    session = timestamp.map(
        lambda value: market_local_datetime(int(value), market_type).date().isoformat() if pd.notna(value) else ""
    )
    typical_price = (high + low + close) / 3
    cumulative_value = (typical_price * volume).groupby(session).cumsum()
    cumulative_volume = volume.groupby(session).cumsum()
    return cumulative_value / cumulative_volume.replace(0, np.nan)


def add_surge_indicators(df: pd.DataFrame, market_type: str = CRYPTO) -> pd.DataFrame:
    """Add non-predictive price/volume diagnostics for momentum-stock review.

    Every metric uses the current and prior bars only. The values are useful
    for ranking attention and validating a strategy, but are not an order
    recommendation or an assertion that a move will continue.
    """
    out = df.copy()
    if out.empty:
        return out
    close = _s(out["close"])
    high = _s(out["high"])
    low = _s(out["low"])
    volume = _s(out.get("volume", 0)).fillna(0)
    supplied_turnover = _s(out.get("turnover", 0)).fillna(0)
    turnover = supplied_turnover.where(supplied_turnover > 0, close * volume)
    volume_mean = volume.rolling(20, min_periods=10).mean()
    volume_std = volume.rolling(20, min_periods=10).std(ddof=0)
    previous_close = close.shift(1)
    previous_high20 = high.shift(1).rolling(20, min_periods=20).max()

    out["session_vwap"] = session_vwap(out, market_type)
    out["relative_volume20"] = volume / volume_mean.replace(0, np.nan)
    out["volume_zscore20"] = (volume - volume_mean) / volume_std.replace(0, np.nan)
    out["turnover_value"] = turnover
    out["turnover_sma20"] = turnover.rolling(20, min_periods=10).mean()
    out["price_change_1_pct"] = close.pct_change(1) * 100
    out["price_change_5_pct"] = close.pct_change(5) * 100
    out["price_change_15_pct"] = close.pct_change(15) * 100
    out["range_pct"] = (high - low) / previous_close.replace(0, np.nan) * 100
    out["atr_pct"] = atr(out, 14) / close.replace(0, np.nan) * 100
    out["vwap_deviation_pct"] = (close / out["session_vwap"].replace(0, np.nan) - 1) * 100
    out["breakout_high20"] = previous_high20
    out["breakout_20_pct"] = (close / previous_high20.replace(0, np.nan) - 1) * 100
    return out


def surge_snapshot(df: pd.DataFrame, market_type: str = CRYPTO) -> dict[str, float | int | bool | None]:
    """Return the latest explicit momentum diagnostics for the selected symbol."""
    if df is None or df.empty:
        return {"ready": False, "bars": 0}
    d = add_surge_indicators(df, market_type)
    last = d.iloc[-1]

    def value(key: str) -> float | None:
        raw = pd.to_numeric(pd.Series([last.get(key)]), errors="coerce").iloc[0]
        return float(raw) if pd.notna(raw) and np.isfinite(raw) else None

    return {
        "ready": len(d) >= 20,
        "bars": int(len(d)),
        "last_price": value("close"),
        "relative_volume20": value("relative_volume20"),
        "volume_zscore20": value("volume_zscore20"),
        "turnover_value": value("turnover_value"),
        "price_change_1_pct": value("price_change_1_pct"),
        "price_change_5_pct": value("price_change_5_pct"),
        "price_change_15_pct": value("price_change_15_pct"),
        "range_pct": value("range_pct"),
        "atr_pct": value("atr_pct"),
        "vwap_deviation_pct": value("vwap_deviation_pct"),
        "breakout_20_pct": value("breakout_20_pct"),
    }


def add_common_indicators(df: pd.DataFrame, include: set[str] | None = None) -> pd.DataFrame:
    """Add chart indicators, calculating only the requested series when known.

    The UI asks for only the indicators the trader has enabled. Keeping that
    request narrow matters for a 6,000-candle chart because SuperTrend is
    intentionally iterative. Existing callers omit ``include`` and retain the
    complete indicator set.
    """
    out = df.copy()
    wanted = set(include) if include is not None else {
        'ema9', 'ema21', 'ema50', 'sma50', 'vwap', 'rsi14', 'atr14',
        'bb_mid', 'bb_upper', 'bb_lower', 'macd', 'macd_signal',
        'macd_hist', 'obv', 'donchian_high', 'donchian_low', 'stoch_k',
        'stoch_d', 'supertrend', 'supertrend_dir', 'volume_sma20',
    }
    close = out['close']
    if 'ema9' in wanted:
        out['ema9'] = ema(close, 9)
    if 'ema21' in wanted:
        out['ema21'] = ema(close, 21)
    if 'ema50' in wanted:
        out['ema50'] = ema(close, 50)
    if 'sma50' in wanted:
        out['sma50'] = sma(close, 50)
    if 'vwap' in wanted:
        out['vwap'] = vwap(out, window=None)
    if 'rsi14' in wanted:
        out['rsi14'] = rsi(close, 14)
    if {'atr14', 'atr_pct'} & wanted:
        out['atr14'] = atr(out, 14)
        if 'atr_pct' in wanted:
            out['atr_pct'] = out['atr14'] / _s(out['close']) * 100
    if {'bb_mid', 'bb_upper', 'bb_lower'} & wanted:
        out['bb_mid'], out['bb_upper'], out['bb_lower'] = bollinger(close, 20, 2.0)
    if {'macd', 'macd_signal', 'macd_hist'} & wanted:
        out['macd'], out['macd_signal'], out['macd_hist'] = macd(close)
    if 'obv' in wanted:
        out['obv'] = obv(out)
    if {'donchian_high', 'donchian_low'} & wanted:
        out['donchian_high'], out['donchian_low'] = donchian(out, 20)
    if {'stoch_k', 'stoch_d'} & wanted:
        out['stoch_k'], out['stoch_d'] = stochastic(out)
    if {'supertrend', 'supertrend_dir'} & wanted:
        out['supertrend'], out['supertrend_dir'] = supertrend(out)
    if 'volume_sma20' in wanted:
        out['volume_sma20'] = sma(out.get('volume', pd.Series(0, index=out.index)), 20)
    return out
