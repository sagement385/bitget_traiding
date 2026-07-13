# Bitget Quant System

Bitget Futures quantitative trading system for market-data ingestion, charting,
backtesting, paper trading, and guarded live execution.

## Features

- Bitget REST and WebSocket market-data streams with SQLite caching.
- Bounded in-memory candle storage and asynchronous batched writes.
- SMA, RSI, breakout, VWAP/RSI, price-action/volume, and multi-timeframe strategies.
- Backtest and paper-trading workflows with position and risk controls.
- Live chart updates that preserve the current view and reconnect with gap repair.
- Focused crypto-first UI. Stock adapters and surge scanning are opt-in.
- Telegram notifications and optional Toss Securities adapters.

## Quick Start

```powershell
cd D:\bitget_quant_system
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m src.main migrate
python -m src.main demo-data
python -m src.main ui
```

Open `http://127.0.0.1:8000` in a browser. The UI starts with Bitget Futures
crypto markets enabled; stock markets and the surge scanner remain disabled
unless explicitly enabled in `.env`.

On Windows, run `scripts\launch_bitget_trading.vbs` to start the UI in the
background and open a maximized Chrome window automatically. A desktop
shortcut can point to that script for one-click startup.

## Configuration

Put real credentials in `.env`, never in `.env.example`.

```env
BITGET_API_KEY=
BITGET_SECRET_KEY=
BITGET_PASSPHRASE=
DATABASE_URL=
ENABLE_STOCK_MARKETS=false
ENABLE_SURGE_SCANNER=false
```

Use `python -m src.main --help` to inspect available download, backfill,
backtest, paper, account, position, and live-trading commands. Live execution
requires the explicit `--i-understand-live` flag.

## Tests

```powershell
cd D:\bitget_quant_system
python -m pytest -q
```

The repository includes unit and integration coverage for the candle store,
write buffer, streaming metadata, reconnect behavior, indicator refreshes,
and UI feature flags.

## Project Layout

- `src/`: quant runtime, data engine, strategies, execution, and UI.
- `tests/`: automated test suite.
- `data/`: local market-data and SQLite runtime files.
- `backend/` and `frontend/`: optional legacy data integrations kept separate
  from the Bitget quant runtime.
