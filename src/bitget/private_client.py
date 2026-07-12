import os, json, time
from urllib.parse import urlencode
import requests
from src.bitget.signer import sign, timestamp_ms
from src.utils.env import load_dotenv


def _clean(d):
    return {k: v for k, v in (d or {}).items() if v is not None and v != ''}

class BitgetPrivateClient:
    """Bitget Futures v2 private REST client.

    API key만 .env에 넣으면 계좌 조회/주문/취소/주문조회/포지션 조회까지 바로 호출 가능.
    실제 주문은 Broker 쪽 safe flag를 통과해야만 전송된다.
    """
    def __init__(self, base_url='https://api.bitget.com', timeout=10, max_retries=3):
        load_dotenv()
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_key = os.getenv('BITGET_API_KEY', '')
        self.secret = os.getenv('BITGET_SECRET_KEY', '')
        self.passphrase = os.getenv('BITGET_PASSPHRASE', '')

    def has_credentials(self):
        return all([self.api_key, self.secret, self.passphrase])

    def _headers(self, method, request_path_with_query, body=''):
        if not self.has_credentials():
            raise RuntimeError('Bitget API credentials missing. Fill .env first.')
        ts = timestamp_ms()
        return {
            'ACCESS-KEY': self.api_key,
            'ACCESS-SIGN': sign(self.secret, ts, method, request_path_with_query, body),
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': self.passphrase,
            'locale': 'en-US',
            'Content-Type': 'application/json',
        }

    def request(self, method, path, params=None, payload=None):
        method = method.upper()
        params = _clean(params)
        query = urlencode(params) if params else ''
        request_path = f'{path}?{query}' if query else path
        body = json.dumps(_clean(payload), separators=(',', ':')) if method != 'GET' else ''
        url = self.base_url + path
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.request(
                    method, url,
                    params=params if method == 'GET' else None,
                    headers=self._headers(method, request_path, body),
                    data=body or None,
                    timeout=self.timeout,
                )
                r.raise_for_status()
                js = r.json()
                if js.get('code') not in (None, '00000'):
                    raise RuntimeError(js)
                return js
            except Exception as e:
                last_err = e
                if attempt >= self.max_retries:
                    raise
                time.sleep(0.35 * attempt)
        raise last_err

    # Account / position
    def get_account(self, symbol='BTCUSDT', product_type='USDT-FUTURES', margin_coin='USDT'):
        return self.request('GET', '/api/v2/mix/account/account', params={
            'symbol': symbol, 'productType': product_type, 'marginCoin': margin_coin
        })

    def get_positions(self, product_type='USDT-FUTURES', margin_coin='USDT'):
        return self.request('GET', '/api/v2/mix/position/all-position', params={
            'productType': product_type, 'marginCoin': margin_coin
        })

    def get_order_detail(self, symbol, product_type='USDT-FUTURES', order_id=None, client_oid=None):
        return self.request('GET', '/api/v2/mix/order/detail', params={
            'symbol': symbol, 'productType': product_type, 'orderId': order_id, 'clientOid': client_oid
        })

    def get_pending_orders(self, product_type='USDT-FUTURES', symbol=None):
        return self.request('GET', '/api/v2/mix/order/orders-pending', params={
            'productType': product_type, 'symbol': symbol
        })

    # Trading
    def place_order(self, *, symbol, product_type='USDT-FUTURES', margin_mode='isolated', margin_coin='USDT',
                    size, side, order_type='market', price=None, force='gtc', client_oid=None,
                    trade_side=None, reduce_only=None):
        payload = {
            'symbol': symbol,
            'productType': product_type,
            'marginMode': margin_mode,
            'marginCoin': margin_coin,
            'size': str(size),
            'price': None if order_type == 'market' else str(price),
            'side': side,
            'tradeSide': trade_side,
            'orderType': order_type,
            'force': force,
            'clientOid': client_oid,
            'reduceOnly': reduce_only,
        }
        return self.request('POST', '/api/v2/mix/order/place-order', payload=payload)

    def cancel_order(self, *, symbol, product_type='USDT-FUTURES', margin_coin='USDT', order_id=None, client_oid=None):
        return self.request('POST', '/api/v2/mix/order/cancel-order', payload={
            'symbol': symbol, 'productType': product_type, 'marginCoin': margin_coin,
            'orderId': order_id, 'clientOid': client_oid
        })

    def close_positions(self, *, symbol, product_type='USDT-FUTURES', hold_side=None):
        return self.request('POST', '/api/v2/mix/order/close-positions', payload={
            'symbol': symbol, 'productType': product_type, 'holdSide': hold_side
        })

    def set_leverage(self, *, symbol, product_type='USDT-FUTURES', margin_coin='USDT', leverage=1, hold_side=None):
        return self.request('POST', '/api/v2/mix/account/set-leverage', payload={
            'symbol': symbol, 'productType': product_type, 'marginCoin': margin_coin,
            'leverage': str(leverage), 'holdSide': hold_side
        })
