import numpy as np, pandas as pd
from src.utils.time import INTERVAL_MS, to_ms

def make_demo_candles(symbol='BTCUSDT', category='USDT-FUTURES', interval='1H', start='2024-01-01', periods=1500, seed=7):
    rng=np.random.default_rng(seed)
    ts=to_ms(start)+np.arange(periods)*INTERVAL_MS[interval]
    ret=rng.normal(0.0002,0.012,periods)
    close=30000*np.exp(np.cumsum(ret))
    open_=np.r_[close[0], close[:-1]]
    high=np.maximum(open_,close)*(1+rng.uniform(0,0.006,periods))
    low=np.minimum(open_,close)*(1-rng.uniform(0,0.006,periods))
    volume=rng.uniform(100,1000,periods)
    return pd.DataFrame({'timestamp':ts,'open':open_,'high':high,'low':low,'close':close,'volume':volume,'turnover':volume*close,'symbol':symbol,'category':category,'interval':interval})
