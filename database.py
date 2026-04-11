"""
SQLite persistence layer for the trading agent.
Stores trades, volume data, wallet snapshots, and spike events.
All state survives restarts.
"""

import aiosqlite
import os
from datetime import datetime
from typing import Any

import config


async def get_db() -> aiosqlite.Connection:
    """Return a connection to the SQLite database, creating it if needed."""
    os.makedirs(os.path.dirname(config.SQLITE_DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(config.SQLITE_DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, faster than FULL
    await db.execute("PRAGMA busy_timeout=5000")    # wait up to 5s on lock contention
    await db.execute("PRAGMA wal_autocheckpoint=100")  # checkpoint WAL after 100 pages
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create all tables and run migrations if needed."""
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migrations: add new columns to existing tables without dropping data.
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
                await db.commit()
            except Exception:
                pass  # Column already exists — ignore
    finally:
        await db.close()

# ALTER TABLE migrations run once; SQLite raises if column already exists (caught above).
_MIGRATIONS = [
    "ALTER TABLE positions ADD COLUMN token_id TEXT DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN exit_target REAL DEFAULT NULL",
    # Self-improvement layer
    "ALTER TABLE positions ADD COLUMN market_regime TEXT DEFAULT 'unknown'",
    "ALTER TABLE positions ADD COLUMN max_favorable_excursion REAL DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN max_adverse_excursion REAL DEFAULT NULL",
    # Migrate adaptive_params if it exists with old float schema
    "ALTER TABLE adaptive_params ADD COLUMN value_json TEXT DEFAULT NULL",
]


SCHEMA = """
-- Tracked top-performing wallets
CREATE TABLE IF NOT EXISTS tracked_wallets (
    address         TEXT PRIMARY KEY,
    rank            INTEGER NOT NULL,
    total_profit    REAL NOT NULL DEFAULT 0,
    trade_count     INTEGER NOT NULL DEFAULT 0,
    win_rate        REAL NOT NULL DEFAULT 0,
    last_refreshed  TEXT NOT NULL,
    notes           TEXT DEFAULT ''
);

-- Positions we hold
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    market_slug     TEXT NOT NULL DEFAULT '',
    market_question TEXT NOT NULL DEFAULT '',
    outcome         TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    side            TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    entry_price     REAL NOT NULL,
    size            REAL NOT NULL,
    cost_basis      REAL NOT NULL,
    strategy        TEXT NOT NULL CHECK(strategy IN ('copy_trade', 'volume_spike')),
    source_wallet   TEXT DEFAULT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT DEFAULT NULL,
    exit_price      REAL DEFAULT NULL,
    pnl             REAL DEFAULT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed', 'cancelled')),
    token_id        TEXT DEFAULT NULL,
    exit_target     REAL DEFAULT NULL,
    notes           TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);

-- Order execution log (every order attempted)
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER REFERENCES positions(id),
    market_id       TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'limit',
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'filled', 'partially_filled', 'cancelled', 'failed')),
    polymarket_order_id TEXT DEFAULT NULL,
    error_message   TEXT DEFAULT NULL,
    created_at      TEXT NOT NULL,
    filled_at       TEXT DEFAULT NULL,
    retries         INTEGER NOT NULL DEFAULT 0
);

-- Volume tracking buckets (12-hour windows)
CREATE TABLE IF NOT EXISTS volume_buckets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    bucket_start    TEXT NOT NULL,
    bucket_end      TEXT NOT NULL,
    volume          REAL NOT NULL DEFAULT 0,
    trade_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(market_id, outcome, bucket_start)
);

CREATE INDEX IF NOT EXISTS idx_volume_market ON volume_buckets(market_id, outcome);

-- Volume spike events
CREATE TABLE IF NOT EXISTS spike_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    market_slug     TEXT NOT NULL DEFAULT '',
    outcome         TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    spike_magnitude REAL NOT NULL,
    rolling_avg     REAL NOT NULL,
    recent_volume   REAL NOT NULL,
    price_wall      INTEGER NOT NULL DEFAULT 0,
    trend_aligned   INTEGER NOT NULL DEFAULT 0,
    trade_decision  TEXT NOT NULL CHECK(trade_decision IN ('enter', 'fade', 'skip')),
    rationale       TEXT DEFAULT '',
    position_id     INTEGER DEFAULT NULL REFERENCES positions(id),
    outcome_correct INTEGER DEFAULT NULL,
    price_move      REAL DEFAULT NULL,
    resolved_at     TEXT DEFAULT NULL
);

-- Wallet balance snapshots
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    balance         REAL NOT NULL,
    total_exposure  REAL NOT NULL DEFAULT 0,
    available       REAL NOT NULL DEFAULT 0
);

-- Copy trade wallet performance tracking
CREATE TABLE IF NOT EXISTS copy_trade_stats (
    wallet_address  TEXT PRIMARY KEY,
    trades_copied   INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    total_pnl       REAL NOT NULL DEFAULT 0,
    last_copied_at  TEXT DEFAULT NULL
);

-- Strategy doc update log
CREATE TABLE IF NOT EXISTS doc_updates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    updated_at      TEXT NOT NULL,
    update_type     TEXT NOT NULL,
    content_hash    TEXT NOT NULL
);

-- User field observations submitted via the dashboard
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'X/Twitter',
    market_tag  TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    acted_on    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at);

-- Adaptive parameters updated by the nightly reflection loop (JSON values)
CREATE TABLE IF NOT EXISTS adaptive_params (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Signal attribution: tracks which signals actually predicted profitable trades
CREATE TABLE IF NOT EXISTS signal_performance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name     TEXT NOT NULL,
    signal_value    REAL NOT NULL,
    signal_bucket   TEXT NOT NULL,      -- 'low'/'medium'/'high', 'has_wall'/'no_wall', etc.
    position_id     INTEGER REFERENCES positions(id),
    market_regime   TEXT NOT NULL DEFAULT 'unknown',
    was_profitable  INTEGER NOT NULL DEFAULT 0,
    pnl             REAL DEFAULT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signal_perf_name ON signal_performance(signal_name, signal_bucket);
CREATE INDEX IF NOT EXISTS idx_signal_perf_regime ON signal_performance(market_regime);
"""


# ── Helper functions ────────────────────────────────────────────────────────

async def insert_position(
    db: aiosqlite.Connection,
    market_id: str,
    market_slug: str,
    market_question: str,
    outcome: str,
    side: str,
    entry_price: float,
    size: float,
    strategy: str,
    source_wallet: str | None = None,
    token_id: str | None = None,
    exit_target: float | None = None,
    notes: str = "",
    market_regime: str = "unknown",
) -> int:
    """Insert a new open position and return its ID."""
    cost_basis = entry_price * size
    cursor = await db.execute(
        """INSERT INTO positions
           (market_id, market_slug, market_question, outcome, side, entry_price,
            size, cost_basis, strategy, source_wallet, opened_at,
            token_id, exit_target, notes, market_regime)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (market_id, market_slug, market_question, outcome, side, entry_price,
         size, cost_basis, strategy, source_wallet,
         datetime.utcnow().isoformat(), token_id, exit_target, notes, market_regime),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def close_position(
    db: aiosqlite.Connection,
    position_id: int,
    exit_price: float,
) -> float:
    """Close a position, calculate P&L, return P&L."""
    row = await db.execute_fetchall(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    )
    if not row:
        raise ValueError(f"Position {position_id} not found")

    pos = dict(row[0])
    if pos["side"] == "BUY":
        pnl = (exit_price - pos["entry_price"]) * pos["size"]
    else:
        pnl = (pos["entry_price"] - exit_price) * pos["size"]

    await db.execute(
        """UPDATE positions SET status='closed', closed_at=?, exit_price=?, pnl=?
           WHERE id=?""",
        (datetime.utcnow().isoformat(), exit_price, pnl, position_id),
    )
    await db.commit()
    return pnl


async def get_open_positions(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all currently open positions."""
    rows = await db.execute_fetchall(
        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
    )
    return [dict(r) for r in rows]


async def get_total_exposure(db: aiosqlite.Connection) -> float:
    """Sum of cost_basis for all open positions."""
    rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(cost_basis), 0) as total FROM positions WHERE status='open'"
    )
    return float(rows[0]["total"])


async def insert_order(
    db: aiosqlite.Connection,
    position_id: int | None,
    market_id: str,
    outcome: str,
    side: str,
    price: float,
    size: float,
    order_type: str = "limit",
) -> int:
    """Log an order attempt."""
    cursor = await db.execute(
        """INSERT INTO orders
           (position_id, market_id, outcome, side, price, size, order_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (position_id, market_id, outcome, side, price, size, order_type,
         datetime.utcnow().isoformat()),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def update_order_status(
    db: aiosqlite.Connection,
    order_id: int,
    status: str,
    polymarket_order_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update an order's status after execution attempt."""
    filled_at = datetime.utcnow().isoformat() if status == "filled" else None
    await db.execute(
        """UPDATE orders SET status=?, polymarket_order_id=?, error_message=?, filled_at=?
           WHERE id=?""",
        (status, polymarket_order_id, error_message, filled_at, order_id),
    )
    await db.commit()


async def insert_volume_bucket(
    db: aiosqlite.Connection,
    market_id: str,
    outcome: str,
    bucket_start: str,
    bucket_end: str,
    volume: float,
    trade_count: int,
) -> None:
    """Upsert a volume bucket."""
    await db.execute(
        """INSERT INTO volume_buckets (market_id, outcome, bucket_start, bucket_end, volume, trade_count)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(market_id, outcome, bucket_start)
           DO UPDATE SET volume=volume+excluded.volume, trade_count=trade_count+excluded.trade_count""",
        (market_id, outcome, bucket_start, bucket_end, volume, trade_count),
    )
    await db.commit()


async def get_rolling_volume(
    db: aiosqlite.Connection,
    market_id: str,
    outcome: str,
    since: str,
) -> tuple[float, float]:
    """Return (total_volume, avg_volume_per_bucket) since the given timestamp."""
    rows = await db.execute_fetchall(
        """SELECT COALESCE(SUM(volume), 0) as total, COUNT(*) as buckets
           FROM volume_buckets
           WHERE market_id=? AND outcome=? AND bucket_start >= ?""",
        (market_id, outcome, since),
    )
    total = float(rows[0]["total"])
    buckets = int(rows[0]["buckets"])
    avg = total / max(buckets, 1)
    return total, avg


async def insert_spike_event(
    db: aiosqlite.Connection,
    market_id: str,
    market_slug: str,
    outcome: str,
    spike_magnitude: float,
    rolling_avg: float,
    recent_volume: float,
    price_wall: bool,
    trend_aligned: bool,
    trade_decision: str,
    rationale: str,
    position_id: int | None = None,
) -> int:
    """Log a detected volume spike event."""
    cursor = await db.execute(
        """INSERT INTO spike_events
           (market_id, market_slug, outcome, detected_at, spike_magnitude,
            rolling_avg, recent_volume, price_wall, trend_aligned,
            trade_decision, rationale, position_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (market_id, market_slug, outcome, datetime.utcnow().isoformat(),
         spike_magnitude, rolling_avg, recent_volume,
         int(price_wall), int(trend_aligned), trade_decision, rationale,
         position_id),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def record_balance_snapshot(
    db: aiosqlite.Connection,
    balance: float,
    total_exposure: float,
) -> None:
    """Store a point-in-time balance snapshot."""
    await db.execute(
        """INSERT INTO balance_snapshots (timestamp, balance, total_exposure, available)
           VALUES (?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), balance, total_exposure,
         balance - total_exposure),
    )
    await db.commit()


async def get_adaptive_param_json(
    db: aiosqlite.Connection,
    key: str,
) -> Any | None:
    """Retrieve a JSON-encoded adaptive parameter, or None if not set."""
    import json
    rows = await db.execute_fetchall(
        "SELECT value_json FROM adaptive_params WHERE key = ?", (key,)
    )
    if rows and rows[0]["value_json"]:
        try:
            return json.loads(rows[0]["value_json"])
        except (ValueError, KeyError):
            return None
    return None


async def set_adaptive_param_json(
    db: aiosqlite.Connection,
    key: str,
    value: Any,
) -> None:
    """Upsert a JSON-encoded adaptive parameter."""
    import json
    await db.execute(
        """INSERT INTO adaptive_params (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value_json = excluded.value_json,
               updated_at = excluded.updated_at""",
        (key, json.dumps(value), datetime.utcnow().isoformat()),
    )
    await db.commit()


async def record_signal_performance(
    db: aiosqlite.Connection,
    signal_name: str,
    signal_value: float,
    signal_bucket: str,
    position_id: int | None,
    market_regime: str,
    was_profitable: bool,
    pnl: float | None = None,
) -> None:
    """Record a signal outcome for post-hoc attribution analysis."""
    await db.execute(
        """INSERT INTO signal_performance
           (signal_name, signal_value, signal_bucket, position_id,
            market_regime, was_profitable, pnl, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (signal_name, signal_value, signal_bucket, position_id,
         market_regime, int(was_profitable), pnl,
         datetime.utcnow().isoformat()),
    )
    await db.commit()


async def update_position_excursions(
    db: aiosqlite.Connection,
    position_id: int,
    max_favorable: float | None,
    max_adverse: float | None,
) -> None:
    """Update the peak (MFE) and trough (MAE) excursion values on a position."""
    await db.execute(
        """UPDATE positions
           SET max_favorable_excursion = ?,
               max_adverse_excursion   = ?
           WHERE id = ?""",
        (max_favorable, max_adverse, position_id),
    )
    await db.commit()


async def get_regime_stats(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return per-regime trade performance for the STRATEGY_DOC.md analytics section."""
    rows = await db.execute_fetchall(
        """SELECT market_regime,
                  COUNT(*) AS trades,
                  ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate,
                  ROUND(SUM(pnl), 2) AS total_pnl
           FROM positions
           WHERE status = 'closed'
             AND market_regime IS NOT NULL
             AND market_regime != 'unknown'
           GROUP BY market_regime
           ORDER BY total_pnl DESC"""
    )
    return [dict(r) for r in rows]


async def get_strategy_stats(db: aiosqlite.Connection) -> dict[str, Any]:
    """Aggregate stats for the strategy doc."""
    total_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM positions WHERE status='closed'"
    )
    total_trades = int(total_rows[0]["cnt"])

    win_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM positions WHERE status='closed' AND pnl > 0"
    )
    wins = int(win_rows[0]["cnt"])

    pnl_rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM positions WHERE status='closed'"
    )
    total_pnl = float(pnl_rows[0]["total"])

    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    # Per-wallet copy trade stats (closed only — for win rate / P&L)
    copy_stats = await db.execute_fetchall(
        """SELECT source_wallet, COUNT(*) as trades,
                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                  COALESCE(SUM(pnl), 0) as pnl
           FROM positions
           WHERE strategy='copy_trade' AND status='closed' AND source_wallet IS NOT NULL
           GROUP BY source_wallet
           ORDER BY pnl DESC"""
    )

    # Open position stats — grouped by source wallet
    open_by_wallet = await db.execute_fetchall(
        """SELECT source_wallet,
                  COUNT(*) as open_count,
                  COALESCE(SUM(cost_basis), 0) as cost_basis,
                  MIN(opened_at) as first_opened,
                  MAX(opened_at) as last_opened
           FROM positions
           WHERE strategy='copy_trade' AND status='open' AND source_wallet IS NOT NULL
           GROUP BY source_wallet
           ORDER BY cost_basis DESC"""
    )

    # Open position count and exposure totals
    open_totals = await db.execute_fetchall(
        """SELECT COUNT(*) as cnt,
                  COUNT(DISTINCT market_id) as unique_markets,
                  COALESCE(SUM(cost_basis), 0) as total_cost
           FROM positions WHERE status='open'"""
    )

    # Recent spike events
    spike_events = await db.execute_fetchall(
        """SELECT * FROM spike_events ORDER BY detected_at DESC LIMIT 20"""
    )

    # Orders summary: how many attempted, failed (geo-block etc.)
    order_summary = await db.execute_fetchall(
        """SELECT status, COUNT(*) as cnt FROM orders GROUP BY status"""
    )

    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "copy_stats": [dict(r) for r in copy_stats],
        "open_by_wallet": [dict(r) for r in open_by_wallet],
        "open_totals": dict(open_totals[0]) if open_totals else {"cnt": 0, "unique_markets": 0, "total_cost": 0.0},
        "recent_spikes": [dict(r) for r in spike_events],
        "order_summary": {row["status"]: row["cnt"] for row in order_summary},
        "regime_stats": await get_regime_stats(db),
    }
