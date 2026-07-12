import math

def floor_to_step(value: float, step: float) -> float:
    if step <= 0: return value
    return math.floor(value / step) * step

def round_to_tick(value: float, tick: float) -> float:
    if tick <= 0: return value
    return round(round(value / tick) * tick, 12)
