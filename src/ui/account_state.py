from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def _unwrap_rows(payload: Any) -> list[dict[str, Any]]:
    value = payload
    if isinstance(value, dict):
        value = value.get("data", value.get("result", value.get("items", value.get("rows", []))))
    if isinstance(value, dict):
        value = value.get("items", value.get("list", [value]))
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError):
        return None
    return timestamp * 1000 if timestamp < 10_000_000_000 else timestamp


def normalize_account(payload: Any) -> dict[str, Any]:
    rows = _unwrap_rows(payload)
    row = rows[0] if rows else {}
    equity = _number(_first(row, "accountEquity", "usdtEquity", "equity", "totalEquity"))
    available = _number(_first(row, "available", "availableBalance", "crossedMaxAvailable", "maxAvailable"))
    used_margin = _number(_first(row, "totalMargin", "margin", "usedMargin", "frozen"))
    unrealized_pnl = _number(_first(row, "unrealizedPL", "unrealizedPnl", "unrealizedProfit"))
    return {
        "status": "connected" if row else "empty",
        "equity": equity,
        "available": available,
        "used_margin": used_margin,
        "unrealized_pnl": unrealized_pnl,
        "margin_coin": _first(row, "marginCoin", "asset", default="USDT"),
        "raw_fields": sorted(row.keys()),
    }


def normalize_positions(payload: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    wanted = str(symbol or "").upper().strip()
    positions: list[dict[str, Any]] = []
    for row in _unwrap_rows(payload):
        item_symbol = str(_first(row, "symbol", "instId", "instrument", default="")).upper()
        if wanted and item_symbol and item_symbol != wanted:
            continue
        qty = _number(_first(row, "total", "positionAmt", "positionQty", "qty", "size"), 0.0) or 0.0
        if abs(qty) <= 0:
            continue
        mark_price = _number(_first(row, "markPrice", "marketPrice", "currentPrice"))
        avg_entry = _number(_first(row, "averageOpenPrice", "openPriceAvg", "avgEntryPrice", "entryPrice"))
        notional = abs(qty) * abs(mark_price or avg_entry or 0.0)
        positions.append(
            {
                "symbol": item_symbol or wanted,
                "side": str(_first(row, "holdSide", "side", "positionSide", default="unknown")).lower(),
                "qty": abs(qty),
                "avg_entry_price": avg_entry,
                "mark_price": mark_price,
                "unrealized_pnl": _number(_first(row, "unrealizedPL", "unrealizedPnl", "upl")),
                "leverage": _number(_first(row, "leverage", "lever")),
                "liquidation_price": _number(_first(row, "liquidationPrice", "liqPrice")),
                "stop_loss": _number(_first(row, "stopLoss", "stopLossPrice")),
                "take_profit": _number(_first(row, "takeProfit", "takeProfitPrice")),
                "margin": _number(_first(row, "margin", "marginSize")),
                "notional": notional,
            }
        )
    return positions


def normalize_fills(payload: Any, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    wanted = str(symbol or "").upper().strip()
    fills: list[dict[str, Any]] = []
    for row in _unwrap_rows(payload):
        item_symbol = str(_first(row, "symbol", "instId", "instrument", default="")).upper()
        if wanted and item_symbol and item_symbol != wanted:
            continue
        timestamp = _timestamp_ms(_first(row, "tradeTime", "fillTime", "ctime", "uTime", "timestamp", "time"))
        fills.append(
            {
                "timestamp": timestamp,
                "symbol": item_symbol or wanted,
                "side": str(_first(row, "side", "tradeSide", "holdSide", default="unknown")).lower(),
                "qty": _number(_first(row, "baseVolume", "size", "qty", "volume"), 0.0) or 0.0,
                "price": _number(_first(row, "price", "fillPrice", "avgPrice")),
                "fee": _number(_first(row, "fee", "fillFee"), 0.0) or 0.0,
                "pnl": _number(_first(row, "profit", "realizedPL", "pnl", "profitLoss"), 0.0) or 0.0,
                "order_id": _first(row, "orderId", "orderID"),
                "trade_id": _first(row, "tradeId", "tradeID", "fillId"),
                "source": "bitget",
            }
        )
    fills.sort(key=lambda row: row.get("timestamp") or 0, reverse=True)
    return fills[: max(1, int(limit))]


def _today_pnl(fills: Iterable[dict[str, Any]], now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    total = 0.0
    for fill in fills:
        timestamp = fill.get("timestamp")
        if not timestamp:
            continue
        date = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date()
        if date == current.date():
            total += float(fill.get("pnl") or 0.0)
    return total


def build_risk_snapshot(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or {}
    equity = account.get("equity")
    daily_pnl = _today_pnl(fills)
    max_daily_loss_pct = 0.03
    max_leverage = max((float(row["leverage"]) for row in positions if row.get("leverage") is not None), default=None)
    configured_daily_limit = _number(settings.get("daily_loss_limit"), None)
    daily_limit = configured_daily_limit if configured_daily_limit and configured_daily_limit > 0 else abs(float(equity or 0.0)) * max_daily_loss_pct if equity is not None else None
    configured_max_leverage = _number(settings.get("max_leverage"), 3.0) or 3.0
    configured_max_losses = int(_number(settings.get("max_consecutive_losses"), 3) or 3)
    usage_pct = min(100.0, abs(min(0.0, daily_pnl)) / daily_limit * 100) if daily_limit else None
    return {
        "daily_pnl": daily_pnl,
        "daily_loss_limit": daily_limit,
        "daily_loss_usage_pct": usage_pct,
        "consecutive_losses": 0,
        "max_consecutive_losses": configured_max_losses,
        "max_leverage": max_leverage,
        "configured_max_leverage": configured_max_leverage,
        "gross_exposure": sum(float(row.get("notional") or 0.0) for row in positions),
        "position_count": len(positions),
        "status": "ready" if account.get("status") == "connected" else "waiting_for_account",
    }


def build_notifications(*, account: dict[str, Any], positions: list[dict[str, Any]], fills: list[dict[str, Any]], error: str | None = None) -> list[dict[str, Any]]:
    notifications: list[dict[str, Any]] = []
    if error:
        notifications.append({"level": "error", "code": "account_sync_error", "message": "Bitget 계좌 동기화 실패", "detail": error})
    elif account.get("status") == "empty":
        notifications.append({"level": "warning", "code": "account_empty", "message": "계좌 응답에 잔고 데이터가 없습니다."})
    if positions:
        notifications.append({"level": "info", "code": "open_positions", "message": f"열린 포지션 {len(positions)}건을 확인했습니다."})
    if fills:
        latest = fills[0]
        notifications.append({"level": "info", "code": "latest_fill", "message": f"최근 체결 {latest.get('symbol') or '-'} {latest.get('side') or '-'}", "timestamp": latest.get("timestamp")})
    return notifications
