"""
Flask dashboard server — runs in a background thread alongside the trading agent.
Reads directly from the SQLite database (WAL mode allows concurrent reads).
Serves on localhost:5000.
"""

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template

import config

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(config.SQLITE_DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = db.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _scalar(db: sqlite3.Connection, sql: str, params: tuple = (), default=0):
    cur = db.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else default


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/overview")
def overview():
    db = _db()
    try:
        # Latest balance snapshot
        snap = _rows(db,
            "SELECT balance, total_exposure, available FROM balance_snapshots "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        balance = snap[0]["balance"] if snap else 0.0
        exposure = snap[0]["total_exposure"] if snap else 0.0
        available = snap[0]["available"] if snap else 0.0

        # All-time P&L
        total_pnl = _scalar(db,
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='closed'"
        )

        # Win rate
        closed = _scalar(db, "SELECT COUNT(*) FROM positions WHERE status='closed'")
        wins = _scalar(db,
            "SELECT COUNT(*) FROM positions WHERE status='closed' AND pnl > 0"
        )
        win_rate = round(wins / closed * 100, 1) if closed > 0 else 0.0

        # Open positions
        open_count = _scalar(db, "SELECT COUNT(*) FROM positions WHERE status='open'")

        # Trades executed today (UTC)
        today = datetime.now(timezone.utc).date().isoformat()
        trades_today = _scalar(db,
            "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND status='filled'",
            (today,)
        )

        return jsonify({
            "balance": round(balance, 2),
            "exposure": round(exposure, 2),
            "available": round(available, 2),
            "total_pnl": round(float(total_pnl), 2),
            "win_rate": win_rate,
            "open_positions": open_count,
            "trades_today": trades_today,
            "paper_trading": config.PAPER_TRADING,
        })
    finally:
        db.close()


@app.route("/api/copy_trades")
def copy_trades():
    db = _db()
    try:
        # All copy trade positions (open + recently closed)
        positions = _rows(db,
            """SELECT p.id, p.market_question, p.outcome, p.side,
                      p.entry_price, p.exit_price, p.size, p.cost_basis,
                      p.pnl, p.status, p.source_wallet, p.opened_at, p.closed_at
               FROM positions p
               WHERE p.strategy = 'copy_trade'
               ORDER BY p.opened_at DESC
               LIMIT 100"""
        )

        # Enrich with current price approximation (use exit_price or entry_price)
        for p in positions:
            p["current_price"] = p["exit_price"] if p["exit_price"] else p["entry_price"]
            if p["status"] == "open":
                p["unrealized_pnl"] = round(
                    (p["current_price"] - p["entry_price"]) * p["size"]
                    if p["side"] == "BUY"
                    else (p["entry_price"] - p["current_price"]) * p["size"],
                    4,
                )
            else:
                p["unrealized_pnl"] = None

        # Monitored wallets
        wallets = _rows(db,
            """SELECT address, rank, total_profit, trade_count, win_rate, last_refreshed
               FROM tracked_wallets
               ORDER BY rank ASC
               LIMIT 20"""
        )

        # Last executed copy trade order
        last_order = _rows(db,
            """SELECT o.created_at, o.side, o.outcome, o.price, o.size, o.status,
                      p.market_question, p.source_wallet
               FROM orders o
               LEFT JOIN positions p ON p.id = o.position_id
               WHERE p.strategy = 'copy_trade'
               ORDER BY o.created_at DESC
               LIMIT 1"""
        )

        return jsonify({
            "positions": positions,
            "monitored_wallets": wallets,
            "last_order": last_order[0] if last_order else None,
        })
    finally:
        db.close()


@app.route("/api/volume_spikes")
def volume_spikes():
    db = _db()
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        spikes = _rows(db,
            """SELECT se.id, se.market_slug, se.outcome, se.detected_at,
                      se.spike_magnitude, se.rolling_avg, se.recent_volume,
                      se.price_wall, se.trend_aligned, se.trade_decision,
                      se.rationale, se.position_id,
                      se.outcome_correct, se.price_move,
                      p.status as position_status, p.pnl
               FROM spike_events se
               LEFT JOIN positions p ON p.id = se.position_id
               WHERE se.detected_at >= ?
               ORDER BY se.detected_at DESC""",
            (since,)
        )

        # Currently active (open position from a spike)
        active = _rows(db,
            """SELECT se.market_slug, se.outcome, se.spike_magnitude,
                      se.trade_decision, p.entry_price, p.size, p.opened_at
               FROM spike_events se
               JOIN positions p ON p.id = se.position_id
               WHERE p.status = 'open'
               ORDER BY p.opened_at DESC"""
        )

        return jsonify({
            "spikes_24h": spikes,
            "active_positions": active,
        })
    finally:
        db.close()


@app.route("/api/logs")
def logs():
    log_file = Path(config.LOG_DIR) / "agent.log"
    lines = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            lines = [l.rstrip() for l in all_lines[-50:]]
        except OSError:
            lines = ["[error reading log file]"]
    else:
        lines = ["[log file not yet created]"]
    return jsonify({"lines": lines})


@app.route("/api/strategy_doc")
def strategy_doc():
    doc_path = Path(config.STRATEGY_DOC_PATH)
    content = ""
    if doc_path.exists():
        try:
            content = doc_path.read_text(encoding="utf-8")
        except OSError:
            content = "_Error reading STRATEGY_DOC.md_"
    else:
        content = "_STRATEGY_DOC.md not yet generated._"
    return jsonify({"content": content})


# ── Startup helper ────────────────────────────────────────────────────────────

def start_dashboard(host: str = "0.0.0.0", port: int = 5000) -> threading.Thread:
    """Start Flask in a daemon thread. Returns the thread."""
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        name="dashboard",
        daemon=True,
    )
    thread.start()
    return thread
