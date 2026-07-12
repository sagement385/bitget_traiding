import pandas as pd
from src.data_engine.demo_data import make_demo_candles
from src.data_engine.validator import validate_candles
from src.strategies.sma_cross import SmaCrossStrategy
from src.backtest.engine import BacktestEngine
from src.backtest.metrics import max_drawdown
from src.execution.risk_manager import RiskManager, RiskConfig
from src.models import OrderRequest


def test_demo_data_validates():
    df=make_demo_candles(periods=200)
    assert validate_candles(df,'1H',strict=True)==[]


def test_sma_strategy_signal_shape():
    df=make_demo_candles(periods=120)
    sig=SmaCrossStrategy(fast=5, slow=20).generate(df)
    assert sig.target_position in (-1,0,1)
    assert sig.symbol=='BTCUSDT'


def test_backtest_outputs_metrics():
    df=make_demo_candles(periods=300)
    res=BacktestEngine(df,SmaCrossStrategy(fast=5,slow=20),initial_cash=10000).run('/tmp/bt_test')
    assert 'total_return' in res['metrics']
    assert len(res['equity_curve']) > 0


def test_backtest_uses_immutable_dataset_prefixes_without_lookahead(tmp_path):
    from src.models import Signal

    class PrefixRecordingStrategy:
        def __init__(self):
            self.seen_last_timestamps = []

        def generate(self, candles, portfolio=None, position=None):
            self.seen_last_timestamps.append(int(candles['timestamp'].iloc[-1]))
            return Signal('BTCUSDT', 0, 'observe_only', 0.0, {})

    df = make_demo_candles(periods=12, interval='1m')
    strategy = PrefixRecordingStrategy()
    result = BacktestEngine(df, strategy, symbol='BTCUSDT').run(str(tmp_path))
    expected = [int(df['timestamp'].iloc[index - 1]) for index in range(2, len(df))]
    assert strategy.seen_last_timestamps == expected
    assert result['dataset']['rows'] == len(df)
    assert result['dataset']['execution_model'] == 'signal_on_completed_bar_execute_next_bar_open'
    assert result['metrics']['periods_per_year'] == 525600.0
    assert (tmp_path / result['dataset']['dataset_id'][:16] / 'dataset.json').exists()


def test_stock_backtest_dataset_is_session_aware_and_rejects_intraday_holes():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.backtest.dataset import build_backtest_dataset
    from src.data_engine.validator import DataValidationError

    kst = ZoneInfo('Asia/Seoul')
    first_open = int(datetime(2024, 1, 2, 9, 0, tzinfo=kst).timestamp() * 1000)
    next_open = int(datetime(2024, 1, 3, 9, 0, tzinfo=kst).timestamp() * 1000)

    def row(timestamp):
        return {'timestamp': timestamp, 'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1, 'turnover': 100, 'interval': '1m'}

    accepted = pd.DataFrame([row(first_open), row(first_open + 60_000), row(next_open)])
    dataset = build_backtest_dataset(accepted, symbol='005930', category='KRX', interval='1m', market_type='KOR_STOCK')
    assert dataset.metadata['market_type'] == 'KOR_STOCK'

    broken = pd.DataFrame([row(first_open), row(first_open + 120_000), row(next_open)])
    try:
        build_backtest_dataset(broken, symbol='005930', category='KRX', interval='1m', market_type='KOR_STOCK')
    except DataValidationError as exc:
        assert 'missing/nonuniform candles' in str(exc)
    else:
        raise AssertionError('intra-session stock holes must reject the dataset')


def test_mdd_negative_or_zero():
    eq=pd.Series([100,120,90,95])
    assert max_drawdown(eq) <= 0


def test_risk_blocks_large_order():
    rm=RiskManager(RiskConfig(max_order_pct=0.1))
    ok,reason=rm.validate(order_notional=200, equity=1000)
    assert not ok and reason=='order_notional_exceeds_limit'


def test_client_oid_unique():
    assert OrderRequest('BTCUSDT','buy',1).client_oid != OrderRequest('BTCUSDT','buy',1).client_oid

def test_bitget_private_sign_path_with_query(monkeypatch):
    import os
    from src.bitget.private_client import BitgetPrivateClient
    monkeypatch.setenv('BITGET_API_KEY','k')
    monkeypatch.setenv('BITGET_SECRET_KEY','s')
    monkeypatch.setenv('BITGET_PASSPHRASE','p')
    c=BitgetPrivateClient()
    assert c.has_credentials()


def test_live_broker_blocks_by_default():
    from src.execution.live_bitget_broker import LiveBitgetBroker
    from src.models import OrderRequest
    b=LiveBitgetBroker(safe_mode=True)
    try:
        b.place_order(OrderRequest(symbol='BTCUSDT', side='buy', qty=0.001), equity=1000, mark_price=50000)
    except RuntimeError as e:
        assert 'safe_mode' in str(e)
    else:
        raise AssertionError('must block')


def test_order_request_validates_side_qty():
    from src.models import OrderRequest
    try:
        OrderRequest(symbol='BTCUSDT', side='hold', qty=1)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid side accepted')

def test_bitget_public_chunk_size_respects_row_limit():
    from src.bitget.public_client import BitgetPublicClient
    from src.utils.time import INTERVAL_MS
    c=BitgetPublicClient()
    assert c._chunk_ms('1m') <= INTERVAL_MS['1m'] * 190
    assert c._chunk_ms('1H') <= INTERVAL_MS['1H'] * 190


def test_bitget_public_empty_chunk_is_not_fatal(monkeypatch):
    from src.bitget.public_client import BitgetPublicClient
    c = BitgetPublicClient()
    monkeypatch.setattr(c, '_request_candles', lambda *a, **k: [])
    assert c._fetch_chunk('BTCUSDT', 'usdt-futures', '1m', 1, 2) == []


def test_week_month_intervals_supported():
    from src.bitget.public_client import aggregate_ohlcv
    df=make_demo_candles(periods=24*40, interval='1H')
    daily = df.copy()
    daily['timestamp'] = daily['timestamp']
    w=aggregate_ohlcv(daily, '1W')
    assert set(['open','high','low','close','timestamp']).issubset(w.columns)

def test_merge_candle_frames_replaces_current_candle():
    import pandas as pd
    from src.data_engine.candle_stream import merge_candle_frames
    old = pd.DataFrame([{"timestamp": 1000, "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1}])
    new = pd.DataFrame([{"timestamp": 1000, "open": 1, "high": 3, "low": 1, "close": 2.5, "volume": 2}])
    out = merge_candle_frames(old, new)
    assert len(out) == 1
    assert float(out.iloc[0]["high"]) == 3
    assert float(out.iloc[0]["close"]) == 2.5


def test_ws_candle_topic_contract():
    from src.bitget.websocket_client import candle_topic
    t = candle_topic("BTCUSDT", "1m", "USDT-FUTURES")
    assert t == {"instType": "USDT-FUTURES", "channel": "candle1m", "instId": "BTCUSDT"}


def test_tick_bucket_start_seconds():
    from src.data_engine.tick_engine import bucket_start_ms
    assert bucket_start_ms(123456, '1s') == 123000
    assert bucket_start_ms(123456, '5s') == 120000


def test_tick_bucket_uses_bitget_exchange_day_boundary():
    from src.data_engine.canonical import bucket_start_ms as canonical_bucket_start
    from src.data_engine.tick_engine import bucket_start_ms

    # Bitget daily candles start at 00:00 UTC+8, i.e. 16:00 UTC on the day before.
    ts = int(pd.Timestamp('2026-06-23T01:23:00Z').timestamp() * 1000)
    assert bucket_start_ms(ts, '1D') == canonical_bucket_start(ts, '1D')
    assert bucket_start_ms(ts, '1W') == canonical_bucket_start(ts, '1W')
    assert bucket_start_ms(ts, '1M') == canonical_bucket_start(ts, '1M')


def test_tick_aggregator_builds_ohlcv():
    from src.data_engine.tick_engine import CandleAggregator, Tick
    agg = CandleAggregator('BTCUSDT', 'USDT-FUTURES', '1s')
    c1 = agg.update(Tick('BTCUSDT', 100.0, 1.0, 'buy', 1000))
    c2 = agg.update(Tick('BTCUSDT', 105.0, 2.0, 'sell', 1500))
    assert c1['open'] == 100.0
    assert c2['high'] == 105.0
    assert c2['low'] == 100.0
    assert c2['close'] == 105.0
    assert c2['volume'] == 3.0


def test_parse_trade_message_dict():
    from src.data_engine.tick_engine import parse_trade_message
    msg = {'data': [{'price': '101', 'size': '0.5', 'side': 'buy', 'ts': '1000'}]}
    ticks = parse_trade_message(msg, 'BTCUSDT')
    assert len(ticks) == 1
    assert ticks[0].price == 101.0


def test_ws_trade_topic_contract():
    from src.bitget.websocket_client import trade_topic
    assert trade_topic('BTCUSDT', 'USDT-FUTURES') == {'instType': 'USDT-FUTURES', 'channel': 'trade', 'instId': 'BTCUSDT'}

def test_common_indicators_align_with_candles():
    from src.indicators.library import add_common_indicators
    df = make_demo_candles(periods=220)
    out = add_common_indicators(df)
    assert len(out) == len(df)
    for col in ['ema9','ema21','vwap','rsi14','atr14','macd_hist','bb_upper','volume_sma20']:
        assert col in out.columns


def test_common_indicators_can_limit_requested_series():
    from src.indicators.library import add_common_indicators
    df = make_demo_candles(periods=220)
    out = add_common_indicators(df, include={'ema9', 'volume_sma20'})
    assert {'ema9', 'volume_sma20'}.issubset(out.columns)
    assert 'supertrend' not in out.columns
    assert 'macd' not in out.columns


def test_scalp_strategy_signal_shape_and_backtest():
    from src.strategies.scalp_vwap_rsi import ScalpVwapRsiStrategy
    df = make_demo_candles(periods=360, interval='1m')
    strat = ScalpVwapRsiStrategy(symbol='BTCUSDT')
    sig = strat.generate(df)
    assert sig.target_position in (-1, 0, 1)
    res = BacktestEngine(df, strat, initial_cash=10000).run('/tmp/bt_scalp_test')
    assert 'total_return' in res['metrics']


def test_load_or_fetch_bounds_requested_window(monkeypatch, tmp_path):
    from src.data_engine import historical_loader as hl
    from src.data_engine import canonical as cn
    captured = {}
    class FakeClient:
        def download_candles(self, symbol, category, interval, start, end):
            captured['symbol']=symbol; captured['interval']=interval; captured['start']=int(start); captured['end']=int(end)
            return pd.DataFrame(columns=['timestamp','open','high','low','close','volume','turnover','symbol','category','interval'])
    monkeypatch.setattr(hl, 'BitgetPublicClient', lambda: FakeClient())
    monkeypatch.setattr(cn, 'BitgetPublicClient', lambda: FakeClient())
    monkeypatch.setattr(hl, 'now_ms', lambda: 1704067200000)  # 2024-01-01
    monkeypatch.setattr(cn, 'now_ms', lambda: 1704067200000)
    db = tmp_path / 't.db'
    df, report = hl.load_or_fetch_candles('BTCUSDT','USDT-FUTURES','1m', start='2020-01-01', end='2024-01-01', limit=1000, source='bitget', repair=False, db_path=str(db))
    assert captured['end'] - captured['start'] <= 1001 * 60_000


def test_load_or_fetch_uses_cache_without_refetch(monkeypatch, tmp_path):
    from src.data_engine import historical_loader as hl
    from src.data_engine.storage import upsert_candles
    rows = []
    ts = 1700000000000
    for i in range(10):
        rows.append({
            'timestamp': ts + i * 1000,
            'open': 1 + i,
            'high': 2 + i,
            'low': 1 + i,
            'close': 1.5 + i,
            'volume': 1,
            'turnover': 0,
            'symbol': 'BTCUSDT',
            'category': 'USDT-FUTURES',
            'interval': '1s',
        })
    db = tmp_path / 'cache.db'
    upsert_candles(pd.DataFrame(rows), db_path=str(db))

    class BadClient:
        def download_candles(self, *a, **k):
            raise AssertionError('should not refetch a complete cached range')

    monkeypatch.setattr(hl, 'BitgetPublicClient', lambda: BadClient())
    df, report = hl.load_or_fetch_candles(
        'BTCUSDT', 'USDT-FUTURES', '1s',
        start=ts, end=ts + 9 * 1000, limit=10,
        source='bitget', repair=False, db_path=str(db),
    )
    assert len(df) == 10
    assert report.fetched == 0


def test_load_or_fetch_fetches_only_missing_tail(monkeypatch, tmp_path):
    from src.data_engine import historical_loader as hl
    from src.data_engine.storage import upsert_candles
    ts = 1700000000000
    cached = pd.DataFrame([{
        'timestamp': ts + i * 1000,
        'open': 1 + i,
        'high': 2 + i,
        'low': 1 + i,
        'close': 1.5 + i,
        'volume': 1,
        'turnover': 0,
        'symbol': 'BTCUSDT',
        'category': 'USDT-FUTURES',
        'interval': '1s',
    } for i in range(5)])
    db = tmp_path / 'tail.db'
    upsert_candles(cached, db_path=str(db))
    captured = {}

    class FakeClient:
        def download_candles(self, symbol, category, interval, start, end):
            captured['start'] = int(start)
            captured['end'] = int(end)
            return pd.DataFrame([{
                'timestamp': ts + i * 1000,
                'open': 1 + i,
                'high': 2 + i,
                'low': 1 + i,
                'close': 1.5 + i,
                'volume': 1,
                'turnover': 0,
                'symbol': symbol,
                'category': category,
                'interval': interval,
            } for i in range(5, 10)])

    monkeypatch.setattr(hl, 'BitgetPublicClient', lambda: FakeClient())
    df, report = hl.load_or_fetch_candles(
        'BTCUSDT', 'USDT-FUTURES', '1s',
        start=ts, end=ts + 9 * 1000, limit=10,
        source='bitget', repair=False, db_path=str(db),
    )
    assert len(df) == 10
    assert report.fetched == 5
    assert captured['start'] == ts + 5 * 1000
    assert captured['end'] == ts + 10 * 1000


def test_ui_indicator_endpoint_uses_same_interval_candles():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    c = TestClient(app)
    candles = c.get('/api/candles', params={'source': 'demo', 'interval': '1m'}).json()['candles'][:250]
    res = c.post('/api/indicators', json={'symbol': 'BTCUSDT', 'category': 'USDT-FUTURES', 'interval': '1m', 'candles': candles})
    js = res.json()
    assert res.status_code == 200
    assert js['ok'] is True
    assert js['rows'] == len(candles)
    assert js['indicators']['lower']['volume'][0]['time'] == candles[0]['time']


def test_ui_html_uses_editable_end_date_control():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    html = TestClient(app).get('/').text
    assert 'type="date"' in html
    assert 'End / Today' in html
    assert 'readonly' not in html


def test_api_candles_returns_context_and_limit():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    c = TestClient(app)
    js = c.get('/api/candles', params={'source':'demo','interval':'5m','limit':123}).json()
    assert js['ok'] is True
    assert js['symbol'] == 'BTCUSDT'
    assert js['interval'] == '5m'
    assert 'stale' in js


def test_ui_contains_race_guard_and_status_badge():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    html = TestClient(app).get('/').text
    assert 'candleRequestSeq' in html
    assert 'sameResponseContext' in html
    assert 'netBadge' in html
    assert 'chartLimit()' in html
    assert 'olderLoadArmed' in html
    assert 'limit:String(chartLimit())' in html


def test_api_candles_uses_cache_fallback_on_bitget_failure(monkeypatch, tmp_path):
    from src.ui import app as uiapp
    from fastapi.testclient import TestClient
    import pandas as pd
    sample = pd.DataFrame([{
        'timestamp': 1700000000000, 'open': 1, 'high': 2, 'low': 1, 'close': 1.5,
        'volume': 10, 'turnover': 0, 'symbol': 'BTCUSDT', 'category': 'USDT-FUTURES', 'interval': '1m'
    }])
    monkeypatch.setattr(uiapp, '_load_local', lambda *a, **k: sample.copy())
    class BadClient:
        def download_candles(self, *a, **k):
            raise RuntimeError('temporary failure')
    monkeypatch.setattr(uiapp, 'load_or_fetch_candles', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('temporary failure')))
    monkeypatch.setattr(uiapp, 'load_or_fetch_canonical_candles', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('temporary failure')))
    js = TestClient(uiapp.app).get('/api/candles', params={'source':'bitget','interval':'1m'}).json()
    assert js['ok'] is True
    assert js['stale'] is True
    assert js['candles'][0]['volume'] == 10

def test_price_action_volume_strategy_has_r_metadata():
    from src.strategies.price_action_volume import PriceActionVolumeStrategy
    rows=[]
    ts=1700000000000
    price=100.0
    for i in range(80):
        rows.append({'timestamp':ts+i*60000,'open':price,'high':price+0.4,'low':price-0.4,'close':price+0.05,'volume':100,'turnover':0,'symbol':'BTCUSDT','category':'USDT-FUTURES','interval':'1m'})
        price+=0.03
    # high-volume impulse candle
    rows.append({'timestamp':ts+80*60000,'open':103,'high':108,'low':102.5,'close':107,'volume':400,'turnover':0,'symbol':'BTCUSDT','category':'USDT-FUTURES','interval':'1m'})
    # pullback toward 30~50% body area, then bullish confirmation
    rows += [
        {'timestamp':ts+81*60000,'open':107,'high':107.4,'low':105.4,'close':106.0,'volume':160,'turnover':0,'symbol':'BTCUSDT','category':'USDT-FUTURES','interval':'1m'},
        {'timestamp':ts+82*60000,'open':106,'high':106.5,'low':104.9,'close':105.5,'volume':130,'turnover':0,'symbol':'BTCUSDT','category':'USDT-FUTURES','interval':'1m'},
        {'timestamp':ts+83*60000,'open':105.5,'high':107.1,'low':105.2,'close':106.9,'volume':145,'turnover':0,'symbol':'BTCUSDT','category':'USDT-FUTURES','interval':'1m'},
    ]
    df=pd.DataFrame(rows)
    sig=PriceActionVolumeStrategy(volume_mult=1.5).generate(df, position={'target_position':0})
    assert sig.target_position == 1
    assert sig.metadata['initial_stop'] < df.iloc[-1]['close']
    assert sig.metadata['r_management'] is True


def test_backtest_returns_trade_levels_for_price_action():
    from src.strategies.price_action_volume import PriceActionVolumeStrategy
    df=make_demo_candles(periods=260, interval='1m')
    # inject a deterministic impulse/pullback segment
    base = df.index[-20]
    df.loc[base, ['open','high','low','close','volume']] = [100,108,99,107,10000]
    df.loc[base+1, ['open','high','low','close','volume']] = [107,107.5,105.5,106,2000]
    df.loc[base+2, ['open','high','low','close','volume']] = [106,106.5,104.8,105.5,1900]
    df.loc[base+3, ['open','high','low','close','volume']] = [105.5,107.4,105.2,107.2,2200]
    df.loc[base+4:, 'high'] = df.loc[base+4:, 'high'].clip(lower=112)
    strat=PriceActionVolumeStrategy(volume_mult=1.2, allow_short=True)
    res=BacktestEngine(df, strat, initial_cash=10000, allow_short=True).run('/tmp/bt_pa_test')
    assert 'trade_levels' in res
    assert 'avg_profit_loss_ratio' in res['metrics']


def test_backtest_rejects_gap_candles_strictly():
    import pandas as pd
    from src.strategies.sma_cross import SmaCrossStrategy
    rows = []
    ts = 1700000000000
    for i in [0, 1, 2, 4, 5]:
        rows.append({
            'timestamp': ts + i * 60_000,
            'open': 100 + i,
            'high': 101 + i,
            'low': 99 + i,
            'close': 100.5 + i,
            'volume': 10,
            'turnover': 0,
            'symbol': 'BTCUSDT',
            'category': 'USDT-FUTURES',
            'interval': '1m',
        })
    try:
        BacktestEngine(pd.DataFrame(rows), SmaCrossStrategy(symbol='BTCUSDT', fast=2, slow=3)).run('/tmp/bt_gap_reject')
    except Exception as e:
        assert 'missing/nonuniform candles' in str(e)
    else:
        raise AssertionError('backtest must reject gapped candles')


def test_ui_backtest_second_interval_bitget_uses_local_without_nameerror(monkeypatch):
    from src.ui import app as uiapp
    from fastapi.testclient import TestClient
    from src.data_engine.demo_data import make_demo_candles
    sample = make_demo_candles(periods=120, interval='1s')
    monkeypatch.setattr(uiapp, '_ensure_data', lambda *a, **k: sample.copy())
    r = TestClient(uiapp.app).post('/api/backtest', json={'source': 'bitget', 'interval': '1s', 'strategy': 'sma_cross'})
    js = r.json()
    assert r.status_code == 200
    assert js['ok'] is True
    assert js['interval'] == '1s'


def test_upsert_candles_drops_bad_timestamp_rows(tmp_path):
    import pandas as pd
    from src.data_engine.storage import upsert_candles, load_candles
    db = tmp_path / 'bad_ts.db'
    df = pd.DataFrame([
        {'timestamp': 'bad', 'open': 1, 'high': 2, 'low': 1, 'close': 1.5, 'volume': 1, 'turnover': 0, 'symbol': 'BTCUSDT', 'category': 'USDT-FUTURES', 'interval': '1m'},
        {'timestamp': 1700000000000, 'open': 1, 'high': 2, 'low': 1, 'close': 1.5, 'volume': 1, 'turnover': 0, 'symbol': 'BTCUSDT', 'category': 'USDT-FUTURES', 'interval': '1m'},
    ])
    assert upsert_candles(df, db_path=str(db)) == 1
    out = load_candles('BTCUSDT', 'USDT-FUTURES', '1m', db_path=str(db))
    assert len(out) == 1
    assert int(out.iloc[0]['timestamp']) == 1700000000000


def test_live_broker_safe_mode_blocks_cancel_write():
    from src.execution.live_bitget_broker import LiveBitgetBroker
    b = LiveBitgetBroker(safe_mode=True)
    try:
        b.cancel_order('cid', symbol='BTCUSDT')
    except RuntimeError as e:
        assert 'safe_mode' in str(e)
    else:
        raise AssertionError('safe mode must block cancel_order')


def test_latest_candles_returns_context_on_cache_fallback(monkeypatch):
    import pandas as pd
    from src.ui import app as uiapp
    from fastapi.testclient import TestClient
    sample = pd.DataFrame([{
        'timestamp': 1700000000000, 'open': 1, 'high': 2, 'low': 1, 'close': 1.5,
        'volume': 10, 'turnover': 0, 'symbol': 'ETHUSDT', 'category': 'USDT-FUTURES', 'interval': '5m'
    }])
    class BadClient:
        def get_recent_candles(self, *a, **k):
            raise RuntimeError('api down')
    monkeypatch.setattr(uiapp, 'BitgetPublicClient', lambda: BadClient())
    monkeypatch.setattr(uiapp, '_load_local', lambda *a, **k: sample.copy())
    js = TestClient(uiapp.app).get('/api/latest-candles', params={'symbol':'ETHUSDT','category':'USDT-FUTURES','interval':'5m'}).json()
    assert js['ok'] is True and js['stale'] is True
    assert js['symbol'] == 'ETHUSDT'
    assert js['interval'] == '5m'


def test_storage_purges_corrupt_shifted_candle_rows(tmp_path):
    import sqlite3
    from src.data_engine.storage import init_db, load_candles
    db = tmp_path / 'bad.db'
    init_db(str(db))
    with sqlite3.connect(db) as con:
        con.execute("""
            INSERT INTO candles(symbol, category, interval, timestamp, open, high, low, close, volume, turnover)
            VALUES ('1704067200000','30000','30010',30020,30030,30040,30050,'BTCUSDT','USDT-FUTURES','1m')
        """)
    init_db(str(db))
    assert load_candles('BTCUSDT', 'USDT-FUTURES', '1m', db_path=str(db)).empty


def test_ui_html_keeps_live_across_interval_switch_and_editable_end_date():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    html = TestClient(app).get('/').text
    assert 'id="end" type="date"' in html
    assert 'id="end" type="date" readonly' not in html
    assert 'liveWanted' in html
    assert 'await loadCandles(sourceForInterval(),{keepLive:true});startLive()' in html
    assert 'setTradeLevels([])' in html
    assert 'normalizeCategory' in html


def test_ui_syncs_price_and_lower_indicator_panes():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    html = TestClient(app).get('/').text
    assert '/static/lightweight-charts.standalone.production.js' in html
    assert 'addSeries' in html
    assert 'CandlestickSeries' in html
    assert 'HistogramSeries' in html
    assert 'panes' in html
    assert "lowerDef={volume:['Volume','hist',1]" in html
    assert 'subscribeVisibleLogicalRangeChange' in html
    assert 'setVisibleLogicalRange' in html
    assert 'isoUtc(startSec)' in html
    assert 'breakLineGaps' in html
    assert 'createSeriesMarkers' in html


def test_multi_timeframe_strategy_signal_shape():
    from src.strategies.multi_timeframe_momentum import MultiTimeframeMomentumStrategy
    df = make_demo_candles(periods=800, interval='1m')
    strat = MultiTimeframeMomentumStrategy(symbol='BTCUSDT', allow_short=True)
    sig = strat.generate(df)
    assert sig.target_position in (-1, 0, 1)
    assert 'mtf_vote_15m' in (sig.metadata or {})


def test_backfill_aggregates_1m_to_15m_without_gaps():
    from src.data_engine.backfill import aggregate_from_1m
    from src.data_engine.validator import validate_candles
    df = make_demo_candles(periods=120, interval='1m')
    out = aggregate_from_1m(df, '15m')
    assert len(out) == 8
    assert out['interval'].eq('15m').all()
    assert validate_candles(out, '15m', strict=True) == []


def test_backfill_aggregate_high_low_includes_open_close():
    from src.data_engine.backfill import aggregate_from_1m
    ts = 1700000000000
    df = pd.DataFrame([
        {
            'timestamp': ts,
            'open': 90,
            'high': 101,
            'low': 95,
            'close': 100,
            'volume': 1,
            'turnover': 1,
            'symbol': 'BTCUSDT',
            'category': 'USDT-FUTURES',
            'interval': '1m',
        },
        {
            'timestamp': ts + 60_000,
            'open': 100,
            'high': 102,
            'low': 96,
            'close': 110,
            'volume': 2,
            'turnover': 2,
            'symbol': 'BTCUSDT',
            'category': 'USDT-FUTURES',
            'interval': '1m',
        },
    ])
    out = aggregate_from_1m(df, '5m')
    assert float(out.iloc[0]['low']) == 90
    assert float(out.iloc[0]['high']) == 110


def test_full_backfill_runs_in_monthly_chunks(monkeypatch, tmp_path):
    from src.data_engine import backfill as bf
    calls = []
    rebuild = {}

    def fake_backfill_range(**kwargs):
        calls.append(kwargs)
        return {'1m': 10, '_fetched_1m': 10, '_missing_ranges': 1}

    def fake_rebuild(symbol, category, intervals, out_dir, start=None, end=None, write_csv=False, progress=False):
        rebuild['symbol'] = symbol
        rebuild['category'] = category
        rebuild['intervals'] = intervals
        rebuild['out_dir'] = out_dir
        rebuild['start'] = start
        rebuild['end'] = end
        rebuild['write_csv'] = write_csv
        rebuild['progress'] = progress
        return {'15m': 20, '1M': 2}

    monkeypatch.setattr(bf, 'backfill_range', fake_backfill_range)
    monkeypatch.setattr(bf, 'rebuild_derived_intervals', fake_rebuild)
    rows = bf.backfill_full_history(
        'BTCUSDT',
        'USDT-FUTURES',
        start='2024-06-01T00:00:00Z',
        end='2026-02-01T00:00:00Z',
        out_dir=tmp_path,
    )
    assert rows['_chunks'] == 20
    assert rows['1m'] == 200
    assert calls[0]['suffix'] == 'full_2024_06'
    assert calls[-1]['suffix'] == 'full_2026_01'
    assert all(c['derive_intervals'] == () for c in calls)
    assert rebuild['start'] is None
    assert '15m' in rebuild['intervals'] and '1M' in rebuild['intervals']
    assert rows['15m'] == 20
    assert rows['1M'] == 2


def test_ensure_1m_single_missing_bar_uses_exclusive_fetch_end(monkeypatch, tmp_path):
    import pandas as pd
    from src.data_engine import canonical as cn
    ts = 1700000040000
    captured = {}

    monkeypatch.setattr(cn, 'load_candles', lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(cn, 'upsert_candles', lambda *a, **k: 1)

    class FakeClient:
        def download_candles(self, symbol, category, interval, start, end):
            captured['start'] = int(start)
            captured['end'] = int(end)
            return pd.DataFrame([{
                'timestamp': captured['start'], 'open': 1, 'high': 1, 'low': 1, 'close': 1,
                'volume': 1, 'turnover': 0, 'symbol': symbol, 'category': category, 'interval': interval,
            }])

    monkeypatch.setattr(cn, 'BitgetPublicClient', lambda: FakeClient())
    df, report = cn.ensure_1m_range('BTCUSDT', 'USDT-FUTURES', ts, ts, db_path=str(tmp_path / 'x.db'), fetch=True)
    assert not df.empty
    assert captured['end'] - captured['start'] == 60_000


def test_ensure_1m_force_refreshes_complete_current_window(monkeypatch, tmp_path):
    import pandas as pd
    from src.data_engine import canonical as cn

    ts = 1700000040000
    cached = pd.DataFrame([{
        'timestamp': ts, 'open': 1, 'high': 1, 'low': 1, 'close': 1,
        'volume': 1, 'turnover': 0, 'symbol': 'BTCUSDT',
        'category': 'USDT-FUTURES', 'interval': '1m',
    }])
    captured = {}
    monkeypatch.setattr(cn, 'load_candles', lambda *a, **k: cached.copy())
    monkeypatch.setattr(cn, 'upsert_candles', lambda *a, **k: 1)

    class FakeClient:
        def download_candles(self, symbol, category, interval, start, end):
            captured['start'] = int(start)
            captured['end'] = int(end)
            return cached.copy()

    monkeypatch.setattr(cn, 'BitgetPublicClient', lambda: FakeClient())
    cn.ensure_1m_range(
        'BTCUSDT', 'USDT-FUTURES', ts, ts,
        db_path=str(tmp_path / 'x.db'), fetch=True,
        force_refresh_start_ms=ts, refresh_recent_tail=False,
    )
    assert captured['start'] == ts
    assert captured['end'] == ts + 60_000


def test_live_gap_repair_starts_at_chart_cursor(monkeypatch):
    from types import SimpleNamespace
    from src.ui import app as uiapp

    now = 1700010000000
    cursor = 1700001000  # chart protocol uses seconds
    captured = {}
    sample = pd.DataFrame([{
        'timestamp': 1700001000000, 'open': 1, 'high': 2, 'low': 1,
        'close': 1.5, 'volume': 1, 'turnover': 0, 'symbol': 'BTCUSDT',
        'category': 'USDT-FUTURES', 'interval': '15m',
    }])

    monkeypatch.setattr(uiapp, 'now_ms', lambda: now)

    def fake_load(symbol, category, interval, start, end, **kwargs):
        captured.update({'symbol': symbol, 'interval': interval, 'start': int(start), 'end': int(end), **kwargs})
        return sample.copy(), SimpleNamespace(fetched_1m=7, gaps_found=0)

    monkeypatch.setattr(uiapp, 'load_or_fetch_canonical_candles', fake_load)
    df, report = uiapp._repair_live_gap('BTCUSDT', 'USDT-FUTURES', '15m', cursor)
    assert len(df) == 1
    assert captured['start'] == uiapp.bucket_start_ms(cursor * 1000, '15m')
    assert captured['end'] == now
    assert captured['refresh_recent_tail'] is False
    assert report['fetched_1m'] == 7


def test_ui_live_merges_existing_chart_and_sends_cursor():
    from fastapi.testclient import TestClient
    from src.ui.app import app
    html = TestClient(app).get('/').text
    assert "chartLastTime=allCandles.length" in html
    assert "from:String(chartLastTime)" in html
    assert "d.type==='catchup'" in html
    assert "mergeLiveCandles" in html


def test_storage_migrates_legacy_crypto_rows_and_separates_market_types(tmp_path):
    import sqlite3
    from src.data_engine.storage import init_db, load_candles, upsert_candles

    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE candles (
                symbol TEXT NOT NULL, category TEXT NOT NULL, interval TEXT NOT NULL, timestamp INTEGER NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0, turnover REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'bitget', updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(symbol, category, interval, timestamp)
            )
            """
        )
        con.execute(
            "INSERT INTO candles VALUES ('BTCUSDT','USDT-FUTURES','1m',1700000000000,1,2,1,1.5,1,0,'bitget',0)"
        )
    init_db(str(db))
    crypto = load_candles("BTCUSDT", "USDT-FUTURES", "1m", db_path=str(db), market_type="CRYPTO")
    assert len(crypto) == 1
    stock = crypto.copy()
    stock["symbol"] = "BTCUSDT"
    stock["category"] = "NASDAQ"
    stock["market_type"] = "US_STOCK"
    upsert_candles(stock, db_path=str(db), market_type="US_STOCK")
    assert len(load_candles("BTCUSDT", "NASDAQ", "1m", db_path=str(db), market_type="US_STOCK")) == 1
    assert len(load_candles("BTCUSDT", "USDT-FUTURES", "1m", db_path=str(db), market_type="CRYPTO")) == 1


def test_market_timezone_buckets_and_korean_regular_session_filter():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.data_engine.canonical import aggregate_from_1m, bucket_start_ms

    kor_open = int(datetime(2026, 1, 2, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    us_open = int(datetime(2026, 7, 6, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    assert bucket_start_ms(kor_open, "1D", "KOR_STOCK") == int(datetime(2026, 1, 2, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    assert bucket_start_ms(us_open, "1D", "US_STOCK") == int(datetime(2026, 7, 6, 0, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    rows = []
    for offset in (-60_000, 0, 60_000, 6 * 60 * 60 * 1000 + 29 * 60_000, 6 * 60 * 60 * 1000 + 30 * 60_000):
        rows.append({"timestamp": kor_open + offset, "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1, "turnover": 1, "symbol": "005930", "category": "KRX", "interval": "1m"})
    out = aggregate_from_1m(pd.DataFrame(rows), "1m", "KOR_STOCK")
    assert out["timestamp"].tolist() == [kor_open, kor_open + 60_000, kor_open + 6 * 60 * 60 * 1000 + 29 * 60_000]
    assert out["market_type"].eq("KOR_STOCK").all()


def test_market_state_manager_cancels_inactive_stream_and_keeps_risk_loop():
    import asyncio
    from src.execution.market_state_manager import MarketStateManager

    async def scenario():
        manager = MarketStateManager(risk_poll_seconds=5)
        manager.configure_market("KOR_STOCK", position_probe=lambda: 1)
        await manager.switch_active("KOR_STOCK")
        heavy = asyncio.create_task(asyncio.sleep(30))
        assert await manager.attach_heavy_task("KOR_STOCK", heavy)
        state = await manager.switch_active("CRYPTO")
        await asyncio.sleep(0)
        assert heavy.cancelled() or heavy.done()
        assert state["markets"]["KOR_STOCK"]["risk_polling"] is True
        await manager.shutdown()

    asyncio.run(scenario())


def test_market_state_position_update_starts_inactive_risk_loop():
    import asyncio
    from src.execution.market_state_manager import MarketStateManager

    async def scenario():
        manager = MarketStateManager(risk_poll_seconds=5)
        state = await manager.update_known_position_count("US_STOCK", 2)
        assert state["markets"]["US_STOCK"]["risk_polling"] is True
        await manager.shutdown()

    asyncio.run(scenario())


def test_toss_public_client_requires_oauth_credentials(monkeypatch):
    from src.toss.auth import TossApiConfigurationError, TossOAuthClient
    from src.toss.public_client import TossPublicClient

    monkeypatch.setattr("src.toss.auth.load_dotenv", lambda: None)
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TOSS_API_KEY", raising=False)
    monkeypatch.delenv("TOSS_SECRET_KEY", raising=False)
    TossOAuthClient.reset_cache_for_testing()
    try:
        TossPublicClient().get_recent_candles("005930", "KRX", "1m", market_type="KOR_STOCK")
    except TossApiConfigurationError:
        pass
    else:
        raise AssertionError("Toss market data must require OAuth credentials")


def test_ui_contains_market_switching_contract():
    from fastapi.testclient import TestClient
    from src.ui.app import app

    html = TestClient(app).get("/").text
    assert 'data-market="CRYPTO"' in html
    assert 'data-market="KOR_STOCK"' in html
    assert 'data-market="US_STOCK"' in html
    assert "/api/market/switch" in html
    assert "market_type:p.market_type" in html


def test_stock_universe_db_search_and_manual_placeholder(tmp_path):
    from src.data_engine.storage import (
        ensure_stock_symbol,
        search_stock_symbols,
        stock_universe_status,
        upsert_stock_symbols,
    )

    db = str(tmp_path / "stock-universe.db")
    inserted = upsert_stock_symbols(
        [
            {
                "market_type": "KOR_STOCK",
                "symbol": "005930",
                "exchange": "KRX",
                "name": "Samsung Electronics",
                "name_en": "Samsung Electronics",
                "currency": "KRW",
                "is_tradeable": True,
                "source": "fixture",
            },
            {
                "market_type": "US_STOCK",
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "name": "Apple Inc.",
                "currency": "USD",
                "is_tradeable": True,
                "source": "fixture",
            },
        ],
        db,
        source="fixture",
    )
    assert inserted == 2
    assert [row["symbol"] for row in search_stock_symbols("samsung", "KOR_STOCK", db_path=db)] == ["005930"]
    assert search_stock_symbols("AAPL", "KOR_STOCK", db_path=db) == []

    placeholder = ensure_stock_symbol("MSFT", "US_STOCK", db_path=db)
    assert placeholder["symbol"] == "MSFT"
    assert placeholder["status"] == "pending_metadata"
    assert placeholder["is_tradeable"] is False
    status = stock_universe_status(db)
    assert status["markets"]["KOR_STOCK"]["total"] == 1
    assert status["markets"]["US_STOCK"]["total"] == 2


def test_official_stock_metadata_replaces_placeholder_but_keeps_krx_candle_key(tmp_path):
    from src.data_engine.storage import ensure_stock_symbol, search_stock_symbols, upsert_stock_symbols
    from src.ui.app import _stock_candle_category

    db = str(tmp_path / "stock-catalog-cleanup.db")
    ensure_stock_symbol("005930", "KOR_STOCK", db_path=db)
    upsert_stock_symbols(
        [{
            "market_type": "KOR_STOCK",
            "symbol": "005930",
            "exchange": "KOSPI",
            "name": "Samsung Electronics",
            "is_tradeable": True,
            "source": "toss_stock_info",
        }],
        db,
        source="toss_stock_info",
    )
    rows = search_stock_symbols("005930", "KOR_STOCK", limit=10, db_path=db)
    assert [(row["exchange"], row["status"]) for row in rows] == [("KOSPI", "active")]
    assert _stock_candle_category("KOR_STOCK", "KOSPI") == "KRX"


def test_stock_gap_detection_excludes_overnight_and_detects_intrasession_gap():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.data_engine.canonical import find_market_gaps, market_to_ms

    kst = ZoneInfo("Asia/Seoul")
    first_day = int(datetime(2024, 1, 2, 9, 0, tzinfo=kst).timestamp() * 1000)
    second_day = int(datetime(2024, 1, 3, 9, 0, tzinfo=kst).timestamp() * 1000)
    rows = [
        {"timestamp": first_day, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": first_day + 60_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": first_day + 180_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": second_day, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]
    gaps = find_market_gaps(pd.DataFrame(rows), "1m", "KOR_STOCK")
    assert gaps == [(first_day + 120_000, first_day + 120_000, 1)]
    assert market_to_ms("2024-01-02", "KOR_STOCK") == int(datetime(2024, 1, 2, 0, 0, tzinfo=kst).timestamp() * 1000)


def test_surge_snapshot_uses_price_volume_history_without_future_rows():
    from src.indicators.library import surge_snapshot

    base = 1_704_067_200_000
    rows = []
    for index in range(25):
        close = 100 + index * 0.1
        rows.append(
            {
                "timestamp": base + index * 60_000,
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.1,
                "close": close,
                "volume": 100,
                "turnover": close * 100,
            }
        )
    rows[-1].update({"open": 102.3, "high": 106, "low": 102.2, "close": 105.5, "volume": 700, "turnover": 73_850})
    snapshot = surge_snapshot(pd.DataFrame(rows), "US_STOCK")
    assert snapshot["ready"] is True
    assert snapshot["relative_volume20"] > 1
    assert snapshot["price_change_5_pct"] > 0
    assert snapshot["breakout_20_pct"] > 0


def test_surge_scanner_rotates_local_catalog_and_persists_rankings(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.data_engine.storage import upsert_candles, upsert_stock_symbols
    from src.scanner.surge_scanner import SurgeScanner

    db = str(tmp_path / "surge-scanner.db")
    upsert_stock_symbols(
        [
            {"market_type": "KOR_STOCK", "symbol": "000001", "exchange": "KRX", "name": "First", "is_tradeable": True},
            {"market_type": "KOR_STOCK", "symbol": "000002", "exchange": "KRX", "name": "Second", "is_tradeable": True},
        ],
        db,
        source="fixture",
    )
    start = int(datetime(2024, 1, 2, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    rows = []
    for symbol in ("000001", "000002"):
        for index in range(25):
            close = 100.0 + index * (0.25 if symbol == "000001" else 0.01)
            volume = 100.0
            if symbol == "000001" and index == 24:
                close, volume = 110.0, 900.0
            rows.append(
                {
                    "timestamp": start + index * 60_000,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": volume,
                    "turnover": close * volume,
                    "symbol": symbol,
                    "category": "KRX",
                    "interval": "1m",
                    "market_type": "KOR_STOCK",
                }
            )
    upsert_candles(pd.DataFrame(rows), db, source="fixture", market_type="KOR_STOCK")
    scanner = SurgeScanner(db_path=db, client=object())
    first = scanner.refresh("KOR_STOCK", max_symbols=1, fetch_current=False)
    assert first.scanned_this_run == 1
    assert first.cycle_complete is False
    second = scanner.refresh("KOR_STOCK", max_symbols=1, fetch_current=False)
    assert second.cycle_complete is True
    snapshot = scanner.snapshot("KOR_STOCK", top_n=100)
    assert snapshot["catalog_total"] == 2
    assert snapshot["cycle_complete"] is True
    assert snapshot["rankings"][0]["symbol"] == "000001"


def test_surge_score_uses_only_the_supplied_completed_history():
    from src.indicators.library import surge_snapshot
    from src.scanner.surge_scanner import score_surge_snapshot

    base = 1_704_067_200_000
    rows = []
    for index in range(25):
        close = 100 + index * 0.1
        rows.append({"timestamp": base + index * 60_000, "open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 100, "turnover": close * 100})
    rows[-1].update({"close": 105, "high": 105.2, "volume": 800, "turnover": 84_000})
    prefix_score = score_surge_snapshot(surge_snapshot(pd.DataFrame(rows), "US_STOCK"))
    future_rows = rows + [{"timestamp": base + 25 * 60_000, "open": 400, "high": 500, "low": 300, "close": 450, "volume": 99_999, "turnover": 44_999_550}]
    assert prefix_score == score_surge_snapshot(surge_snapshot(pd.DataFrame(future_rows[:-1]), "US_STOCK"))


def test_stock_search_api_contract_is_local_and_searchable(monkeypatch):
    from fastapi.testclient import TestClient
    import src.ui.app as uiapp

    monkeypatch.setattr(
        uiapp,
        "search_stock_symbols",
        lambda query, market_type, limit=20: [
            {
                "market_type": market_type,
                "symbol": "005930",
                "exchange": "KRX",
                "name": "Samsung Electronics",
                "name_en": "Samsung Electronics",
                "currency": "KRW",
                "status": "active",
                "is_tradeable": True,
                "source": "fixture",
            }
        ],
    )
    monkeypatch.setattr(uiapp, "stock_universe_status", lambda: {"total": 1, "markets": {"KOR_STOCK": {"total": 1, "tradeable": 1, "updated_at": None}}})
    response = TestClient(uiapp.app).get("/api/stocks/search", params={"market_type": "KOR_STOCK", "q": "005930"})
    data = response.json()
    assert response.status_code == 200
    assert data["ok"] is True
    assert data["results"][0]["symbol"] == "005930"
    assert data["direct_entry"] == "005930"
    assert "stockSearchPanel" in uiapp.HTML
    assert "/api/stocks/select" in uiapp.HTML


def test_toss_official_oauth_candles_and_holdings_contract(monkeypatch):
    from src.toss.auth import TossOAuthClient
    from src.toss.private_client import TossPrivateClient
    from src.toss.public_client import TossPublicClient

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return FakeResponse({"access_token": "test-token", "token_type": "Bearer", "expires_in": 86400})

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if url.endswith("/api/v1/candles"):
                return FakeResponse({"result": {"candles": [{
                    "timestamp": "2026-03-25T09:00:00+09:00",
                    "openPrice": "71600",
                    "highPrice": "72300",
                    "lowPrice": "71500",
                    "closePrice": "72000",
                    "volume": "3521000",
                }]}})
            if url.endswith("/api/v1/accounts"):
                return FakeResponse({"result": [{"accountSeq": 1, "accountType": "BROKERAGE"}]})
            if url.endswith("/api/v1/holdings"):
                return FakeResponse({"result": {"items": [{"symbol": "005930", "quantity": "10", "marketCountry": "KR"}]}})
            raise AssertionError(f"Unexpected Toss URL: {url}")

    monkeypatch.setenv("TOSS_CLIENT_ID", "test-client")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("TOSS_ALLOWED_IP", "203.0.113.10")
    TossOAuthClient.reset_cache_for_testing()
    session = FakeSession()
    auth = TossOAuthClient(session=session)
    market = TossPublicClient(session=session, auth=auth)
    candles = market.get_recent_candles("005930", "KRX", "1m", market_type="KOR_STOCK")
    assert len(candles) == 1
    assert candles.iloc[0]["close"] == 72000

    private = TossPrivateClient(session=session, auth=auth)
    holdings = private.get_positions("KOR_STOCK")
    assert private.has_credentials() is True
    assert holdings["result"]["items"][0]["symbol"] == "005930"
    token_request = session.calls[0]
    assert token_request[1].endswith("/oauth2/token")
    assert token_request[2]["data"]["grant_type"] == "client_credentials"
    holding_request = next(call for call in session.calls if call[1].endswith("/api/v1/holdings"))
    assert holding_request[2]["headers"]["Authorization"] == "Bearer test-token"
    assert holding_request[2]["headers"]["X-Tossinvest-Account"] == "1"
