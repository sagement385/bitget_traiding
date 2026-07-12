from dataclasses import dataclass
from typing import Any, Optional
import time, uuid

@dataclass
class Signal:
    symbol: str
    target_position: int
    reason: str
    confidence: float = 1.0
    metadata: dict[str, Any] | None = None

@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: float
    order_type: str = 'market'
    price: Optional[float] = None
    pos_side: str = 'net'
    reduce_only: bool = False
    time_in_force: str = 'GTC'
    client_oid: str = ''
    product_type: str = 'USDT-FUTURES'
    margin_mode: str = 'isolated'
    margin_coin: str = 'USDT'
    trade_side: Optional[str] = None
    market_type: str = 'CRYPTO'
    def __post_init__(self):
        if not self.client_oid:
            self.client_oid = f"cq-{int(time.time()*1000)}-{uuid.uuid4().hex[:10]}"
        self.side = self.side.lower()
        if self.side not in ('buy','sell'):
            raise ValueError('side must be buy or sell')
        if float(self.qty) <= 0:
            raise ValueError('qty must be positive')

@dataclass
class Position:
    symbol: str
    side: str = 'none'
    qty: float = 0.0
    avg_entry_price: float = 0.0
    leverage: float = 1.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    liquidation_price: float | None = None
    updated_at: int = 0
    market_type: str = 'CRYPTO'

@dataclass
class Trade:
    timestamp: int
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    slippage: float
    pnl: float
    reason: str
    equity: float
