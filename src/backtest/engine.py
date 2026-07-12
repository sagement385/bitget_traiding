import json
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from src.models import Trade
from src.backtest.metrics import summarize
from src.backtest.dataset import build_backtest_dataset
from src.markets import CRYPTO, normalize_market_type


class BacktestEngine:
    def __init__(self, candles: pd.DataFrame, strategy, initial_cash=10000.0, fee_rate=0.0006, slippage_rate=0.0002, leverage=1.0, allow_short=False, symbol='BTCUSDT', category='USDT-FUTURES', market_type=CRYPTO):
        interval = str(candles['interval'].iloc[0]) if 'interval' in candles.columns and len(candles) else '1H'
        self.market_type = normalize_market_type(market_type, category)
        self.dataset = build_backtest_dataset(
            candles,
            symbol=symbol,
            category=category,
            interval=interval,
            market_type=self.market_type,
        )
        self.df = self.dataset.candles.copy(deep=True)
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.leverage = leverage
        self.allow_short = allow_short
        self.symbol = symbol
        self.pos = 0
        self.qty = 0.0
        self.entry_price = 0.0
        self.position_meta = {}
        self.trades = []
        self.equity = []
        self.trade_levels = []
        self._active_level = None

    def _mark_equity(self, price, ts):
        unreal = 0 if self.pos == 0 else self.qty * (price - self.entry_price) * self.pos
        eq = self.cash + unreal
        self.equity.append({'timestamp': int(ts), 'equity': eq, 'cash': self.cash, 'position': self.pos, 'price': price})
        return eq

    def _trade_dict(self, ts, side, qty, price, fee, slippage, pnl, reason, position_side='', event_type=''):
        d = Trade(int(ts), self.symbol, side, float(qty), float(price), float(fee), float(slippage), float(pnl), reason, float(self.cash)).__dict__
        d['position_side'] = position_side
        d['event_type'] = event_type
        d['equity_after'] = float(self.cash)
        return d

    def _close_fraction(self, price, ts, reason, fraction=1.0):
        if self.pos == 0 or self.qty <= 0:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        close_qty = self.qty * fraction
        exec_price = price * (1 - self.slippage_rate * self.pos)
        notional = abs(close_qty * exec_price)
        fee = notional * self.fee_rate
        pnl = close_qty * (exec_price - self.entry_price) * self.pos - fee
        self.cash += pnl
        position_side = 'long' if self.pos == 1 else 'short'
        self.trades.append(self._trade_dict(
            ts,
            'sell' if self.pos == 1 else 'buy',
            close_qty,
            exec_price,
            fee,
            abs(price - exec_price),
            pnl,
            reason,
            position_side=position_side,
            event_type='partial_exit' if fraction < 0.999 else 'exit',
        ))
        self.qty -= close_qty
        if self.qty <= 1e-12:
            if self._active_level is not None:
                self._active_level['end_ts'] = int(ts)
                self.trade_levels.append(self._active_level)
                self._active_level = None
            self.pos = 0
            self.qty = 0.0
            self.entry_price = 0.0
            self.position_meta = {}

    def _close(self, price, ts, reason):
        self._close_fraction(price, ts, reason, 1.0)

    def _open(self, target, price, ts, reason, metadata=None):
        if target == 0:
            return
        metadata = metadata or {}
        exec_price = price * (1 + self.slippage_rate * target)
        notional = self.cash * self.leverage
        self.qty = notional / exec_price if exec_price else 0.0
        fee = notional * self.fee_rate
        self.cash -= fee
        self.entry_price = exec_price
        self.pos = target
        stop = metadata.get('initial_stop')
        if stop is None:
            stop = exec_price * (0.99 if target == 1 else 1.01)
        risk = abs(exec_price - float(stop))
        if risk <= 0:
            risk = exec_price * 0.005
            stop = exec_price - risk if target == 1 else exec_price + risk
        self.position_meta = {
            **metadata,
            'initial_stop': float(stop),
            'active_stop': float(stop),
            'initial_risk': float(risk),
            'partial_taken': False,
            'max_r': 0.0,
        }
        position_side = 'long' if target == 1 else 'short'
        self.trades.append(self._trade_dict(
            ts,
            'buy' if target == 1 else 'sell',
            self.qty,
            exec_price,
            fee,
            abs(price - exec_price),
            -fee,
            reason,
            position_side=position_side,
            event_type='entry',
        ))
        self._active_level = {
            'start_ts': int(ts),
            'end_ts': None,
            'side': position_side,
            'entry': float(exec_price),
            'stop': float(stop),
            'target1': float(exec_price + target * risk),
            'target2': float(exec_price + target * risk * 2.0),
            'event_ts': metadata.get('event_ts'),
            'event_high': metadata.get('event_high'),
            'event_low': metadata.get('event_low'),
            'setup': metadata.get('setup', reason),
        }

    def _trail_reference(self, i: int, direction: int) -> float | None:
        lookback = int(self.position_meta.get('trail_swing_lookback') or 5)
        ema_n = int(self.position_meta.get('trail_ema') or 21)
        hist = self.df.iloc[max(0, i - max(ema_n * 3, lookback * 2)):i + 1]
        if hist.empty:
            return None
        close = pd.to_numeric(hist['close'], errors='coerce')
        ema = close.ewm(span=ema_n, adjust=False).mean().iloc[-1]
        if direction == 1:
            swing = pd.to_numeric(hist['low'].tail(lookback), errors='coerce').min()
            return float(max(swing, ema))
        swing = pd.to_numeric(hist['high'].tail(lookback), errors='coerce').max()
        return float(min(swing, ema))

    def _manage_position(self, row, i: int):
        if self.pos == 0 or not self.position_meta.get('r_management'):
            return
        ts = int(row['timestamp'])
        high = float(row['high'])
        low = float(row['low'])
        close = float(row['close'])
        risk = float(self.position_meta.get('initial_risk') or 0)
        if risk <= 0:
            return
        stop = float(self.position_meta.get('active_stop'))
        direction = self.pos

        # Existing stop first: conservative OHLC assumption.
        if direction == 1 and low <= stop:
            self._close(stop, ts, 'stop_or_trailing_stop')
            return
        if direction == -1 and high >= stop:
            self._close(stop, ts, 'stop_or_trailing_stop')
            return

        if direction == 1:
            r_now = (high - self.entry_price) / risk
        else:
            r_now = (self.entry_price - low) / risk
        self.position_meta['max_r'] = max(float(self.position_meta.get('max_r') or 0), float(r_now))

        if r_now >= float(self.position_meta.get('breakeven_r') or 1.0):
            stop = max(stop, self.entry_price) if direction == 1 else min(stop, self.entry_price)
        if r_now >= float(self.position_meta.get('lock_r') or 1.5):
            lock = float(self.position_meta.get('lock_profit_r') or 0.5)
            locked_stop = self.entry_price + direction * risk * lock
            stop = max(stop, locked_stop) if direction == 1 else min(stop, locked_stop)
        if r_now >= float(self.position_meta.get('partial_take_r') or 2.0) and not self.position_meta.get('partial_taken'):
            partial_price = self.entry_price + direction * risk * float(self.position_meta.get('partial_take_r') or 2.0)
            self._close_fraction(partial_price, ts, 'partial_take_2R', 0.5)
            if self.pos == 0:
                return
            self.position_meta['partial_taken'] = True
        if r_now >= float(self.position_meta.get('trail_after_r') or 2.0):
            ref = self._trail_reference(i, direction)
            if ref is not None:
                stop = max(stop, ref) if direction == 1 else min(stop, ref)
        self.position_meta['active_stop'] = float(stop)
        if self._active_level is not None:
            self._active_level['stop'] = float(stop)

        # If close has clearly lost the dynamic reference, exit at close.
        if direction == 1 and close <= stop:
            self._close(close, ts, 'close_below_trailing_reference')
        elif direction == -1 and close >= stop:
            self._close(close, ts, 'close_above_trailing_reference')

    def run(self, out_dir='results/backtests'):
        if self.df.empty or len(self.df) < 3:
            raise ValueError('Backtest needs at least 3 candle rows. Download/create non-empty data first.')
        required = {'timestamp', 'open', 'high', 'low', 'close'}
        missing = required - set(self.df.columns)
        if missing:
            raise ValueError(f'CSV is missing candle columns: {sorted(missing)}')
        for i in range(2, len(self.df)):
            # The decision may inspect only completed candles through i-1.
            # Row i is the next bar, used solely for open-price execution and
            # subsequent intrabar stop/target simulation.
            hist = self.df.iloc[:i].copy(deep=True)
            row = self.df.loc[i]
            price = float(row['open'])
            ts = int(row['timestamp'])
            pos_state = {
                'target_position': self.pos,
                'entry_price': self.entry_price,
                'qty': self.qty,
                'cash': self.cash,
                'metadata': self.position_meta,
            }
            sig = self.strategy.generate(hist, position=pos_state)
            target = int(sig.target_position)
            if not self.allow_short and target < 0:
                target = 0
            if target != self.pos:
                self._close(price, ts, f'exit_to_{target}:{sig.reason}')
                self._open(target, price, ts, sig.reason, getattr(sig, 'metadata', None) or {})
            self._manage_position(row, i)
            self._mark_equity(float(row['close']), ts)
        if self.pos != 0:
            last = self.df.iloc[-1]
            self._close(float(last['close']), int(last['timestamp']), 'final_close')
            self._mark_equity(float(last['close']), int(last['timestamp']))
        eq = pd.DataFrame(self.equity)
        tr = pd.DataFrame(self.trades)
        metrics = summarize(eq, tr, self.initial_cash, interval=self.dataset.metadata["interval"], market_type=self.market_type)
        self.save_outputs(eq, tr, metrics, out_dir)
        return {'equity_curve': eq, 'trades': tr, 'metrics': metrics, 'trade_levels': self.trade_levels, 'dataset': self.dataset.metadata}

    def save_outputs(self, eq, tr, metrics, out_dir):
        p = Path(out_dir) / self.dataset.metadata['dataset_id'][:16]
        p.mkdir(parents=True, exist_ok=True)
        eq.to_csv(p / 'equity_curve.csv', index=False)
        tr.to_csv(p / 'trades.csv', index=False)
        (p / 'metrics.json').write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
        (p / 'trade_levels.json').write_text(json.dumps(self.trade_levels, indent=2, ensure_ascii=False))
        (p / 'dataset.json').write_text(json.dumps(self.dataset.metadata, indent=2, ensure_ascii=False))
        if len(eq):
            plt.figure(figsize=(10, 4))
            plt.plot(pd.to_datetime(eq['timestamp'], unit='ms'), eq['equity'])
            plt.title('Equity Curve')
            plt.tight_layout()
            plt.savefig(p / 'equity_curve.png')
            plt.close()
