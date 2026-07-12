# Multi-Market and Toss Integration - Detailed Implementation Guide

> **Status update (2026-06-25):** This document records the original
> pre-contract architecture. The official Toss Open API contract has now been
> mapped to OAuth2 REST endpoints. For current credentials, endpoints,
> cache/polling behavior, and verification status, use `TOSS_INTEGRATION.md`
> as the authoritative operational document. Historical references below to
> configurable Toss paths or a future Toss WebSocket are superseded there.

## 1. Purpose and Current Scope

This document records the multi-market extension added to the existing Bitget
quant system. The extension keeps the original crypto workflow intact while
adding an explicit architecture for Korean and US equities through Toss
Securities adapters.

The implementation has four practical goals:

1. Keep crypto, Korean equities, and US equities as separate data domains.
2. Use the same cache-first 1-minute candle pipeline for every market.
3. Stop expensive inactive-market work immediately when the UI changes market.
4. Keep a deliberately small risk-monitoring loop for an inactive market only
   when that market has an open position.

The code is designed so that a candle is never identified only by a symbol.
For example, BTCUSDT in a crypto market and an identically named stock symbol
in another market cannot overwrite each other in SQLite.

## 2. Important Safety Boundary for Toss Securities

The Toss adapters are implemented as configuration-driven integration points.
They intentionally do not hard-code unverified private API URLs, request field
names, WebSocket topics, or order schemas.

Why this matters:

- A trading integration must use the official, approved Toss Securities API
  contract for the specific account and service.
- Guessing an endpoint or an order field is unsafe. A read request can fail
  harmlessly, but an incorrectly guessed order request is unacceptable.
- The current code fails explicitly with TossApiConfigurationError when the
  required endpoint configuration is absent. It does not silently call an
  invented URL.
- LiveTossBroker starts in safe_mode=True. It validates an order but refuses
  to submit or cancel it until safe mode is deliberately disabled after the
  official contract and a paper-trading validation are complete.

This means the system structure is implemented and testable now. The final
vendor-specific URL, authentication, payload, response, and subscription
mapping must be filled from approved Toss documentation or an approved sandbox
response before enabling live stock trading.

## 3. Architecture After the Change

### 3.1 Market domains

The application now recognizes these market_type values:

| Market type | Meaning | Candle timezone | Regular session policy |
| --- | --- | --- | --- |
| CRYPTO | Bitget cryptocurrency market | Asia/Shanghai, UTC+8 | 24 hours, every day |
| KOR_STOCK | Korean equities | Asia/Seoul, UTC+9 | Weekdays, 09:00 to 15:30 |
| US_STOCK | US equities | America/New_York | Weekdays, 09:30 to 16:00 |

The US market uses the IANA timezone America/New_York rather than a fixed UTC
offset. Python therefore applies EST or EDT automatically for calendar
boundaries, daily candles, weekly candles, monthly candles, and session checks.

### 3.2 Data flow

    Provider REST or WebSocket
              |
              v
       normalized 1-minute OHLCV frame
              |
              v
    SQLite candles table, partitioned by market_type
              |
              v
    canonical aggregation for 2m, 3m, 5m, 15m, 1H, 1D, 1W, 1M
              |
              v
       FastAPI endpoint and synchronized UI charts

The 1-minute candle is the canonical source of truth. Higher intervals are
derived from it instead of mixing independently fetched provider candles. This
prevents a 15-minute chart, volume chart, and indicator chart from using
different time boundaries.

### 3.3 Runtime resource flow

    UI market tab click
              |
              v
    POST /api/market/switch
              |
              v
    MarketStateManager.switch_active(market_type)
              |
              +-- cancel every inactive market heavy task
              |   - chart WebSocket forwarding
              |   - high-frequency candle processing
              |   - live chart render feed
              |
              +-- retain only inactive markets with known positions
                  - lightweight REST position polling
                  - no inactive chart stream

When a market becomes active again, its normal chart/live feed is started from
the existing SQLite cache and any gap between the cached tail and the current
provider time is repaired. It is not replaced with an unrelated fresh chart.

## 4. File-by-File Implementation

### 4.1 src/markets.py

Responsibility: Define the market taxonomy and the market calendar rules in one
place.

Implemented components:

- CRYPTO, KOR_STOCK, and US_STOCK constants.
- MarketSpec dataclass containing the market type, IANA timezone, and optional
  regular-session open and close times.
- MARKET_SPECS registry for the three supported market types.
- normalize_market_type(value) validates input and defaults missing legacy
  callers to CRYPTO.
- market_spec(market_type, category) returns the selected policy.
- market_local_datetime(timestamp_ms, market_type) converts a UTC timestamp to
  the market-local timezone.
- is_regular_session_timestamp(timestamp_ms, market_type) rejects stock bars
  outside their weekday regular trading session while allowing crypto at all
  times.

Why it is implemented this way:

- Timezone code previously belonged implicitly to crypto behavior. If each
  caller applied its own offset, daily or monthly boundaries would drift.
- IANA timezone names handle daylight-saving time correctly. A fixed US UTC
  offset would make daily candles one hour wrong for part of the year.
- Session filtering is central rather than duplicated in the API, chart, and
  aggregation code. This makes the rule testable and prevents a volume panel
  from receiving off-session candles that the price panel excludes.

Current limitation:

- The baseline stock session rule knows weekdays and normal market hours. It
  does not yet include a provider-specific holiday and early-close calendar.
  Once Toss supplies a trading-calendar endpoint, that calendar should extend
  this module. Provider candles remain the authoritative source for holidays.

### 4.2 src/data_engine/storage.py

Responsibility: Own SQLite creation, legacy migration, cache isolation, upsert,
lookup, range inspection, deletion, and recorded gap storage.

Schema change in candles:

    market_type TEXT NOT NULL DEFAULT 'CRYPTO'
    PRIMARY KEY (market_type, symbol, category, interval, timestamp)

The lookup index uses the same identity components. The market type is the
first key because it is a hard domain boundary, not merely a display filter.

Migration behavior:

1. Open the existing database and inspect PRAGMA table_info(candles).
2. If market_type already exists, ensure the new market-aware index exists.
3. If it does not exist, rename the old candles table to a temporary legacy
   name.
4. Create the new candles table with the new primary key.
5. Copy every legacy candle into the new table with market_type='CRYPTO'.
6. Drop the temporary legacy table after the copy succeeds.
7. Add market_type to data_gaps when that legacy table needs migration.

Why a rebuild is used instead of only ALTER TABLE:

- SQLite cannot change an existing primary key in place. Adding a simple column
  would leave the old primary key active and would still allow a collision
  between markets.
- Rebuilding makes the new identity constraint real, not cosmetic.
- Mapping legacy rows to CRYPTO preserves all previously downloaded Bitget
  history without asking the provider to download it again.

Changed storage APIs:

- upsert_candles(..., market_type=...)
- load_candles(..., market_type=...)
- available_range(..., market_type=...)
- delete_candles(..., market_type=...)
- record_gap(..., market_type=...)

All of these default to CRYPTO to preserve existing Bitget callers. Upsert
deduplicates using the full market-aware key and updates OHLCV values when the
same candle is received again. This is necessary because a currently open bar
can change until it closes.

The production database migration was also verified after implementation:

- Legacy crypto rows were retained under CRYPTO.
- The legacy temporary table was absent after successful migration.
- SQLite checkpointing was performed so the persisted database, rather than a
  transient WAL file, contains the verified migration state.

### 4.3 src/data_engine/canonical.py

Responsibility: Make cached 1-minute candles canonical, find only missing
regions, fetch patches, and aggregate higher intervals deterministically.

Key implementation details:

- bucket_start_ms(timestamp, interval, market_type) now receives market type.
- Small fixed intervals such as 1m, 2m, 3m, 5m, 15m, and 1H use their expected
  bucket calculation.
- 6H and 12H use the local market hour rather than a universal date boundary.
- 1D, 1W, and 1M are floored in the market-local calendar.
- ensure_1m_range() loads the cache first, finds missing leading, trailing,
  and internal minute ranges, merges adjacent requests, then fetches only those
  ranges.
- load_or_fetch_canonical_candles() requests enough 1-minute data to cover the
  requested higher-interval buckets, aggregates it, persists the result, and
  returns a report explaining the cache/fetch result.
- aggregate_from_1m() filters Korean and US source candles through
  is_regular_session_timestamp() before grouping. Crypto data remains 24/7.

Why this solves candle/date mismatch problems:

- A daily candle must start at the market's local day, not at UTC midnight and
  not at a browser-local boundary.
- A US daily candle cannot use Korean local time; it must use New York local
  time including DST.
- Price, volume, and lower indicators all use the same aggregated frame. They
  therefore share timestamp, bar count, and x-axis positions.
- The cache scanner requests only empty intervals. A verified historical range
  remains local and does not need to be downloaded again whenever the UI opens.

Gap repair behavior:

1. Load the existing 1-minute cache for the requested time window.
2. Detect unrepresented ranges inside the requested window.
3. Also detect missing head or tail ranges when the user requests outside the
   available cache range.
4. Request the smallest merged provider ranges possible.
5. Normalize and upsert the returned patches.
6. Aggregate from the repaired canonical 1-minute set.

This gives the desired cache-first behavior: historical data is re-used, only
gaps between history and current time are requested, and repaired data is
stored for later sessions.

### 4.4 src/toss/public_client.py

Responsibility: Provide a safe REST data adapter for Korean and US stock candles.

Implemented components:

- TossApiConfigurationError makes unconfigured integration failures explicit.
- TossPublicEndpoints reads the API base URL and separate Korean/US candle
  paths from environment variables.
- normalize_toss_candles() translates common camelCase and snake_case candle
  fields into the shared timestamp, open, high, low, close, volume schema.
- TossPublicClient.get_recent_candles() supports a recent live/catch-up request.
- TossPublicClient.download_candles() supports historical range requests.

Why separate Korean and US paths exist:

- Providers often expose different symbols, exchanges, currency conventions,
  request fields, or data routes for local and foreign equities.
- It is safer to expose that distinction in configuration than assume one
  endpoint serves both markets with identical semantics.

Required environment variables for real use:

    TOSS_API_BASE_URL
    TOSS_KOR_CANDLES_PATH
    TOSS_US_CANDLES_PATH

The exact parameter names and pagination details must be checked against the
approved Toss contract. The current normalizer is deliberately isolated so a
vendor response change is contained to this adapter rather than leaking into
the data engine.

### 4.5 src/toss/private_client.py

Responsibility: Encapsulate account, position, order, and cancel REST calls.

Implemented components:

- TossPrivateEndpoints reads account, positions, order, and cancellation paths
  from configuration.
- TossPrivateClient reads the issued API key and secret from environment
  variables, but blocks private traffic until the official request-signing
  contract is mapped.
- get_account(), get_positions(), place_order(), and cancel_order() are the
  private integration boundary.

Why this is separate from public_client.py:

- Public market data and authenticated trading use different security and
  failure profiles.
- The public candle path can run without private credentials. Order code must not
  accidentally execute because a chart fetcher is present.
- Keeping private calls separate makes permission audits and later credential
  rotation simpler.

Required variables for real private calls include:

    TOSS_API_KEY
    TOSS_SECRET_KEY
    TOSS_ALLOWED_IP
    TOSS_ACCOUNT_PATH
    TOSS_POSITIONS_PATH
    TOSS_ORDER_PATH
    TOSS_CANCEL_PATH

### 4.6 src/toss/websocket_client.py

Responsibility: Hold the provider-specific real-time streaming connection and
reconnection behavior behind one async interface.

Implemented behavior:

- Reads TOSS_WS_URL from the environment.
- Opens a WebSocket connection only when configured and when the approved
  WebSocket authentication contract has been mapped.
- Sends a subscription frame containing market type, symbol, and interval.
- Yields received provider messages to the UI live-feed layer.
- Reconnects after a transient failure rather than terminating the entire API
  server.

Why this adapter is intentionally generic:

- WebSocket subscription payloads and authentication procedures are vendor
  contract details. They must be mapped from approved documentation.
- The rest of the application only needs normalized candle rows; it should not
  need to know the vendor's topic naming convention.

### 4.7 src/execution/live_toss_broker.py

Responsibility: Provide a Toss implementation of the existing Broker contract.

LiveTossBroker:

- Inherits from the application's broker base abstraction.
- Accepts only KOR_STOCK and US_STOCK order requests.
- Uses RiskManager validation before a request reaches the private client.
- Converts an OrderRequest to the adapter's provider payload boundary.
- Starts with safe_mode=True.
- Blocks submit and cancellation requests in safe mode.

Why safe_mode defaults to true:

- A configured token should not automatically turn a development server into a
  live trading system.
- The correct rollout sequence is: official API mapping, read-only position
  check, sandbox/paper order test, explicit risk limits, then a deliberate
  safe_mode=False deployment decision.

The broker skeleton is the correct place for quantity rounding, market-specific
order types, tick-size rules, order-state mapping, broker error translation,
and idempotency once the official contract is available.

### 4.8 src/execution/market_state_manager.py

Responsibility: Enforce the resource policy when the user changes market tabs.

Implemented structures:

- MarketRuntimeState stores active heavy connections, risk-polling state, and
  known position count for each market.
- MarketStateManager owns the active market, registered position probes,
  registered risk-poll hooks, background task sets, and state snapshots.
- attach_heavy_task() and detach_heavy_task() identify chart/WebSocket work.
- switch_active() marks a market active, cancels heavy work for all inactive
  markets, and evaluates whether each inactive market needs risk polling.
- update_known_position_count() immediately starts or stops an inactive
  market's lightweight risk loop when its position count changes.
- snapshot() provides an inspectable API representation for the UI and tests.

Resource policy:

| State | WebSocket/chart processing | REST position polling |
| --- | --- | --- |
| Active market | Allowed | Allowed when needed |
| Inactive market, no positions | Cancelled | Stopped |
| Inactive market, positions exist | Cancelled | Retained at lightweight interval |

Why the known position count exists:

- A missing Toss credential or a temporary provider failure must not make the
  manager assume a real position is zero.
- Position probes may return None to mean "unknown". In that case the previous
  known state is retained rather than incorrectly terminating risk observation.
- A successful position update can start or stop polling immediately rather
  than waiting for the next tab change.

The default risk polling interval is 20 seconds. It is intentionally not a
chart-rate loop. This loop only maintains the minimal information needed for
future stop-loss/take-profit policy enforcement.

### 4.9 src/models.py

Responsibility: Carry market identity through shared execution models.

Changes:

- OrderRequest now has market_type with CRYPTO as the backwards-compatible
  default.
- Position now has market_type with the same default.

Why this change is necessary:

- Broker routing must know whether an order belongs to Bitget, Korean equities,
  or US equities.
- Risk and position records must never become ambiguous once the application
  holds more than one asset class.

### 4.10 src/ui/app.py

Responsibility: Expose the multi-market FastAPI contract and keep all chart
panels synchronized with one candle context.

Main API additions and changes:

| Endpoint or component | Behavior |
| --- | --- |
| GET /api/candles | Accepts market_type, uses cache-first canonical candles, returns market context with data. |
| POST /api/indicators | Receives the current market candle frame so indicators use identical timestamps. |
| GET /api/latest-candles | Reads current provider data for live catch-up, normalizes and persists it. |
| POST /api/backtest | Carries market context through the request path. |
| GET /api/market/state | Returns active market and heavy/risk task state. |
| POST /api/market/switch | Applies MarketStateManager resource policy. |
| POST /api/market/positions | Updates known position count and starts/stops inactive risk polling. |
| WebSocket /ws/live | Binds the live stream to one market, registers it as heavy work, repairs cached tail gaps, and unregisters on disconnect. |

UI changes:

- Added Crypto, KR Stocks, and US Stocks market controls.
- Market switching changes sensible default symbol/category pairs:
  BTCUSDT and USDT-FUTURES for crypto, 005930 and KRX for Korean equities,
  AAPL and NASDAQ for US equities.
- The selected market type is included in candle, indicator, latest-candle,
  backtest, and live WebSocket requests.
- Starting live mode uses the currently rendered cached chart as its base.
  New candles are merged into that frame; the UI does not discard history and
  redraw an unrelated short sample.
- Before streaming, the live path repairs only the gap from the cached chart
  tail to current provider time. It then writes that repair to SQLite.
- Price, volume, and lower indicator panes are updated from the same candle
  dataset. Panning, zooming, interval selection, and date-range changes stay
  aligned instead of making independent requests for each panel.

Why indicators and volume now move together:

- All panels are derived from the response that represents the active symbol,
  market type, interval, start, and end.
- The browser code checks response context before applying asynchronous data.
  A late response for an old market or interval is ignored instead of replacing
  the current chart.
- On a timeframe change, the application requests/aggregates the selected
  timeframe once and recomputes dependent indicators from that exact result.

### 4.11 .env.example and config.yaml

Responsibility: Document deploy-time market-specific configuration without
putting credentials into source code.

.env.example now lists Toss base URL, Korean/US candle paths, private endpoint
paths, WebSocket URL, token, and account ID placeholders.

config.yaml documents the intended market policies: crypto uses UTC+8, Korean
equities use Seoul session rules, and US equities use New York session rules.

Why both files are useful:

- Environment variables are appropriate for secrets and environment-specific
  vendor URLs.
- Versioned YAML is appropriate for visible non-secret runtime policy.

### 4.12 TOSS_INTEGRATION.md

Responsibility: Provide the operational contract for completing a real Toss
integration.

It documents the safe-mode behavior, required environment keys, the need to
map approved provider fields, and the recommended validation sequence before
live order submission is enabled.

### 4.13 tests/test_core.py

Responsibility: Lock down existing behavior and test the new multi-market
contracts without requiring a live trading account.

Added/updated coverage includes:

- Legacy SQLite candle migration assigns existing rows to CRYPTO.
- Candles with the same symbol can coexist across separate market types.
- Korean and US local calendar bucket behavior is verified.
- Korean off-session source minutes are excluded from aggregated candles.
- Switching active market cancels inactive heavy tasks.
- An inactive market with a position retains its risk loop.
- Updating inactive known position count starts and stops the loop correctly.
- Unconfigured Toss public calls fail explicitly before any guessed network
  request is made.
- UI source contains the market-switch API contract and market context flow.

Latest full local result after implementation:

    54 passed in approximately 10 to 13 seconds

## 5. Operational Sequences

### 5.1 First historical load

1. The requested symbol, market type, interval, start, and end arrive at
   GET /api/candles.
2. The canonical layer checks SQLite for 1-minute history in the requested
   span.
3. It identifies only missing segments.
4. The correct provider client fetches those segments.
5. The normalized data is stored with its market_type.
6. Higher intervals are aggregated from the complete cached-and-repaired
   1-minute series.
7. The UI receives one frame for price, volume, and indicators.

### 5.2 Later application launch

1. Existing SQLite history is loaded immediately.
2. The application does not request the entire historical period again.
3. It requests only data missing after the latest cached candle or inside a
   detected empty region.
4. Returned patches are saved, so the next launch has a smaller or zero gap.

### 5.3 Turning live mode on

1. Existing rendered candles remain visible.
2. The live endpoint finds the last cached/rendered candle.
3. REST catch-up fills the interval up to current time when necessary.
4. The repair is persisted to SQLite.
5. The live WebSocket stream appends or updates the current candle.
6. Volume and lower indicators are recomputed/updated from the same merged
   candle series.

### 5.4 Switching from crypto to Korean or US stocks

1. The browser closes the previous live chart connection.
2. The browser calls POST /api/market/switch.
3. The manager cancels all inactive heavy tasks.
4. The selected market is allowed to open its chart/live stream.
5. For an inactive market with known open positions, only lightweight REST
   position polling remains.
6. With no position, that inactive market has neither WebSocket activity nor
   background candle derivation work.

## 6. Verification Performed

The implementation was checked at three levels:

1. Automated tests: the complete suite passed with 54 tests.
2. Database migration: existing crypto records were preserved under CRYPTO and
   the old non-market-aware table was removed after migration.
3. UI/API smoke checks:
   - The local application served correctly.
   - Crypto to Korean market switching updated symbol/category and market state.
   - An unconfigured Toss market displayed an explicit configuration failure,
     which is expected and preferable to an ambiguous blank chart.
   - Returning to crypto restored BTCUSDT and USDT-FUTURES context.
   - POST /api/market/positions was verified to enable risk polling for an
     inactive market with a positive position count and disable it at zero.

## 7. Required Steps Before Enabling Real Toss Trading

1. Obtain the official approved Toss Securities API documentation and account
   permissions for Korean and US equities.
2. Fill the Toss URLs and credentials in a private .env file, never in source
   control.
3. Map actual request/response field names in the Toss adapters.
4. Save sanitized fixture responses and add tests for pagination, timezone,
   symbol/exchange identifiers, error messages, and WebSocket events.
5. Validate read-only candle and position endpoints first.
6. Validate an explicitly non-live or paper order flow.
7. Define per-market tick size, lot size, currency, order types, commission,
   trading calendar, and stop-loss/take-profit policy.
8. Only then set LiveTossBroker safe_mode=False in a deliberate deployment
   configuration, with risk limits tested independently.

## 8. Known Boundaries and Future Extensions

- The system currently has regular-session weekday filtering for stocks, not a
  complete holiday/early-close exchange calendar.
- The state manager preserves a lightweight polling loop for a position but it
  does not independently submit stop-loss or take-profit orders. That policy
  belongs in an explicit, tested risk/execution rule after the provider order
  contract is confirmed.
- The Toss adapter is ready for a real contract but cannot be considered a
  production live connector until official endpoints and schemas are mapped.
- Backtest strategy quality is separate from data correctness. Any new stock or
  crypto strategy should use chronological walk-forward validation and avoid
  using future candles while generating a decision.

## 9. Maintenance Rules

When adding another provider or market, keep these invariants:

1. Add a distinct market_type and MarketSpec before adding UI controls.
2. Include market_type in every persistence identity, cache path, API request,
   live stream, order request, position, and test fixture.
3. Normalize provider data to canonical 1-minute OHLCV before aggregation.
4. Aggregate calendar intervals in the market's local timezone.
5. Treat inactive chart work as cancellable heavy work.
6. Leave only explicitly justified risk monitoring for inactive positions.
7. Do not enable live order submission by default for a new provider.

Following these rules keeps the original Bitget system stable while making the
stock integrations independently configurable, testable, and safe to complete.
