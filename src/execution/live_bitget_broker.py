from src.bitget.private_client import BitgetPrivateClient
from src.execution.risk_manager import RiskManager, RiskConfig
from src.models import OrderRequest

class LiveBitgetBroker:
    """Bitget Demo/Live 공통 주문 브로커.

    - safe_mode=True: 주문 전송 차단. 계좌조회/포지션조회만 가능.
    - safe_mode=False: 실제 주문 전송 가능. 반드시 CLI의 명시 플래그 필요.
    """
    def __init__(self, private_client=None, *, product_type='USDT-FUTURES', margin_mode='isolated',
                 margin_coin='USDT', risk_manager=None, safe_mode=True, mode='live'):
        self.private_client = private_client or BitgetPrivateClient()
        self.product_type = product_type
        self.margin_mode = margin_mode
        self.margin_coin = margin_coin
        self.risk_manager = risk_manager or RiskManager(RiskConfig())
        self.safe_mode = safe_mode
        self.mode = mode

    def _map_order(self, order: OrderRequest):
        order_type = 'market' if order.order_type.lower() == 'market' else 'limit'
        force = order.time_in_force.lower() if order.time_in_force else 'gtc'
        # one-way mode 기준: buy/sell만 사용, reduceOnly로 청산 표시
        return {
            'symbol': order.symbol,
            'product_type': getattr(order, 'product_type', self.product_type),
            'margin_mode': getattr(order, 'margin_mode', self.margin_mode),
            'margin_coin': getattr(order, 'margin_coin', self.margin_coin),
            'size': order.qty,
            'side': order.side.lower(),
            'order_type': order_type,
            'price': order.price,
            'force': force,
            'client_oid': order.client_oid,
            'trade_side': getattr(order, 'trade_side', None),
            'reduce_only': 'YES' if order.reduce_only else 'NO',
        }

    def place_order(self, order: OrderRequest, *, equity=None, current_exposure=0, leverage=1, mark_price=None):
        if self.safe_mode:
            raise RuntimeError('safe_mode=True: live/demo order blocked. Use --i-understand-live or demo mode after API check.')
        notional = float(order.qty) * float(mark_price or order.price or 0)
        if equity is not None and notional > 0:
            ok, reason = self.risk_manager.validate(notional, float(equity), current_exposure, leverage)
            if not ok:
                raise RuntimeError(f'risk_blocked:{reason}')
        return self.private_client.place_order(**self._map_order(order))

    def cancel_order(self, client_oid: str, *, symbol, order_id=None):
        if self.safe_mode:
            raise RuntimeError('safe_mode=True: cancel_order blocked.')
        return self.private_client.cancel_order(symbol=symbol, product_type=self.product_type,
                                                margin_coin=self.margin_coin, order_id=order_id,
                                                client_oid=client_oid)

    def get_account(self, symbol='BTCUSDT'):
        return self.private_client.get_account(symbol=symbol, product_type=self.product_type, margin_coin=self.margin_coin)

    def get_positions(self):
        return self.private_client.get_positions(product_type=self.product_type, margin_coin=self.margin_coin)

    def get_order_detail(self, symbol, client_oid=None, order_id=None):
        return self.private_client.get_order_detail(symbol, self.product_type, order_id, client_oid)

    def close_all(self, symbol, hold_side=None):
        if self.safe_mode:
            raise RuntimeError('safe_mode=True: close_all blocked.')
        return self.private_client.close_positions(symbol=symbol, product_type=self.product_type, hold_side=hold_side)

DemoBitgetBroker = LiveBitgetBroker
