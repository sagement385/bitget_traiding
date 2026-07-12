import asyncio

import pandas as pd


def candle_row(timestamp=1_700_000_000_000, close=100.0):
    return {
        "timestamp": timestamp,
        "open": 99.0,
        "high": max(101.0, close),
        "low": 98.0,
        "close": close,
        "volume": 2.0,
        "turnover": 200.0,
        "symbol": "BTCUSDT",
        "category": "USDT-FUTURES",
        "interval": "1m",
        "market_type": "CRYPTO",
    }


def test_candle_store_fast_path_and_historical_patch():
    from src.data_engine.candle_store import CandleStore

    store = CandleStore(symbol="BTCUSDT", category="USDT-FUTURES", interval="1m", max_rows=3)
    store.replace_all([candle_row(1_700_000_000_000 + i * 60_000) for i in range(3)])

    updated = store.apply(candle_row(1_700_000_000_000 + 2 * 60_000, close=110))
    assert updated.action == "updated"
    assert store.last().close == 110

    appended = store.apply(candle_row(1_700_000_000_000 + 3 * 60_000, close=111))
    assert appended.action == "inserted"
    assert appended.evicted is True
    assert len(store.values()) == 3

    patched = store.apply(candle_row(1_700_000_000_000 + 60_000, close=105))
    assert patched.action == "historical_patch"
    assert store.to_frame().set_index("timestamp").loc[1_700_000_060_000, "close"] == 105


def test_candle_write_buffer_coalesces_repeated_live_updates(monkeypatch, tmp_path):
    import src.data_engine.write_buffer as write_buffer

    calls = []

    def fake_upsert(frame, db_path, source, batch_size, market_type):
        calls.append((frame.copy(), source, market_type))
        return len(frame)

    monkeypatch.setattr(write_buffer, "upsert_candles", fake_upsert)

    async def scenario():
        buffer = write_buffer.CandleWriteBuffer(
            db_path=str(tmp_path / "trading.db"),
            flush_interval_seconds=0.01,
            max_batch_size=20,
        )
        for close in range(100, 110):
            await buffer.put_frame(pd.DataFrame([candle_row(close=close)]), source="bitget_live")
        await buffer.close()
        return buffer

    buffer = asyncio.run(scenario())
    assert len(calls) == 1
    assert len(calls[0][0]) == 1
    assert calls[0][0].iloc[0]["close"] == 109
    assert buffer.flush_count == 1
    assert buffer.write_count == 1


def test_account_state_normalizes_provider_rows_and_risk():
    from src.ui.account_state import build_risk_snapshot, normalize_account, normalize_fills, normalize_positions

    account = normalize_account({"data": [{"accountEquity": "1000", "available": "850", "totalMargin": "150"}]})
    positions = normalize_positions({"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.01", "averageOpenPrice": "60000", "markPrice": "61000", "unrealizedPL": "10", "leverage": "3"}]}, symbol="BTCUSDT")
    fills = normalize_fills({"data": [{"symbol": "BTCUSDT", "tradeSide": "buy", "baseVolume": "0.01", "price": "60000", "tradeTime": "1700000000000", "profit": "4.5"}]}, symbol="BTCUSDT")

    risk = build_risk_snapshot(account, positions, fills)
    assert account["equity"] == 1000
    assert positions[0]["notional"] == 610
    assert fills[0]["pnl"] == 4.5
    assert risk["position_count"] == 1
    assert risk["configured_max_leverage"] == 3.0


def test_account_snapshot_is_explicit_when_credentials_are_missing(monkeypatch):
    from fastapi.testclient import TestClient
    import src.ui.app as uiapp

    class NoCredentialsClient:
        def has_credentials(self):
            return False

    monkeypatch.setattr(uiapp, "BitgetPrivateClient", NoCredentialsClient)
    monkeypatch.setattr(uiapp, "ENABLE_LIVE_TRADING", True)
    response = TestClient(uiapp.app).get("/api/account/snapshot", params={"mode": "LIVE"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "credentials_missing"
    assert payload["account"]["equity"] is None
    assert payload["positions"] == []
    assert payload["notifications"][0]["code"] == "credentials_missing"
