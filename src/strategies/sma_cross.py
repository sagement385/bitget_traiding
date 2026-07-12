from src.strategies.base import Strategy
from src.indicators.trend import sma
from src.models import Signal

class SmaCrossStrategy(Strategy):
    name='sma_cross'
    def __init__(self, symbol='BTCUSDT', fast=20, slow=60, allow_short=False):
        if fast >= slow: raise ValueError('fast must be < slow')
        self.symbol=symbol; self.fast=fast; self.slow=slow; self.allow_short=allow_short
    def generate(self, candles, portfolio=None, position=None):
        if len(candles) < self.slow+2: return Signal(self.symbol,0,'not_enough_data',0)
        c=candles['close']; f=sma(c,self.fast); s=sma(c,self.slow)
        if f.iloc[-2] <= s.iloc[-2] and f.iloc[-1] > s.iloc[-1]:
            return Signal(self.symbol,1,'fast_ma_cross_above_slow',0.7,{'fast':float(f.iloc[-1]),'slow':float(s.iloc[-1])})
        if f.iloc[-2] >= s.iloc[-2] and f.iloc[-1] < s.iloc[-1]:
            return Signal(self.symbol,-1 if self.allow_short else 0,'fast_ma_cross_below_slow',0.7)
        cur=0 if position is None else (1 if getattr(position,'qty',0)>0 and getattr(position,'side','')=='long' else -1 if getattr(position,'qty',0)>0 and getattr(position,'side','')=='short' else 0)
        return Signal(self.symbol,cur,'hold',0.5)
