import argparse, json
from src.data_engine.historical_loader import download_to_csv
from src.data_engine.demo_data import make_demo_candles
from src.data_engine.storage import init_db, save_csv, load_csv
from src.data_engine.validator import validate_candles
from src.strategies.factory import make_strategy
from src.backtest.engine import BacktestEngine


def add_strategy_args(parser):
    parser.add_argument('--strategy', default='sma_cross', choices=['sma_cross','rsi','rsi_reversal','breakout','scalp_vwap_rsi','scalp','price_action_volume','pa_volume','multi_timeframe_momentum','mtf_momentum'])
    parser.add_argument('--fast', type=int, default=20)
    parser.add_argument('--slow', type=int, default=60)
    parser.add_argument('--rsi-period', type=int, default=14)
    parser.add_argument('--rsi-lower', type=float, default=30)
    parser.add_argument('--rsi-upper', type=float, default=70)
    parser.add_argument('--breakout-window', type=int, default=20)
    parser.add_argument('--min-rel-volume', type=float, default=0.80)
    parser.add_argument('--min-atr-pct', type=float, default=0.03)
    parser.add_argument('--stop-atr', type=float, default=1.20)
    parser.add_argument('--take-atr', type=float, default=1.80)
    parser.add_argument('--allow-short', action='store_true')
    parser.add_argument('--volume-mult', type=float, default=1.8)
    parser.add_argument('--pullback-bars', type=int, default=13)


def build_strategy_kwargs(a, symbol):
    kwargs={'symbol': symbol, 'allow_short': getattr(a, 'allow_short', False)}
    if a.strategy == 'sma_cross':
        kwargs.update({'fast': a.fast, 'slow': a.slow})
    elif a.strategy in ('rsi','rsi_reversal'):
        kwargs.update({'period': a.rsi_period, 'lower': a.rsi_lower, 'upper': a.rsi_upper})
    elif a.strategy == 'breakout':
        kwargs.update({'window': a.breakout_window})
    elif a.strategy in ('scalp_vwap_rsi','scalp'):
        kwargs.update({'min_rel_volume': a.min_rel_volume, 'min_atr_pct': a.min_atr_pct, 'stop_atr': a.stop_atr, 'take_atr': a.take_atr})
    elif a.strategy in ('price_action_volume','pa_volume'):
        kwargs.update({'volume_mult': a.volume_mult, 'pullback_bars': a.pullback_bars})
    elif a.strategy in ('multi_timeframe_momentum','mtf_momentum'):
        kwargs.update({
            'min_rel_volume': a.min_rel_volume,
            'min_atr_pct': a.min_atr_pct,
            'stop_atr': a.stop_atr,
            'take_r': a.take_atr,
        })
    return kwargs


def main():
    p=argparse.ArgumentParser(prog='bitget-quant')
    sub=p.add_subparsers(dest='cmd', required=True)

    d=sub.add_parser('download')
    d.add_argument('--symbol',required=True); d.add_argument('--category',default='USDT-FUTURES')
    d.add_argument('--interval',default='1H'); d.add_argument('--start',required=True); d.add_argument('--end',required=True); d.add_argument('--out',required=True); d.add_argument('--limit', type=int, default=1000, help='default max saved candles; use a large value for full backfill')

    bf=sub.add_parser('backfill-year')
    bf.add_argument('--symbol', default='BTCUSDT')
    bf.add_argument('--category', default='USDT-FUTURES')
    bf.add_argument('--year', type=int, required=True)
    bf.add_argument('--out-dir', default='data/raw')

    full=sub.add_parser('backfill-full')
    full.add_argument('--symbol', default='BTCUSDT')
    full.add_argument('--category', default='USDT-FUTURES')
    full.add_argument('--start', default=None, help='default uses a BTCUSDT listing-era start')
    full.add_argument('--end', default=None, help='default is now')
    full.add_argument('--out-dir', default='data/raw')
    full.add_argument('--derive', action='store_true', help='kept for compatibility; derived intervals are enabled by default')
    full.add_argument('--no-derive', action='store_true', help='only backfill canonical 1m candles')

    dd=sub.add_parser('demo-data')
    dd.add_argument('--symbol',default='BTCUSDT'); dd.add_argument('--category',default='USDT-FUTURES'); dd.add_argument('--interval',default='1H'); dd.add_argument('--out',default='data/raw/BTCUSDT_1H.csv')

    b=sub.add_parser('backtest')
    b.add_argument('--csv',required=True); add_strategy_args(b)
    b.add_argument('--initial-cash',type=float,default=10000); b.add_argument('--fee-rate',type=float,default=0.0006); b.add_argument('--slippage-rate',type=float,default=0.0002)

    acc=sub.add_parser('account')
    acc.add_argument('--symbol',default='BTCUSDT'); acc.add_argument('--product-type',default='USDT-FUTURES'); acc.add_argument('--margin-coin',default='USDT')

    pos=sub.add_parser('positions')
    pos.add_argument('--product-type',default='USDT-FUTURES'); pos.add_argument('--margin-coin',default='USDT')

    rt=sub.add_parser('paper')
    rt.add_argument('--symbol',default='BTCUSDT'); rt.add_argument('--product-type',default='USDT-FUTURES'); rt.add_argument('--market-type',default='CRYPTO',choices=['CRYPTO','KOR_STOCK','US_STOCK']); rt.add_argument('--interval',default='1m'); rt.add_argument('--poll-sec',type=int,default=10); add_strategy_args(rt); rt.add_argument('--once',action='store_true')

    demo=sub.add_parser('demo')
    demo.add_argument('--symbol',default='BTCUSDT'); demo.add_argument('--product-type',default='USDT-FUTURES'); demo.add_argument('--market-type',default='CRYPTO',choices=['CRYPTO','KOR_STOCK','US_STOCK']); demo.add_argument('--interval',default='1m'); demo.add_argument('--poll-sec',type=int,default=10); add_strategy_args(demo); demo.add_argument('--once',action='store_true'); demo.add_argument('--i-understand-live',action='store_true')

    live=sub.add_parser('live')
    live.add_argument('--symbol',default='BTCUSDT'); live.add_argument('--product-type',default='USDT-FUTURES'); live.add_argument('--market-type',default='CRYPTO',choices=['CRYPTO','KOR_STOCK','US_STOCK']); live.add_argument('--interval',default='1m'); live.add_argument('--poll-sec',type=int,default=10); add_strategy_args(live); live.add_argument('--once',action='store_true'); live.add_argument('--i-understand-live',action='store_true')

    sub.add_parser('migrate', help='initialize or migrate the SQLite schema once')
    sub.add_parser('ui')
    a=p.parse_args()

    if a.cmd=='migrate':
        init_db()
        print('SQLite schema is ready.')
    elif a.cmd=='download':
        df=download_to_csv(a.symbol,a.category,a.interval,a.start,a.end,a.out, limit=a.limit); print(f'saved {len(df)} rows -> {a.out}')
    elif a.cmd=='backfill-year':
        from src.data_engine.backfill import backfill_year_minute_data
        rows = backfill_year_minute_data(a.symbol, a.category, a.year, out_dir=a.out_dir)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif a.cmd=='backfill-full':
        from src.data_engine.backfill import DERIVED_INTERVALS, backfill_full_history
        rows = backfill_full_history(
            a.symbol,
            a.category,
            start=a.start,
            end=a.end,
            out_dir=a.out_dir,
            derive_intervals=() if a.no_derive else DERIVED_INTERVALS,
            progress=True,
        )
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif a.cmd=='demo-data':
        df=make_demo_candles(a.symbol,a.category,a.interval); save_csv(df,a.out); print(f'saved {len(df)} rows -> {a.out}')
    elif a.cmd=='backtest':
        df=load_csv(a.csv); validate_candles(df, df.get('interval',['1H'])[0], strict=False)
        symbol=df['symbol'].iloc[0] if 'symbol' in df else 'BTCUSDT'
        strat=make_strategy(a.strategy, **build_strategy_kwargs(a, symbol))
        res=BacktestEngine(df,strat,a.initial_cash,a.fee_rate,a.slippage_rate,allow_short=a.allow_short,symbol=symbol).run()
        print(json.dumps(res['metrics'], indent=2, ensure_ascii=False))
    elif a.cmd=='account':
        from src.bitget.private_client import BitgetPrivateClient
        print(json.dumps(BitgetPrivateClient().get_account(a.symbol,a.product_type,a.margin_coin), indent=2, ensure_ascii=False))
    elif a.cmd=='positions':
        from src.bitget.private_client import BitgetPrivateClient
        print(json.dumps(BitgetPrivateClient().get_positions(a.product_type,a.margin_coin), indent=2, ensure_ascii=False))
    elif a.cmd in ('paper','demo','live'):
        from src.execution.realtime_engine import PollingRealtimeEngine
        from src.execution.live_bitget_broker import LiveBitgetBroker
        from src.execution.live_toss_broker import LiveTossBroker
        broker=None
        if a.cmd in ('demo','live'):
            if a.market_type == 'CRYPTO':
                broker=LiveBitgetBroker(product_type=a.product_type, safe_mode=not a.i_understand_live, mode=a.cmd)
            else:
                broker=LiveTossBroker(market_type=a.market_type, safe_mode=not a.i_understand_live)
        eng=PollingRealtimeEngine(symbol=a.symbol, product_type=a.product_type, interval=a.interval,
                                  strategy=a.strategy, strategy_kwargs=build_strategy_kwargs(a,a.symbol),
                                  mode=a.cmd, broker=broker, poll_sec=a.poll_sec, market_type=a.market_type)
        if a.once: print(json.dumps(eng.run_once(), indent=2, ensure_ascii=False, default=str))
        else: eng.run_forever()
    elif a.cmd=='ui':
        import uvicorn
        uvicorn.run('src.ui.app:app', host='127.0.0.1', port=8000, reload=False)
if __name__=='__main__': main()
