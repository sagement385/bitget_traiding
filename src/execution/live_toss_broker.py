from __future__ import annotations

from typing import Any

from src.execution.broker_base import Broker
from src.execution.risk_manager import RiskConfig, RiskManager
from src.markets import KOR_STOCK, US_STOCK, normalize_market_type
from src.models import OrderRequest
from src.toss.private_client import TossPrivateClient


class LiveTossBroker(Broker):
    """Toss Securities broker using the official REST order contract.

    ``safe_mode`` remains enabled by default. Turning it off is a separate
    live-trading decision; this class never changes that policy on its own.
    """

    def __init__(
        self,
        private_client: TossPrivateClient | None = None,
        *,
        market_type: str = KOR_STOCK,
        risk_manager: RiskManager | None = None,
        safe_mode: bool = True,
    ):
        self.market_type = normalize_market_type(market_type)
        if self.market_type not in {KOR_STOCK, US_STOCK}:
            raise ValueError("LiveTossBroker supports KOR_STOCK or US_STOCK")
        self.private_client = private_client or TossPrivateClient(allow_order_submission=not safe_mode)
        self.risk_manager = risk_manager or RiskManager(RiskConfig())
        self.safe_mode = bool(safe_mode)

    def _map_order(self, order: OrderRequest) -> dict[str, Any]:
        order_type = "MARKET" if order.order_type.lower() == "market" else "LIMIT"
        payload: dict[str, Any] = {
            "clientOrderId": order.client_oid,
            "symbol": order.symbol.upper(),
            "side": order.side.upper(),
            "quantity": str(order.qty),
            "orderType": order_type,
            "timeInForce": "CLS" if str(order.time_in_force).upper() == "CLS" else "DAY",
        }
        if order_type == "LIMIT":
            if order.price is None:
                raise ValueError("Toss LIMIT orders require a price")
            payload["price"] = str(order.price)
        return payload

    def place_order(self, order: OrderRequest, *, equity: float | None = None, current_exposure: float = 0.0, mark_price: float | None = None) -> dict[str, Any]:
        if self.safe_mode:
            raise RuntimeError("safe_mode=True: Toss order submission is blocked")
        notional = float(order.qty) * float(mark_price or order.price or 0)
        if equity is not None and notional > 0:
            allowed, reason = self.risk_manager.validate(notional, float(equity), float(current_exposure), leverage=1)
            if not allowed:
                raise RuntimeError(f"risk_blocked:{reason}")
        return self.private_client.place_order(self._map_order(order))

    def cancel_order(self, client_oid: str, *, symbol: str, order_id: str | None = None) -> dict[str, Any]:
        if self.safe_mode:
            raise RuntimeError("safe_mode=True: Toss order cancellation is blocked")
        if not order_id:
            raise ValueError("Toss cancellation requires the server-issued order_id, not only client_oid")
        return self.private_client.cancel_order(order_id)

    def get_account(self) -> dict[str, Any]:
        return self.private_client.get_account()

    def get_positions(self) -> dict[str, Any]:
        return self.private_client.get_positions(self.market_type)
