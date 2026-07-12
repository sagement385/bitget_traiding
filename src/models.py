from dataclasses import dataclass
from typing import Any, Optional
import time, uuid


@dataclass(frozen=True, slots=True)
class Candle:
    """Normalized OHLCV candle shared by live data and persistence layers."""

    market_type: str
    symbol: str
    category: str
    interval: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0
    is_closed: bool = False
    source: str = "bitget"

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_type", str(self.market_type).upper())
        object.__setattr__(self, "symbol", str(self.symbol).upper())
        object.__setattr__(self, "category", str(self.category).upper())
        object.__setattr__(self, "open_time_ms", int(self.open_time_ms))
        for name in ("open", "high", "low", "close", "volume", "turnover"):
            object.__setattr__(self, name, float(getattr(self, name)))
        if self.open_time_ms <= 0:
            raise ValueError("candle timestamp must be positive")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle OHLC values must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("candle OHLC values are inconsistent")
        if self.volume < 0 or self.turnover < 0:
            raise ValueError("candle volume and turnover cannot be negative")

    @classmethod
    def from_row(cls, row: dict[str, Any], defaults: dict[str, Any] | None = None) -> "Candle":
        values = dict(defaults or {})
        values.update({key: value for key, value in row.items() if value is not None})
        timestamp = values.get("open_time_ms", values.get("timestamp", values.get("time")))
        if timestamp is None:
            raise ValueError("candle timestamp is required")
        timestamp = int(float(timestamp))
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        return cls(
            market_type=values.get("market_type", "CRYPTO"),
            symbol=values.get("symbol", "BTCUSDT"),
            category=values.get("category", "USDT-FUTURES"),
            interval=values.get("interval", "1m"),
            open_time_ms=timestamp,
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            volume=values.get("volume", 0.0) or 0.0,
            turnover=values.get("turnover", 0.0) or 0.0,
            is_closed=bool(values.get("is_closed", values.get("closed", False))),
            source=str(values.get("source", "bitget")),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.open_time_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "turnover": self.turnover,
            "symbol": self.symbol,
            "category": self.category,
            "interval": self.interval,
            "market_type": self.market_type,
            "is_closed": self.is_closed,
            "source": self.source,
        }

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
