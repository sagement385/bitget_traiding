# Bitget Quant System: Implementation Overview

## Purpose

The chart system is cache-first. It uses local SQLite data as the primary source and requests only missing or current data from Bitget. This prevents full historical downloads whenever the application opens.

```text
Bitget REST and WebSocket
        -> canonical one-minute candles in SQLite
        -> derived chart intervals
        -> chart API and live WebSocket
        -> price, volume, and lower indicators on one time scale
```

The implementation is designed to:

- Reuse validated historical data from SQLite.
- Fetch and persist only missing intervals and the current in-progress candle.
- Keep 1m through 1M boundaries aligned with Bitget.
- Start live mode from the displayed chart tail instead of reloading the chart.
- Keep price, volume, and lower indicators synchronized.

## Stored Candle Data

One-minute candles are the canonical source of truth. They are stored in the SQLite `candles` table using symbol, category, interval, and timestamp as the effective unique key.

- A repeated timestamp updates the existing row instead of creating duplicates.
- Derived intervals such as 2m, 3m, 5m, 15m, hourly, daily, weekly, and monthly candles are aggregated from one-minute data.
- Derived results are persisted as well, so later chart loads do not need to aggregate the entire historical range again.

Relevant modules:

- `src/data_engine/storage.py`: SQLite schema, upsert, reads, and available-range checks.
- `src/data_engine/canonical.py`: one-minute repair, gap detection, and interval aggregation.

## Exchange-Aligned Time Boundaries

Daily, weekly, and monthly aggregation uses UTC+8 boundaries to match Bitget chart sessions.

- Daily candles begin at UTC+8 midnight.
- Weekly candles begin at UTC+8 Monday midnight.
- Monthly candles begin at UTC+8 on the first day of the month.
- Multi-hour buckets use the same exchange-aligned time basis.

This prevents date labels and candle contents from shifting when changing between intraday, daily, weekly, and monthly charts.

## Normal Chart Load Flow

When a chart opens:

1. The requested interval is read from SQLite first.
2. Only missing time ranges are identified.
3. Bitget REST requests are limited to those gaps.
4. The most recent five minutes can be refreshed to keep the in-progress candle accurate.
5. New one-minute data and derived interval candles are upserted into SQLite.
6. The UI displays the latest 6,000 candles for fast rendering.

Validated historical data is not downloaded again. For example, if the cache already reaches March 2026, only the span from March 2026 to the current time is requested when it is absent.

## Gap Repair

Gap detection uses expected one-minute timestamp continuity.

- Missing ranges are requested from Bitget only for the affected span.
- Returned one-minute rows are persisted.
- The requested chart interval is re-aggregated and persisted.
- Historical ranges that have already been validated are not rechecked as part of routine chart loading.

## Live Mode Continuity

Live mode uses the existing chart tail as its cursor.

```text
Last displayed candle time
        -> /ws/live?from=<last candle time>
        -> repair only the span from that cursor to now
        -> send catchup candles to the client
        -> merge WebSocket updates into the existing chart
```

The server helper `_repair_live_gap()` follows these rules:

- It applies to standard candle intervals, excluding second-based charts.
- It starts at the client-provided final candle, not at the beginning of history.
- It requests only missing one-minute rows in that range.
- It refreshes the most recent five minutes for the current candle.
- It persists both repaired source data and derived candles.
- It never initiates a full historical validation or full historical download during live startup.

The live WebSocket can send:

- `catchup`: candles between the current chart tail and the present.
- `snapshot`: a recent cached or exchange candle snapshot after connection.
- `candle_update`: an update for the current candle.
- `status`: reconnect and transport information.

## Client-Side Candle Merging

`mergeLiveCandles()` merges live data instead of replacing the chart dataset.

- Existing timestamps are updated with the newest OHLCV values.
- New timestamps are appended.
- Duplicate timestamps are removed.
- The latest 6,000 candles are retained.
- The visible time range is captured before the merge and restored afterward.
- A user following the newest candles remains at the right edge as new data arrives.
- A user inspecting older history keeps the same viewed time range.
- Live startup does not run `fitContent()` when the chart already contains candles.

This retains the current chart position, markers, and selected indicators when live mode starts.

Relevant module:

- `src/ui/app.py`: chart API, live WebSocket, `_repair_live_gap()`, `mergeLiveCandles()`, and `startLive()`.

## Volume and Indicator Synchronization

The price pane, volume pane, and lower indicator panes share synchronized time ranges.

- Dragging or zooming the price chart moves lower panes to the same range.
- Changing from an intraday interval to daily, weekly, or monthly recomputes volume and indicators from that interval's candles.
- A live candle update also updates volume and lower indicator data.
- Only selected indicators are calculated, reducing initial chart load cost.

## Live Data Sources

- Native Bitget Kline WebSockets are used for supported candle intervals.
- Two-minute candles are created from the one-minute Kline stream and persisted.
- Second-based charts use a trade-based stream.
- Incoming live source and derived candles are stored in SQLite for later reuse.

## Verification

The automated suite currently reports:

```text
48 passed
```

Coverage includes UTC+8 day/week/month boundaries, selected-indicator calculation, current-candle refresh, live-gap repair from the chart cursor, and client-side catchup merging.

An actual local WebSocket verification used a cursor 45 minutes before the present and received:

```text
catchup: 3 candles, db_gap_repair, 5 recent one-minute rows refreshed
snapshot: 1,801 candles, rest_or_cache
```

Browser verification also loaded 6,000 cached 15-minute candles, started live mode without resetting the chart, and received current-candle WebSocket updates.

## Expected Operational Behavior

- Initial data collection can take time when a requested period is not yet cached.
- Once data is stored, chart opens are cache-first and fast.
- Restarting the application does not trigger a full historical download.
- Starting live mode later fills only the missing span from the existing chart to the current time.
- Repaired data remains in SQLite and is reused on the next launch.

## Python Module Map

This section covers the active Python code under the top-level `src` directory, plus the active tests and operational scripts. It does not describe the nested `bitget_quant_system/src` copy because the application is launched from the top-level `src` package.

### Package Markers

The following files are empty package markers. They establish import boundaries and contain no runtime business logic:

- `src/__init__.py`
- `src/backtest/__init__.py`
- `src/bitget/__init__.py`
- `src/data_engine/__init__.py`
- `src/execution/__init__.py`
- `src/indicators/__init__.py`
- `src/monitor/__init__.py`
- `src/strategies/__init__.py`
- `src/ui/__init__.py`
- `src/utils/__init__.py`

### Application Entry and Shared Models

#### `src/main.py`

This is the command-line entry point. It creates command arguments, builds strategy-specific settings, and starts the requested mode such as the UI, data collection, backtest, paper operation, or controlled live/demo operation. It centralizes CLI safety switches so real order execution is not enabled by an accidental default.

#### `src/models.py`

Defines shared dataclasses used across strategy, execution, and backtest code:

- `Signal`: target position, reason, confidence, and optional metadata.
- `OrderRequest`: order fields plus validation for side and quantity.
- `Position`: in-memory position state.
- `Trade`: normalized executed-trade record.

Using typed models keeps strategies independent from broker-specific request shapes.

### Bitget API Layer

#### `src/bitget/public_client.py`

Implements the public Bitget market-data client.

- Maps application intervals to Bitget granularities.
- Fetches REST candle ranges in API-safe chunks.
- Uses a process-level rate limiter, retries, backoff, and parallel chunk collection.
- Normalizes Bitget rows into the application OHLCV DataFrame schema.
- Builds unsupported native 2-minute candles from one-minute source candles.
- Handles UTC+8 fallback aggregation for weekly and monthly bars.

This file is the REST source used by initial historical download, gap repair, and recent-candle refresh.

#### `src/bitget/private_client.py`

Implements authenticated Bitget Futures REST requests. It loads credentials from `.env`, constructs signed headers, handles request retries, and exposes account, positions, order placement, cancellation, detail lookup, and close-position operations. It does not bypass broker safety checks; those checks are enforced by the execution layer before requests are sent.

#### `src/bitget/signer.py`

Builds the Bitget authentication timestamp and HMAC signature from the method, request path including query string, and body. Keeping signing here makes private-client request construction deterministic and testable.

#### `src/bitget/websocket_client.py`

Wraps the public Bitget WebSocket connection. It manages subscriptions, reconnection, message dispatch, and topic construction for Kline and trade streams. Kline topics are used for native candle updates; trade topics are used for second-based aggregation.

### Data Engine

#### `src/data_engine/storage.py`

Owns SQLite storage.

- Initializes the `candles` and gap-related schema.
- Quarantines malformed database files and rejects corrupted or shifted candle rows.
- Upserts OHLCV rows in batches, keyed by symbol/category/interval/timestamp.
- Loads bounded ranges or latest rows efficiently.
- Reports stored ranges and records unresolved gaps.

It is the persistence boundary that allows the UI to be cache-first.

#### `src/data_engine/canonical.py`

Implements the canonical one-minute pipeline.

- Treats one-minute candles as the source for derived intervals.
- Calculates exchange-aligned bucket starts, including UTC+8 day/week/month boundaries.
- Detects only missing one-minute ranges inside the requested window.
- Optionally forces a small recent refresh for the active candle.
- Aggregates open, high, low, close, volume, and turnover correctly from one-minute rows.
- Persists derived intervals and returns a report with fetched rows and gap counts.

The live-gap repair path and normal chart cache path both use this module.

#### `src/data_engine/backfill.py`

Provides bulk historical collection tools.

- Downloads one-minute data over an explicit range.
- Processes full history in monthly chunks to avoid one large unstable request.
- Rebuilds every derived interval from the stored one-minute data.
- Can export compatibility CSV files while keeping SQLite as the primary cache.

This is the operational tool for initial full-history loading or rebuilding a damaged derived cache.

#### `src/data_engine/historical_loader.py`

Provides the older generic cache-first loader for direct interval requests. It checks cache coverage, requests only missing tails or ranges, optionally repairs gaps, stores results, and returns a `LoadReport`. The canonical one-minute loader is preferred for standard chart intervals because it guarantees one shared source of truth.

#### `src/data_engine/candle_stream.py`

Handles live Kline-based candles.

- `LiveCandleStream` consumes native Bitget Kline messages, replaces the in-progress candle by timestamp, and persists updates.
- `LiveSyntheticCandleStream` consumes one-minute Klines and aggregates them into a synthetic two-minute stream.
- `merge_candle_frames()` merges old and new DataFrames without duplicate timestamps.

This is used by the UI WebSocket for native and two-minute live charts.

#### `src/data_engine/tick_engine.py`

Handles trade-tick-based short intervals.

- Parses Bitget trade messages into typed ticks.
- Stores recent tick data in SQLite.
- Buckets ticks into second-based OHLCV candles.
- Uses the same exchange-aligned bucketing rules for daily-or-larger boundaries.
- Emits updates through `LiveTickCandleStream`.

This is used when a standard Kline stream is not suitable for second charts.

#### `src/data_engine/validator.py`

Validates candle quality before a backtest or cache operation relies on it. It normalizes numeric fields, detects fixed-interval and monthly gaps, and verifies timestamp order and OHLC validity. Backtests reject gapped candles instead of silently producing invalid results.

#### `src/data_engine/demo_data.py`

Creates deterministic synthetic OHLCV candles for local development and tests. It allows the UI, indicators, and strategies to operate without an API request.

### Indicator Layer

#### `src/indicators/library.py`

Contains the main Pandas indicator implementations: SMA, EMA, RSI, true range, ATR, VWAP, Bollinger Bands, MACD, OBV, Donchian channels, Stochastic, SuperTrend, and volume SMA.

`add_common_indicators()` accepts an `include` set. The UI passes only the selected indicators, preventing unnecessary computation on a 6,000-candle chart. This is especially important for iterative SuperTrend calculation.

#### `src/indicators/momentum.py`

Re-exports momentum indicators such as RSI, MACD, Stochastic, and OBV from the common library. Strategy code imports this narrower module when it only needs momentum calculations.

#### `src/indicators/trend.py`

Re-exports trend helpers and adds simple rolling Donchian high/low helpers used by breakout strategies.

#### `src/indicators/volatility.py`

Re-exports true range, ATR, and Bollinger functions for volatility-focused strategy code.

### Strategy Layer

#### `src/strategies/base.py`

Defines the abstract `Strategy` contract. Every strategy must produce one normalized `Signal` from candles and optional portfolio/position state.

#### `src/strategies/factory.py`

Maps UI and CLI strategy names to concrete strategy classes. It supports aliases and rejects unknown strategy names before a backtest starts.

#### `src/strategies/sma_cross.py`

Implements a fast/slow SMA crossover strategy. It compares the previous and latest bar to identify actual cross events, returns a hold signal when no crossing occurs, and optionally allows short signals.

#### `src/strategies/rsi_reversal.py`

Implements a basic RSI mean-reversion strategy. It enters long below a lower RSI threshold, optionally enters short above an upper threshold, and returns neutral otherwise.

#### `src/strategies/breakout.py`

Implements a Donchian breakout strategy. It compares the latest close against the previous completed channel boundary to avoid using the current bar's own high/low as the breakout reference.

#### `src/strategies/scalp_vwap_rsi.py`

Implements an intraday candidate strategy using VWAP, EMA 9/21/50, RSI, MACD histogram, ATR, and relative volume.

- Entries require trend, momentum, volatility, and liquidity filters.
- Existing positions use ATR stop loss, ATR take profit, and momentum exits.
- Signal metadata includes indicator values and stop/take settings for backtest display.

It is a systematic candidate, not a profitability guarantee.

#### `src/strategies/multi_timeframe_momentum.py`

Implements a no-lookahead multi-timeframe momentum strategy.

- Starts from historical one-minute rows already available to the backtest loop.
- Builds only completed 5-minute, 15-minute, and 1-hour groups.
- Uses trend votes across those completed groups with volume and ATR filters.
- Supplies risk metadata for the backtest position-management logic.

It deliberately avoids using future candles when deriving higher-timeframe signals.

#### `src/strategies/price_action_volume.py`

Implements a price-action and relative-volume pullback candidate strategy. It identifies an impulse, waits for a pullback into the impulse region, requires confirmation, and emits metadata for initial stop, targets, trailing logic, and R-multiple management.

### Backtest Layer

#### `src/backtest/engine.py`

Runs candle-by-candle historical simulation.

- Validates candle continuity before execution.
- Calls strategies using only rows available before the current decision point.
- Applies fees, slippage, long/short rules, entries, exits, and equity marking.
- Supports ATR-based stops, partial profit taking, break-even movement, locked profit, and dynamic trailing behavior when strategy metadata enables it.
- Writes equity curves, trades, metrics, charts, and trade-level output files.

#### `src/backtest/metrics.py`

Calculates performance metrics from the backtest output: final equity, total return, maximum drawdown, Sharpe ratio, trade count, win rates, average profit/loss, profit factor, average win/loss ratio, and maximum consecutive losses.

### Execution and Risk Layer

#### `src/execution/broker_base.py`

Defines the abstract broker interface with `place_order()` and `cancel_order()` methods. It allows strategies and real-time engines to use paper and live brokers through a common shape.

#### `src/execution/paper_broker.py`

Implements a simple in-memory paper broker. Orders are marked filled immediately and can later be marked canceled for test and paper workflows.

#### `src/execution/live_bitget_broker.py`

Wraps the authenticated private client for demo/live order flow.

- Converts normalized orders into Bitget request fields.
- Runs risk validation before transmission.
- Defaults to `safe_mode=True`, blocking order placement, cancellation, and close-all writes.
- Requires an explicit operating mode change before real exchange write actions are possible.

#### `src/execution/order_manager.py`

Maintains a lightweight in-memory map from client order ID to order and status. It is a simple state holder for workflows that need to track submitted requests.

#### `src/execution/position_manager.py`

Maintains in-memory positions by symbol and checks internal versus external position state. A mismatch raises an error so automated execution can be halted instead of continuing with an unknown exposure.

#### `src/execution/realtime_engine.py`

Provides REST-polling real-time strategy execution as a fallback to WebSockets.

- Fetches a rolling candle window.
- Processes only the most recently closed candle, not the still-forming candle.
- Prevents duplicate processing by timestamp.
- Sends resulting orders through paper or explicitly configured live/demo brokers.
- Appends decision events to JSONL logs.

#### `src/execution/risk_manager.py`

Defines configurable execution guards: kill switch, API/WebSocket health, minimum balance, maximum leverage, maximum single-order notional, maximum exposure, daily loss limit, and consecutive-loss limit.

### UI Layer

#### `src/ui/app.py`

This is the FastAPI backend and embedded browser chart application.

Backend responsibilities:

- Serves the chart page and REST endpoints for candles, indicators, latest candles, and backtests.
- Uses SQLite-first chart loading and calls canonical repair only for missing/current ranges.
- Builds serializable candlestick, volume, indicator, trade, equity, drawdown, and return payloads.
- Provides `/ws/live`, which accepts the client `from` cursor, calls `_repair_live_gap()`, sends `catchup`, then streams live Kline or tick updates.
- Keeps live startup scoped to the existing chart tail through the present rather than historical revalidation.

Browser responsibilities implemented in the embedded JavaScript:

- Uses Lightweight Charts price, volume, and lower-indicator panes with synchronized visible ranges.
- Loads candles first and selected indicators asynchronously.
- Recomputes indicators when the interval or enabled indicator set changes.
- Merges `catchup`, `snapshot`, and `candle_update` messages with `mergeLiveCandles()`.
- Preserves the visible range for users viewing history and follows the right edge for users viewing the newest candle.
- Keeps live mode active across supported interval changes.

### Support Utilities

#### `src/monitor/logger.py`

Creates named loggers with a timestamped file handler and console handler for operational diagnostics.

#### `src/utils/env.py`

Loads simple `KEY=VALUE` entries from `.env` into environment variables without overwriting variables already supplied by the host environment.

#### `src/utils/rounding.py`

Provides step-size floor rounding and tick-size price rounding for exchange-safe quantities and prices.

#### `src/utils/time.py`

Defines interval durations, converts ISO strings or numeric values into milliseconds, and returns current epoch milliseconds. It is shared by REST range construction and candle bucketing.

### Tests and Operational Scripts

#### `tests/test_core.py`

Contains the regression suite. It covers candle normalization, storage corruption handling, API chunking, UTC+8 bucket boundaries, canonical gap repair, cache-first behavior, UI payload contracts, lower-pane synchronization, live cursor/catchup behavior, indicator selection, strategy signal shape, backtest continuity rejection, risk controls, and backfill aggregation.

#### `scripts/tail_rebuild_cache.py`

Operational maintenance script for refreshing the recent cache tail and rebuilding the affected derived intervals. It is useful when only the newest cache portion needs repair.

#### `scripts/validate_bitget_chart_match.py`

Validation script that compares locally derived candles against Bitget REST candles for supported intervals and reports timestamp/OHLCV differences or missing intervals. It is intended for confirming that local chart construction matches the exchange.
