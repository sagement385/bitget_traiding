# Toss Securities Open API Integration

## Current Status

The Toss adapter now follows the published Open API contract at
`https://developers.tossinvest.com/docs`.

- Official REST base URL: `https://openapi.tossinvest.com`
- Authentication: OAuth 2.0 Client Credentials
- Market data: authenticated REST requests
- Live stock chart transport: low-rate REST polling
- Order mode: implemented at the API boundary but still blocked by
  `LiveTossBroker(safe_mode=True)` by default

The official guide currently states that WebSocket support is planned for a
future release. No guessed WebSocket URL or subscription frame is used.

## Environment

The WTS calls the credentials **API Key** and **Secret Key**. The official
OAuth request calls the same values `client_id` and `client_secret`.

```text
TOSS_API_KEY=...
TOSS_SECRET_KEY=...
TOSS_ALLOWED_IP=...

# Optional aliases; take precedence when present.
TOSS_CLIENT_ID=
TOSS_CLIENT_SECRET=

# Optional accountSeq from GET /api/v1/accounts. When blank, the client finds
# the first BROKERAGE account for read-only account/holding requests.
TOSS_ACCOUNT_SEQ=

# REST polling period used only while a stock live chart is active.
TOSS_LIVE_POLL_SECONDS=2
```

`TOSS_ALLOWED_IP` is registered in the Toss WTS console. It is not sent as an
HTTP header. The API and secret keys must never be placed in UI JavaScript,
logs, test fixtures, or documentation.

## OAuth Token Handling

`src/toss/auth.py` requests:

```text
POST /oauth2/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
client_id=<API Key>
client_secret=<Secret Key>
```

Every subsequent request uses `Authorization: Bearer <access_token>`.
Toss permits one active token per client, so `TossOAuthClient` keeps a
process-wide token cache with an expiry margin. Public chart loading, account
reads, and inactive-market risk polling share that cache rather than issuing
competing token requests.

## Market Data and Cache Policy

`src/toss/public_client.py` maps the official endpoints:

| Purpose | Endpoint | Notes |
| --- | --- | --- |
| Candles | `GET /api/v1/candles` | `symbol`, `interval`, `count`, `before`, `adjusted` |
| Stock information | `GET /api/v1/stocks` | Up to 200 explicitly supplied symbols |
| Current prices | `GET /api/v1/prices` | Up to 200 explicitly supplied symbols |

The provider returns only `1m` and `1d` candles, with at most 200 rows per
response. The adapter pages backward with the exclusive ISO-8601 `before`
cursor and respects the `MARKET_DATA_CHART` 5 TPS limit. Successful `1m` rows
are stored in SQLite. `src/data_engine/canonical.py` derives `2m`, `3m`, `5m`,
hourly, daily, weekly, and monthly charts from that local 1-minute source.

When a stock chart opens, the canonical loader requests only regular-session
holes plus the small recent tail, persists the patch, and reuses SQLite on the
next open. Korean charts use `Asia/Seoul` with the configured regular session
of 09:00-15:30; US charts use `America/New_York` and automatically respect
EST/EDT in timestamp bucketing.

The official API does **not** provide an all-market stock-universe download.
The UI therefore supports local name/ticker search and enriches a selected
known ticker through `/api/v1/stocks`. A full searchable Korean/US universe
requires a separately licensed symbol catalog import; it must not be faked
from a partial Toss lookup response.

## Live Charts and Resource Switching

`/ws/live` is still the browser-to-FastAPI live channel. For stocks it starts
one REST polling task only while that market is active:

1. It keeps the already rendered SQLite-backed chart.
2. It repairs only the gap from the displayed chart cursor to the present.
3. It polls official Toss `1m` candles at `TOSS_LIVE_POLL_SECONDS` (2 seconds
   by default, below Toss's 5 TPS candle limit).
4. It stores raw rows, derives the selected interval locally, and sends only
   changed current candles to the browser.

`MarketStateManager` cancels the active chart task immediately after a market
switch. An inactive market with an open position retains only the 20-second
read-only holdings/risk loop; it has no live chart polling or chart rendering
task.

## Account, Holdings, and Orders

`src/toss/private_client.py` maps these official REST endpoints:

| Purpose | Endpoint | Additional requirement |
| --- | --- | --- |
| Accounts | `GET /api/v1/accounts` | Bearer token |
| Holdings | `GET /api/v1/holdings` | `X-Tossinvest-Account: accountSeq` |
| Create order | `POST /api/v1/orders` | Bearer token, account header |
| Cancel order | `POST /api/v1/orders/{orderId}/cancel` | server-issued `orderId`, account header |

`LiveTossBroker` maps `LIMIT`/`MARKET`, `BUY`/`SELL`, decimal-string quantity,
price, `DAY`/`CLS`, and `clientOrderId` according to the published order
schema. It remains safe by default: no create, modify, or cancel request is
sent until code explicitly constructs it with `safe_mode=False` and the caller
has made a separate live-trading decision.

## Verification Performed

The local credentials were verified without revealing key material:

- OAuth token request succeeded against the official Toss server.
- `005930` 1-minute candles were fetched and normalized into the SQLite schema.
- A canonical 5-minute KRX chart was derived from those 1-minute rows.
- `AAPL` 1-minute US market data was fetched successfully.
- Read-only account discovery and holdings lookup succeeded with the required
  `X-Tossinvest-Account` header.
- The automated regression suite completed with `59 passed`.

No live order was submitted during verification.
