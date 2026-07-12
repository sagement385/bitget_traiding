from src.strategies.base import Strategy
from src.indicators.trend import donchian_high, donchian_low
from src.models import Signal

class DonchianBreakoutStrategy(Strategy):
    name='breakout'
    def __init__(self, symbol='BTCUSDT', window=20, allow_short=False):
        self.symbol=symbol; self.window=window; self.allow_short=allow_short
    def generate(self,candles,portfolio=None,position=None):
        if len(candles)<self.window+2: return Signal(self.symbol,0,'not_enough_data',0)
        prev_high=donchian_high(candles['high'],self.window).iloc[-2]
        prev_low=donchian_low(candles['low'],self.window).iloc[-2]
        close=float(candles['close'].iloc[-1])
        if close>prev_high: return Signal(self.symbol,1,'donchian_up_breakout',0.65)
        if close<prev_low: return Signal(self.symbol,-1 if self.allow_short else 0,'donchian_down_breakout',0.65)
        return Signal(self.symbol,0,'inside_channel',0.4)
