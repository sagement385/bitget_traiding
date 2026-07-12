from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import pandas as pd

from src.markets import CRYPTO, KOR_STOCK, US_STOCK, normalize_market_type

DEFAULT_DB = "data/trading.db"
CANDLE_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume", "turnover", "symbol", "category", "interval", "market_type"
]
STOCK_MARKETS = frozenset({KOR_STOCK, US_STOCK})


def save_csv(df: pd.DataFrame, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path)


def _connect(db_path: str = DEFAULT_DB):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        con.close()
        raise
    return con


def _quarantine_corrupt_db(db_path: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base = Path(db_path)
    for candidate in [base, Path(str(base) + "-wal"), Path(str(base) + "-shm")]:
        if candidate.exists():
            target = candidate.with_name(f"{candidate.name}.corrupt-{stamp}")
            candidate.replace(target)


def _create_candles_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            market_type TEXT NOT NULL DEFAULT 'CRYPTO',
            symbol TEXT NOT NULL,
            category TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            turnover REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'bitget',
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000),
            PRIMARY KEY(market_type, symbol, category, interval, timestamp)
        )
        """
    )
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(candles)")}
    if "market_type" in columns:
        con.execute("CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(market_type, symbol, category, interval, timestamp)")


def _migrate_candles_market_type(con: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(candles)")}
    if not columns:
        _create_candles_table(con)
        return
    if "market_type" in columns:
        return
    # SQLite cannot add a new field to a composite primary key. Rebuild once and
    # classify all pre-market migration rows as CRYPTO so existing Bitget cache
    # keys remain valid and stock symbols cannot collide with crypto symbols.
    con.execute("DROP INDEX IF EXISTS idx_candles_lookup")
    con.execute("ALTER TABLE candles RENAME TO candles_legacy_market_v1")
    _create_candles_table(con)
    con.execute(
        """
        INSERT INTO candles(
            market_type, symbol, category, interval, timestamp,
            open, high, low, close, volume, turnover, source, updated_at
        )
        SELECT 'CRYPTO', symbol, category, interval, timestamp,
               open, high, low, close, volume, turnover,
               COALESCE(source, 'bitget'),
               COALESCE(updated_at, strftime('%s','now')*1000)
        FROM candles_legacy_market_v1
        """
    )
    con.execute("DROP TABLE candles_legacy_market_v1")


def _create_stock_symbols_table(con: sqlite3.Connection) -> None:
    """Create the local, provider-independent stock universe catalog.

    The universe is intentionally separate from candles. A provider refresh can
    update a stock name, exchange, or tradeability flag without touching the
    historical OHLCV cache, and a manually selected symbol can be retained
    until the official provider catalog is available.
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_symbols (
            market_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            name_en TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            is_tradeable INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000),
            PRIMARY KEY(market_type, symbol, exchange)
        )
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stock_symbols_search
        ON stock_symbols(market_type, symbol, name, name_en, exchange)
        """
    )


def _create_surge_scanner_tables(con: sqlite3.Connection) -> None:
    """Store the latest cache-first surge scan without growing history forever."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS surge_rankings (
            market_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT '',
            rank INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            snapshot_at INTEGER NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'local_catalog',
            PRIMARY KEY(market_type, symbol, exchange)
        )
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_surge_rankings_latest
        ON surge_rankings(market_type, score DESC, snapshot_at DESC)
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS surge_scanner_state (
            market_type TEXT PRIMARY KEY,
            cursor INTEGER NOT NULL DEFAULT 0,
            cycle_started_at INTEGER,
            cycle_scanned INTEGER NOT NULL DEFAULT 0,
            catalog_total INTEGER NOT NULL DEFAULT 0,
            last_run_at INTEGER,
            last_scan_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _init_schema(con: sqlite3.Connection) -> None:
    _create_candles_table(con)
    _migrate_candles_market_type(con)
    _create_stock_symbols_table(con)
    _create_surge_scanner_tables(con)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_flags (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS data_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_type TEXT NOT NULL DEFAULT 'CRYPTO',
            symbol TEXT NOT NULL,
            category TEXT NOT NULL,
            interval TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')*1000)
        )
        """
    )
    gap_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(data_gaps)")}
    if "market_type" not in gap_columns:
        con.execute("ALTER TABLE data_gaps ADD COLUMN market_type TEXT NOT NULL DEFAULT 'CRYPTO'")
    flag = con.execute(
        "SELECT value FROM maintenance_flags WHERE key='purged_legacy_bad_candles_v1'"
    ).fetchone()
    if not flag:
        # Older development builds could write rows with shifted columns because
        # the INSERT column order did not match the DataFrame order. SQLite's
        # dynamic typing lets those corrupt rows remain and they distort chart
        # autoscaling after interval changes. This table scan is expensive on a
        # full history cache, so run it once and remember that it completed.
        con.execute(
            """
            DELETE FROM candles
            WHERE typeof(timestamp) NOT IN ('integer','real')
               OR CAST(timestamp AS INTEGER) < 1000000000000
               OR typeof(open) NOT IN ('integer','real')
               OR typeof(high) NOT IN ('integer','real')
               OR typeof(low) NOT IN ('integer','real')
               OR typeof(close) NOT IN ('integer','real')
               OR (market_type='CRYPTO' AND category NOT LIKE '%FUTURES')
               OR interval NOT IN ('1s','3s','5s','15s','30s','1m','2m','3m','5m','15m','30m','1H','2H','4H','6H','12H','1D','1W','1M')
            """
        )
        con.execute(
            """
            INSERT OR REPLACE INTO maintenance_flags(key, value, updated_at)
            VALUES ('purged_legacy_bad_candles_v1', 'done', strftime('%s','now')*1000)
            """
        )


def init_db(db_path: str = DEFAULT_DB) -> None:
    try:
        with _connect(db_path) as con:
            _init_schema(con)
    except sqlite3.DatabaseError as exc:
        if "malformed" not in str(exc).lower() and "file is not a database" not in str(exc).lower():
            raise
        _quarantine_corrupt_db(db_path)
        with _connect(db_path) as con:
            _init_schema(con)


def upsert_candles(
    df: pd.DataFrame,
    db_path: str = DEFAULT_DB,
    source: str = "bitget",
    batch_size: int = 50_000,
    market_type: str | None = None,
) -> int:
    if df is None or df.empty:
        return 0
    init_db(db_path)
    d = df.copy()
    for col in CANDLE_COLUMNS:
        if col not in d.columns:
            if col == "turnover":
                d[col] = 0.0
            elif col == "category":
                d[col] = "USDT-FUTURES"
            elif col == "symbol":
                d[col] = "BTCUSDT"
            elif col == "interval":
                d[col] = "1m"
            elif col == "market_type":
                d[col] = normalize_market_type(market_type, d.get("category", pd.Series([None])).iloc[0] if len(d) else None)
            else:
                raise ValueError(f"missing candle column: {col}")
    d = d[CANDLE_COLUMNS].copy()
    d["timestamp"] = pd.to_numeric(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp", "open", "high", "low", "close"])
    d["timestamp"] = d["timestamp"].astype("int64")
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0).astype(float)
    d["market_type"] = [normalize_market_type(value, category) for value, category in zip(d["market_type"], d["category"])]
    d = d.drop_duplicates(["market_type", "symbol", "category", "interval", "timestamp"]).sort_values("timestamp")
    insert_cols = ["market_type", "symbol", "category", "interval", "timestamp", "open", "high", "low", "close", "volume", "turnover"]
    sql = """
        INSERT INTO candles(market_type, symbol, category, interval, timestamp, open, high, low, close, volume, turnover, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_type, symbol, category, interval, timestamp) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            turnover=excluded.turnover,
            source=excluded.source,
            updated_at=(strftime('%s','now')*1000)
    """
    batch_size = max(1, int(batch_size))
    total = 0
    batch = []
    with _connect(db_path) as con:
        for r in d[insert_cols].itertuples(index=False, name=None):
            batch.append(tuple(r) + (source,))
            if len(batch) >= batch_size:
                con.executemany(sql, batch)
                con.commit()
                total += len(batch)
                batch.clear()
        if batch:
            con.executemany(sql, batch)
            con.commit()
            total += len(batch)
    return total


def load_candles(symbol: str, category: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
                 limit: int | None = None, db_path: str = DEFAULT_DB, market_type: str | None = None) -> pd.DataFrame:
    init_db(db_path)
    normalized_market = normalize_market_type(market_type, category)
    params: list = [normalized_market, symbol.upper(), category, interval]
    where = "WHERE market_type=? AND symbol=? AND category=? AND interval=?"
    if start_ms is not None:
        where += " AND timestamp>=?"
        params.append(int(start_ms))
    if end_ms is not None:
        where += " AND timestamp<=?"
        params.append(int(end_ms))
    order = "ORDER BY timestamp ASC"
    lim = ""
    if limit is not None:
        # Get last N then re-sort ascending.
        q = f"SELECT timestamp, open, high, low, close, volume, turnover, symbol, category, interval, market_type FROM candles {where} ORDER BY timestamp DESC LIMIT ?"
        params2 = params + [int(limit)]
        with _connect(db_path) as con:
            df = pd.read_sql_query(q, con, params=params2)
        if df.empty:
            return df
        return df.sort_values("timestamp").reset_index(drop=True)
    q = f"SELECT timestamp, open, high, low, close, volume, turnover, symbol, category, interval, market_type FROM candles {where} {order} {lim}"
    with _connect(db_path) as con:
        return pd.read_sql_query(q, con, params=params)


def available_range(symbol: str, category: str, interval: str, db_path: str = DEFAULT_DB, market_type: str | None = None) -> tuple[int | None, int | None, int]:
    init_db(db_path)
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM candles WHERE market_type=? AND symbol=? AND category=? AND interval=?",
            (normalize_market_type(market_type, category), symbol.upper(), category, interval),
        ).fetchone()
    if not row or row[2] == 0:
        return None, None, 0
    return int(row[0]), int(row[1]), int(row[2])


def delete_candles(symbol: str, category: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
                   db_path: str = DEFAULT_DB, market_type: str | None = None) -> int:
    init_db(db_path)
    params: list = [normalize_market_type(market_type, category), symbol.upper(), category, interval]
    where = "WHERE market_type=? AND symbol=? AND category=? AND interval=?"
    if start_ms is not None:
        where += " AND timestamp>=?"
        params.append(int(start_ms))
    if end_ms is not None:
        where += " AND timestamp<=?"
        params.append(int(end_ms))
    with _connect(db_path) as con:
        cur = con.execute(f"DELETE FROM candles {where}", params)
        return int(cur.rowcount or 0)


def record_gap(symbol: str, category: str, interval: str, start_ts: int, end_ts: int, status: str, message: str = "", db_path: str = DEFAULT_DB, market_type: str | None = None) -> None:
    init_db(db_path)
    with _connect(db_path) as con:
        con.execute(
            "INSERT INTO data_gaps(market_type, symbol, category, interval, start_ts, end_ts, status, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (normalize_market_type(market_type, category), symbol.upper(), category, interval, int(start_ts), int(end_ts), status, message),
        )


def save_sqlite(df: pd.DataFrame, db_path: str, table='candles'):
    # Backwards-compatible wrapper. For candles, use upsert semantics.
    if table == 'candles':
        return upsert_candles(df, db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        df.to_sql(table, con, if_exists='append', index=False)


def _stock_default_exchange(market_type: str) -> str:
    return "KRX" if market_type == KOR_STOCK else "NASDAQ"


def _normalize_stock_symbol_row(row: dict[str, Any], default_source: str) -> tuple[Any, ...] | None:
    market_type = normalize_market_type(row.get("market_type"))
    if market_type not in STOCK_MARKETS:
        raise ValueError("Stock universe records require KOR_STOCK or US_STOCK")
    symbol = str(row.get("symbol", "") or "").upper().strip()
    if not symbol:
        return None
    exchange = str(row.get("exchange", row.get("category", "")) or _stock_default_exchange(market_type)).upper().strip()
    name = str(row.get("name", row.get("display_name", "")) or symbol).strip()
    name_en = str(row.get("name_en", row.get("english_name", "")) or "").strip()
    currency = str(row.get("currency", "KRW" if market_type == KOR_STOCK else "USD") or "").upper().strip()
    status = str(row.get("status", "active") or "active").lower().strip()
    raw_tradeable = row.get("is_tradeable", row.get("tradeable", status == "active"))
    is_tradeable = int(bool(raw_tradeable)) if not isinstance(raw_tradeable, str) else int(raw_tradeable.strip().lower() in {"1", "true", "yes", "y", "active"})
    source = str(row.get("source", default_source) or default_source).strip()
    metadata = row.get("metadata_json", row.get("metadata", {}))
    if isinstance(metadata, str):
        try:
            json.loads(metadata)
            metadata_json = metadata
        except json.JSONDecodeError:
            metadata_json = json.dumps({"raw": metadata}, ensure_ascii=True)
    else:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True)
    return market_type, symbol, exchange, name, name_en, currency, status, is_tradeable, source, metadata_json


def upsert_stock_symbols(
    rows: Iterable[dict[str, Any]] | pd.DataFrame,
    db_path: str = DEFAULT_DB,
    *,
    source: str = "manual",
) -> int:
    """Insert or update stock metadata without requesting market data."""
    init_db(db_path)
    if isinstance(rows, pd.DataFrame):
        records = rows.to_dict(orient="records")
    else:
        records = list(rows or [])
    normalized = [
        item for item in (_normalize_stock_symbol_row(dict(row), source) for row in records if isinstance(row, dict)) if item is not None
    ]
    if not normalized:
        return 0
    sql = """
        INSERT INTO stock_symbols(
            market_type, symbol, exchange, name, name_en, currency,
            status, is_tradeable, source, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_type, symbol, exchange) DO UPDATE SET
            name=excluded.name,
            name_en=excluded.name_en,
            currency=excluded.currency,
            status=excluded.status,
            is_tradeable=excluded.is_tradeable,
            source=excluded.source,
            metadata_json=excluded.metadata_json,
            updated_at=(strftime('%s','now')*1000)
    """
    with _connect(db_path) as con:
        # A user may open a ticker before provider metadata is available.  Once
        # a real catalog row arrives (for example KOSPI for a KRX cache key),
        # remove the duplicate non-tradeable placeholder but never touch its
        # cached candles.
        for market_type, symbol, exchange, _name, _name_en, _currency, _status, _tradeable, row_source, _metadata in normalized:
            if row_source != "manual_placeholder":
                con.execute(
                    """
                    DELETE FROM stock_symbols
                    WHERE market_type=? AND symbol=? AND source='manual_placeholder' AND exchange<>?
                    """,
                    (market_type, symbol, exchange),
                )
        con.executemany(sql, normalized)
    return len(normalized)


def search_stock_symbols(
    query: str = "",
    market_type: str = KOR_STOCK,
    *,
    limit: int = 30,
    db_path: str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Search a local catalog by ticker, local name, or English name."""
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Stock search requires KOR_STOCK or US_STOCK")
    text = str(query or "").strip()
    params: list[Any] = [market_type]
    where = "WHERE market_type=?"
    order_params: list[Any] = []
    if text:
        like = f"%{text.upper()}%"
        where += " AND (UPPER(symbol) LIKE ? OR UPPER(name) LIKE ? OR UPPER(name_en) LIKE ?)"
        params.extend([like, like, like])
        order = "ORDER BY CASE WHEN UPPER(symbol)=? THEN 0 WHEN UPPER(symbol) LIKE ? THEN 1 ELSE 2 END, is_tradeable DESC, symbol ASC, exchange ASC"
        order_params = [text.upper(), f"{text.upper()}%"]
    else:
        order = "ORDER BY is_tradeable DESC, symbol ASC, exchange ASC"
    sql = f"""
        SELECT market_type, symbol, exchange, name, name_en, currency, status,
               is_tradeable, source, metadata_json, updated_at
        FROM stock_symbols
        {where}
        {order}
        LIMIT ?
    """
    with _connect(db_path) as con:
        frame = pd.read_sql_query(sql, con, params=params + order_params + [max(1, min(int(limit), 100))])
    if frame.empty:
        return []
    rows = frame.to_dict(orient="records")
    for row in rows:
        row["is_tradeable"] = bool(row.get("is_tradeable"))
        try:
            row["metadata"] = json.loads(str(row.pop("metadata_json", "{}") or "{}"))
        except json.JSONDecodeError:
            row["metadata"] = {}
    return rows


def list_stock_symbols(
    market_type: str,
    *,
    tradeable_only: bool = False,
    limit: int | None = None,
    db_path: str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """List a local stock universe for a bounded scanner batch.

    This is deliberately a database-only operation.  It never asks Toss for a
    universe, because the official API only enriches explicitly known symbols.
    """
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Stock listing requires KOR_STOCK or US_STOCK")
    params: list[Any] = [market_type]
    where = "WHERE market_type=?"
    if tradeable_only:
        where += " AND is_tradeable=1"
    sql = f"""
        SELECT market_type, symbol, exchange, name, name_en, currency, status,
               is_tradeable, source, metadata_json, updated_at
        FROM stock_symbols
        {where}
        ORDER BY symbol ASC, exchange ASC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, min(int(limit), 100_000)))
    with _connect(db_path) as con:
        frame = pd.read_sql_query(sql, con, params=params)
    if frame.empty:
        return []
    rows = frame.to_dict(orient="records")
    for row in rows:
        row["is_tradeable"] = bool(row.get("is_tradeable"))
        try:
            row["metadata"] = json.loads(str(row.pop("metadata_json", "{}") or "{}"))
        except json.JSONDecodeError:
            row["metadata"] = {}
    return rows


def load_surge_scanner_state(market_type: str, db_path: str = DEFAULT_DB) -> dict[str, Any]:
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Surge scanner requires KOR_STOCK or US_STOCK")
    with _connect(db_path) as con:
        row = con.execute(
            """
            SELECT cursor, cycle_started_at, cycle_scanned, catalog_total,
                   last_run_at, last_scan_count, last_error
            FROM surge_scanner_state WHERE market_type=?
            """,
            (market_type,),
        ).fetchone()
    values = row or (0, None, 0, 0, None, 0, "")
    return {
        "market_type": market_type,
        "cursor": int(values[0] or 0),
        "cycle_started_at": int(values[1]) if values[1] is not None else None,
        "cycle_scanned": int(values[2] or 0),
        "catalog_total": int(values[3] or 0),
        "last_run_at": int(values[4]) if values[4] is not None else None,
        "last_scan_count": int(values[5] or 0),
        "last_error": str(values[6] or ""),
    }


def save_surge_scanner_state(
    market_type: str,
    state: dict[str, Any],
    db_path: str = DEFAULT_DB,
) -> None:
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    with _connect(db_path) as con:
        con.execute(
            """
            INSERT INTO surge_scanner_state(
                market_type, cursor, cycle_started_at, cycle_scanned,
                catalog_total, last_run_at, last_scan_count, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_type) DO UPDATE SET
                cursor=excluded.cursor,
                cycle_started_at=excluded.cycle_started_at,
                cycle_scanned=excluded.cycle_scanned,
                catalog_total=excluded.catalog_total,
                last_run_at=excluded.last_run_at,
                last_scan_count=excluded.last_scan_count,
                last_error=excluded.last_error
            """,
            (
                market_type,
                int(state.get("cursor", 0) or 0),
                state.get("cycle_started_at"),
                int(state.get("cycle_scanned", 0) or 0),
                int(state.get("catalog_total", 0) or 0),
                state.get("last_run_at"),
                int(state.get("last_scan_count", 0) or 0),
                str(state.get("last_error", "") or ""),
            ),
        )


def save_surge_rankings(
    rows: Iterable[dict[str, Any]],
    market_type: str,
    db_path: str = DEFAULT_DB,
) -> int:
    """Upsert only refreshed candidates; unscanned candidates retain their last score."""
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Surge rankings require KOR_STOCK or US_STOCK")
    records: list[tuple[Any, ...]] = []
    for row in rows or []:
        symbol = str(row.get("symbol", "") or "").upper().strip()
        if not symbol:
            continue
        metrics = row.get("metrics", {})
        records.append((
            market_type,
            symbol,
            str(row.get("exchange", "") or "").upper().strip(),
            int(row.get("rank", 0) or 0),
            float(row.get("score", 0) or 0),
            int(row.get("snapshot_at", 0) or 0),
            json.dumps(metrics if isinstance(metrics, dict) else {}, ensure_ascii=True, sort_keys=True),
            str(row.get("source", "local_catalog") or "local_catalog"),
        ))
    if not records:
        return 0
    with _connect(db_path) as con:
        con.executemany(
            """
            INSERT INTO surge_rankings(
                market_type, symbol, exchange, rank, score, snapshot_at,
                metrics_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_type, symbol, exchange) DO UPDATE SET
                rank=excluded.rank,
                score=excluded.score,
                snapshot_at=excluded.snapshot_at,
                metrics_json=excluded.metrics_json,
                source=excluded.source
            """,
            records,
        )
    return len(records)


def load_surge_rankings(
    market_type: str,
    *,
    limit: int = 100,
    db_path: str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Return current rankings only for symbols still present in the local catalog."""
    init_db(db_path)
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Surge rankings require KOR_STOCK or US_STOCK")
    with _connect(db_path) as con:
        frame = pd.read_sql_query(
            """
            SELECT r.market_type, r.symbol, r.exchange, r.rank, r.score,
                   r.snapshot_at, r.metrics_json, r.source,
                   s.name, s.name_en, s.currency, s.status, s.is_tradeable
            FROM surge_rankings r
            INNER JOIN stock_symbols s
                ON s.market_type=r.market_type
               AND s.symbol=r.symbol
               AND s.exchange=r.exchange
            WHERE r.market_type=?
            ORDER BY r.score DESC, r.snapshot_at DESC, r.symbol ASC
            LIMIT ?
            """,
            con,
            params=[market_type, max(1, min(int(limit), 100))],
        )
    if frame.empty:
        return []
    rows = frame.to_dict(orient="records")
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["is_tradeable"] = bool(row.get("is_tradeable"))
        try:
            row["metrics"] = json.loads(str(row.pop("metrics_json", "{}") or "{}"))
        except json.JSONDecodeError:
            row["metrics"] = {}
    return rows


def get_stock_symbol(
    symbol: str,
    market_type: str,
    *,
    exchange: str | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any] | None:
    market_type = normalize_market_type(market_type)
    if market_type not in STOCK_MARKETS:
        raise ValueError("Stock lookup requires KOR_STOCK or US_STOCK")
    text = str(symbol or "").upper().strip()
    if not text:
        return None
    matches = search_stock_symbols(text, market_type, limit=100, db_path=db_path)
    wanted_exchange = str(exchange or "").upper().strip()
    for row in matches:
        if row["symbol"] == text and (not wanted_exchange or row["exchange"] == wanted_exchange):
            return row
    return None


def ensure_stock_symbol(
    symbol: str,
    market_type: str,
    *,
    exchange: str | None = None,
    name: str | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    """Create a non-tradeable placeholder for a manually entered ticker.

    A later official-universe import replaces this record with verified
    exchange/name/tradeability metadata through the same upsert key.
    """
    market_type = normalize_market_type(market_type)
    existing = get_stock_symbol(symbol, market_type, exchange=exchange, db_path=db_path)
    if existing:
        return existing
    text = str(symbol or "").upper().strip()
    if not text:
        raise ValueError("symbol is required")
    chosen_exchange = str(exchange or _stock_default_exchange(market_type)).upper().strip()
    upsert_stock_symbols(
        [{
            "market_type": market_type,
            "symbol": text,
            "exchange": chosen_exchange,
            "name": str(name or text),
            "currency": "KRW" if market_type == KOR_STOCK else "USD",
            "status": "pending_metadata",
            "is_tradeable": False,
            "source": "manual_placeholder",
            "metadata": {"needs_provider_verification": True},
        }],
        db_path,
        source="manual_placeholder",
    )
    created = get_stock_symbol(text, market_type, exchange=chosen_exchange, db_path=db_path)
    if not created:
        raise RuntimeError("Unable to create stock symbol placeholder")
    return created


def stock_universe_status(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    init_db(db_path)
    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT market_type, COUNT(*) AS total,
                   SUM(CASE WHEN is_tradeable=1 THEN 1 ELSE 0 END) AS tradeable,
                   MAX(updated_at) AS updated_at
            FROM stock_symbols
            GROUP BY market_type
            """
        ).fetchall()
    by_market = {
        str(market): {
            "total": int(total or 0),
            "tradeable": int(tradeable or 0),
            "updated_at": int(updated_at) if updated_at is not None else None,
        }
        for market, total, tradeable, updated_at in rows
    }
    for market in STOCK_MARKETS:
        by_market.setdefault(market, {"total": 0, "tradeable": 0, "updated_at": None})
    return {"markets": by_market, "total": sum(item["total"] for item in by_market.values())}
