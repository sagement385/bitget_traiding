from src.strategies.sma_cross import SmaCrossStrategy
from src.strategies.rsi_reversal import RsiReversalStrategy
from src.strategies.breakout import DonchianBreakoutStrategy
from src.strategies.scalp_vwap_rsi import ScalpVwapRsiStrategy
from src.strategies.price_action_volume import PriceActionVolumeStrategy
from src.strategies.multi_timeframe_momentum import MultiTimeframeMomentumStrategy

STRATEGIES = {
    'sma_cross': SmaCrossStrategy,
    'rsi_reversal': RsiReversalStrategy,
    'rsi': RsiReversalStrategy,
    'breakout': DonchianBreakoutStrategy,
    'scalp_vwap_rsi': ScalpVwapRsiStrategy,
    'scalp': ScalpVwapRsiStrategy,
    'price_action_volume': PriceActionVolumeStrategy,
    'pa_volume': PriceActionVolumeStrategy,
    'multi_timeframe_momentum': MultiTimeframeMomentumStrategy,
    'mtf_momentum': MultiTimeframeMomentumStrategy,
}

def make_strategy(name, **kwargs):
    if name not in STRATEGIES:
        raise ValueError(f'unknown strategy: {name}. choices={list(STRATEGIES)}')
    return STRATEGIES[name](**kwargs)
