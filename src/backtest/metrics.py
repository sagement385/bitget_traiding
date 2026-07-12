import numpy as np, pandas as pd

from src.markets import CRYPTO, normalize_market_type
from src.utils.time import INTERVAL_MS

def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min()) if len(dd) else 0.0

def sharpe(returns: pd.Series, periods=365*24) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(np.sqrt(periods) * r.mean() / r.std())

def _pct(x):
    return round(float(x) * 100, 4)

def periods_per_year(interval: str, market_type: str = CRYPTO) -> float:
    """Return the expected number of bars per year for Sharpe scaling."""
    market = normalize_market_type(market_type)
    if interval == "1M":
        return 12.0
    if interval == "1W":
        return 52.0
    if interval == "1D":
        return 365.0 if market == CRYPTO else 252.0
    step = INTERVAL_MS.get(interval)
    if not step:
        return 365.0
    session_minutes = 365.0 * 24.0 * 60.0 if market == CRYPTO else 252.0 * 390.0
    return max(1.0, session_minutes / (float(step) / 60_000.0))


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, initial_cash: float, interval='1H', market_type: str = CRYPTO) -> dict:
    if equity_curve.empty or 'equity' not in equity_curve:
        return {'initial_cash': initial_cash, 'final_equity': initial_cash, 'total_return': 0.0, 'mdd': 0.0, 'trade_count': 0}
    eq = pd.to_numeric(equity_curve['equity'], errors='coerce').ffill().bfill()
    rets = eq.pct_change()
    t = trades.copy() if trades is not None else pd.DataFrame()
    if len(t) and 'pnl' in t:
        t['pnl'] = pd.to_numeric(t['pnl'], errors='coerce').fillna(0.0)
    exits = t[t.get('event_type', '') != 'entry'] if len(t) and 'event_type' in t else t
    wins = exits[exits['pnl'] > 0] if len(exits) else exits
    losses = exits[exits['pnl'] < 0] if len(exits) else exits
    gross_profit = float(wins['pnl'].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses['pnl'].sum())) if len(losses) else 0.0
    pf = gross_profit / gross_loss if gross_loss else (float('inf') if gross_profit > 0 else 0.0)
    avg_win = float(wins['pnl'].mean()) if len(wins) else 0.0
    avg_loss = abs(float(losses['pnl'].mean())) if len(losses) else 0.0
    ratio = avg_win / avg_loss if avg_loss else (float('inf') if avg_win > 0 else 0.0)
    long_exits = exits[exits.get('position_side', '') == 'long'] if len(exits) and 'position_side' in exits else pd.DataFrame()
    short_exits = exits[exits.get('position_side', '') == 'short'] if len(exits) and 'position_side' in exits else pd.DataFrame()
    annual_periods = periods_per_year(interval, market_type)
    return {
        'initial_cash': round(float(initial_cash), 4),
        'final_equity': round(float(eq.iloc[-1]), 4) if len(eq) else initial_cash,
        'total_return': _pct(float(eq.iloc[-1] / initial_cash - 1)) if len(eq) else 0.0,
        'mdd': _pct(max_drawdown(eq)),
        'sharpe': round(sharpe(rets, periods=annual_periods), 4),
        'periods_per_year': round(annual_periods, 4),
        'trade_count': int(len(exits)),
        'raw_trade_events': int(len(t)),
        'win_rate': _pct((exits['pnl'] > 0).mean()) if len(exits) else 0.0,
        'long_win_rate': _pct((long_exits['pnl'] > 0).mean()) if len(long_exits) else 0.0,
        'short_win_rate': _pct((short_exits['pnl'] > 0).mean()) if len(short_exits) else 0.0,
        'avg_pnl': round(float(exits['pnl'].mean()), 4) if len(exits) else 0.0,
        'avg_win': round(avg_win, 4),
        'avg_loss': round(avg_loss, 4),
        'avg_profit_loss_ratio': round(float(ratio), 4) if ratio != float('inf') else 'inf',
        'profit_factor': round(float(pf), 4) if pf != float('inf') else 'inf',
        'max_consecutive_losses': max_consecutive_losses(exits['pnl'].tolist()) if len(exits) else 0,
    }

def max_consecutive_losses(pnls):
    cur = mx = 0
    for p in pnls:
        cur = cur + 1 if p < 0 else 0
        mx = max(mx, cur)
    return mx
