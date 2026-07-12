from src.indicators.library import sma, ema, vwap, bollinger, donchian, supertrend

def donchian_high(s, window):
    return s.rolling(window).max()

def donchian_low(s, window):
    return s.rolling(window).min()
