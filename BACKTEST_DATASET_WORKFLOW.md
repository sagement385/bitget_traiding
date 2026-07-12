# Dataset-Driven Backtest and Automated Trading

## Principle

Charts are presentation only. Backtests and automated execution never read a
browser chart, indicator pane, or visible-range state. They consume a validated
SQLite candle dataset identified by `market_type`, `symbol`, `category`, and
`interval`.

```text
Provider REST (only missing/recent 1m rows)
    -> SQLite canonical candle dataset
    -> strict market-aware dataset validation + fingerprint
    -> backtest or last-closed-candle strategy decision
    -> paper/live broker boundary
```

## Backtest Dataset Contract

`src/backtest/dataset.py` creates one immutable `BacktestDataset` before the
engine starts. It verifies:

- required OHLCV fields, numeric values, timestamps, uniqueness, and order
- valid OHLC price bounds and non-negative volume
- no unresolved crypto interval gaps
- no in-session stock interval gaps
- no Korean/US stock rows outside the configured regular session

Overnight, weekend, and market-close gaps are valid for stocks. They are not
treated as missing candles. A missing candle inside 09:00-15:30 KST for Korea
or 09:30-16:00 New York time for the US rejects the dataset.

The dataset hash is stored as `dataset_id`, with row count, first/last
timestamps, market, interval, and execution model. Backtest artifacts are
written under:

```text
results/backtests/<dataset_id-prefix>/
    dataset.json
    metrics.json
    trades.csv
    equity_curve.csv
    trade_levels.json
```

## No-Lookahead Execution Model

For row `i`:

1. The strategy receives only rows `0` through `i-1`; all are completed bars.
2. Its signal is executed at row `i` open with configured slippage and fees.
3. Stop, target, and trailing logic then uses row `i` high/low/close as the
   conservative intrabar outcome after an open execution.

This model is recorded as:

```text
signal_on_completed_bar_execute_next_bar_open
```

## Metrics

Sharpe annualization is now market and interval aware:

- crypto: 365 x 24-hour calendar trading
- Korean/US stocks: 252 trading days x 390 regular-session minutes
- day, week, and month candles use their appropriate annual bar counts

The returned metrics include `periods_per_year`, so the scale can be audited.

## Automated Execution

`src/execution/realtime_engine.py` now loads the recent canonical SQLite
dataset first. Provider API calls are restricted to filling missing or recent
1-minute candles. It evaluates the last completed candle once, tracks its
timestamp, and never uses UI chart data as a signal input.

Examples:

```powershell
# Bitget paper mode
python -m src.main paper --symbol BTCUSDT --product-type USDT-FUTURES --market-type CRYPTO --interval 5m --once

# Toss Korean stock paper mode; reads/persists Toss 1m data and derives 5m
python -m src.main paper --symbol 005930 --product-type KRX --market-type KOR_STOCK --interval 5m --once
```

Paper mode may calculate a signal, but it does not send a live Toss order.
`demo` and `live` modes retain their explicit safety gates; Toss order
submission still requires an intentional `safe_mode=False` path.

## Verification

Completed after the dataset migration:

- 61 automated tests passed.
- A no-lookahead test asserts each strategy call receives data only through
  the previous completed candle.
- A session-aware stock test accepts overnight gaps and rejects an intraday
  missing one-minute candle.
- A real SQLite `005930` KRX 5-minute dataset ran without chart input.
- A Toss Korean-stock paper execution run used the canonical dataset and
  produced a signal without submitting an order.
