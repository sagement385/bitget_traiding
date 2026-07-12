from src.strategies.base import Strategy
from src.indicators.momentum import rsi
from src.models import Signal

class RsiReversalStrategy(Strategy):
    name='rsi_reversal'
    def __init__(self, symbol='BTCUSDT', period=14, lower=30, upper=70, allow_short=False):
        self.symbol=symbol; self.period=period; self.lower=lower; self.upper=upper; self.allow_short=allow_short
    def generate(self,candles,portfolio=None,position=None):
        if len(candles)<self.period+2: return Signal(self.symbol,0,'not_enough_data',0)
        val=float(rsi(candles['close'], self.period).iloc[-1])
        if val < self.lower: return Signal(self.symbol,1,'rsi_oversold',0.65,{'rsi':val})
        if val > self.upper: return Signal(self.symbol,-1 if self.allow_short else 0,'rsi_overbought',0.65,{'rsi':val})
        return Signal(self.symbol,0,'neutral',0.4,{'rsi':val})
