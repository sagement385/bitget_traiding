import time, json
from pathlib import Path
import pandas as pd
from src.bitget.public_client import BitgetPublicClient, normalize_bitget_candles, BITGET_GRAN
from src.data_engine.canonical import CANONICAL_INTERVALS, load_or_fetch_canonical_candles
from src.strategies.factory import make_strategy
from src.models import OrderRequest
from src.execution.paper_broker import PaperBroker
from src.execution.live_bitget_broker import LiveBitgetBroker
from src.execution.live_toss_broker import LiveTossBroker
from src.execution.risk_manager import RiskConfig, RiskManager
from src.utils.time import INTERVAL_MS
from src.markets import CRYPTO, KOR_STOCK, US_STOCK, normalize_market_type

class PollingRealtimeEngine:
    """REST polling 기반 실시간 엔진.

    WebSocket이 실패해도 이 루프로 API 키 기반 paper/demo/live 운용 가능.
    캔들 마감 후 1회만 전략을 실행한다.
    """
    def __init__(self, *, symbol='BTCUSDT', product_type='USDT-FUTURES', interval='1m', strategy='trend_pullback',
                 strategy_kwargs=None, mode='paper', broker=None, poll_sec=10, window=300, out_dir='results/realtime', market_type=CRYPTO):
        self.symbol=symbol; self.product_type=product_type; self.interval=interval; self.mode=mode
        self.market_type = normalize_market_type(market_type, product_type)
        self.client=BitgetPublicClient(); self.poll_sec=poll_sec; self.window=window
        self.strategy = make_strategy(strategy, **({'symbol': symbol} | (strategy_kwargs or {})))
        if broker is not None:
            self.broker = broker
        elif mode == 'paper':
            self.broker = PaperBroker()
        elif self.market_type == CRYPTO:
            self.broker = LiveBitgetBroker(safe_mode=True, mode=mode)
        else:
            self.broker = LiveTossBroker(market_type=self.market_type, safe_mode=True)
        self.last_closed_ts = None
        self.position = 0
        self.out_dir = Path(out_dir); self.out_dir.mkdir(parents=True, exist_ok=True)

    def fetch_recent(self):
        now = int(time.time()*1000)
        start = now - INTERVAL_MS[self.interval] * self.window
        if self.interval in CANONICAL_INTERVALS:
            # The decision engine reads the same persisted canonical dataset as
            # backtests. Provider REST is used only to patch missing/recent 1m
            # rows, never as a chart feed or an independent strategy input.
            df, _ = load_or_fetch_canonical_candles(
                self.symbol,
                self.product_type,
                self.interval,
                start,
                now,
                limit=self.window,
                fetch=True,
                persist_derived=True,
                market_type=self.market_type,
            )
            return df
        if self.market_type != CRYPTO:
            raise ValueError('Stock realtime execution supports 1m and larger canonical intervals')
        rows = self.client.get_candles(self.symbol, self.product_type, BITGET_GRAN[self.interval], start, now)
        return normalize_bitget_candles(rows, self.symbol, self.product_type, self.interval)

    def run_once(self):
        df = self.fetch_recent()
        if len(df) < 3:
            return {'status':'no_data'}
        # Bitget candle close data only finished for historical/candles response; safest: use penultimate candle.
        closed = df.iloc[:-1].copy() if len(df) > 1 else df
        ts = int(closed['timestamp'].iloc[-1])
        if self.last_closed_ts == ts:
            return {'status':'already_processed', 'timestamp': ts}
        sig = self.strategy.generate(closed)
        action = {'status':'signal', 'timestamp':ts, 'signal':sig.__dict__}
        if sig.target_position != self.position:
            side = 'buy' if sig.target_position > self.position else 'sell'
            qty = 0.001  # 기본 안전 수량. 실제 운용은 config/CLI에서 조정 권장.
            order = OrderRequest(symbol=self.symbol, side=side, qty=qty, order_type='market', reduce_only=(sig.target_position==0))
            action['order'] = self.broker.place_order(order)
            self.position = sig.target_position
        self.last_closed_ts = ts
        with open(self.out_dir/'events.jsonl','a',encoding='utf-8') as f:
            f.write(json.dumps(action, ensure_ascii=False, default=str)+'\n')
        return action

    def run_forever(self):
        while True:
            try:
                print(json.dumps(self.run_once(), ensure_ascii=False, default=str))
            except Exception as e:
                print(json.dumps({'status':'error','error':str(e)}, ensure_ascii=False))
            time.sleep(self.poll_sec)
