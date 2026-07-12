import asyncio, json, time
from typing import Callable, Iterable
from src.bitget.signer import sign
from src.utils.env import load_dotenv
import os

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

PUBLIC_WS = 'wss://ws.bitget.com/v2/ws/public'
PRIVATE_WS = 'wss://ws.bitget.com/v2/ws/private'

class BitgetWebSocketClient:
    def __init__(self, private=False, url=None):
        if websockets is None:
            raise RuntimeError('websockets package not installed. pip install websockets')
        load_dotenv()
        self.private = private
        self.url = url or (PRIVATE_WS if private else PUBLIC_WS)
        self.api_key = os.getenv('BITGET_API_KEY','')
        self.secret = os.getenv('BITGET_SECRET_KEY','')
        self.passphrase = os.getenv('BITGET_PASSPHRASE','')
        self.subscriptions = []
        self.connected = False

    async def _login(self, ws):
        ts = str(int(time.time()))
        payload = {
            'op': 'login',
            'args': [{
                'apiKey': self.api_key,
                'passphrase': self.passphrase,
                'timestamp': ts,
                'sign': sign(self.secret, ts, 'GET', '/user/verify', ''),
            }]
        }
        await ws.send(json.dumps(payload))

    async def subscribe(self, topics: Iterable[dict], on_message: Callable[[dict], None], reconnect=True):
        self.subscriptions = list(topics)
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None) as ws:
                    self.connected = True
                    if self.private:
                        await self._login(ws)
                    await ws.send(json.dumps({'op':'subscribe','args':self.subscriptions}))
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    async for raw in ws:
                        if raw == 'pong':
                            continue
                        data = json.loads(raw)
                        on_message(data)
                    ping_task.cancel()
            except Exception as e:
                self.connected = False
                if not reconnect:
                    raise
                await asyncio.sleep(3)

    async def _ping_loop(self, ws):
        while True:
            await asyncio.sleep(30)
            await ws.send('ping')


def candle_topic(symbol='BTCUSDT', interval='1m', product_type='USDT-FUTURES'):
    return {'instType': product_type, 'channel': f'candle{interval}', 'instId': symbol}


def trade_topic(symbol='BTCUSDT', product_type='USDT-FUTURES'):
    return {'instType': product_type, 'channel': 'trade', 'instId': symbol}
