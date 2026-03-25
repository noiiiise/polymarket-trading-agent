"""
Flask dashboard server — runs in a background thread alongside the trading agent.
Reads directly from the SQLite database (WAL mode allows concurrent reads).
Serves on localhost:5000.
"""

import base64
import json
import os
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

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


@app.route("/api/positions")
def open_positions():
    db = _db()
    try:
        positions = _rows(db,
            """SELECT id, market_question, market_slug, outcome, side,
                      entry_price, size, cost_basis, strategy,
                      source_wallet, opened_at, status
               FROM positions
               WHERE status = 'open'
               ORDER BY opened_at DESC"""
        )
        return jsonify({"positions": positions})
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


@app.route("/api/observations")
def get_observations():
    db = _db()
    try:
        _ensure_observations_table(db)
        obs = _rows(db,
            "SELECT id, created_at, source, market_tag, text, acted_on "
            "FROM observations ORDER BY created_at DESC"
        )
        return jsonify({"observations": obs})
    finally:
        db.close()


@app.route("/api/observations", methods=["POST"])
def add_observation():
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    source = (data.get("source") or "X/Twitter").strip()
    market_tag = (data.get("market_tag") or "").strip()

    if not text:
        return jsonify({"error": "text is required"}), 400

    now = datetime.now(timezone.utc).isoformat()

    db = _db()
    try:
        _ensure_observations_table(db)
        db.execute(
            "INSERT INTO observations (created_at, source, market_tag, text) VALUES (?, ?, ?, ?)",
            (now, source, market_tag, text),
        )
        db.commit()
    finally:
        db.close()

    _inject_observation_into_doc(text, source, market_tag, now)
    return jsonify({"ok": True, "created_at": now})


# ── Observation helpers ───────────────────────────────────────────────────────

def _ensure_observations_table(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'X/Twitter',
            market_tag  TEXT NOT NULL DEFAULT '',
            text        TEXT NOT NULL,
            acted_on    INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.commit()


def _inject_observation_into_doc(
    text: str, source: str, market_tag: str, timestamp: str
) -> None:
    """Prepend a new user observation into STRATEGY_DOC.md and push to GitHub."""
    doc_path = Path(config.STRATEGY_DOC_PATH)
    ts = timestamp[:16].replace("T", " ") + " UTC"
    tag_line = f" · #{market_tag}" if market_tag else ""

    entry = f"\n**{ts}** · {source}{tag_line}\n\n> {text}\n"

    try:
        content = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""

        MARKER = "## Observations & Adaptations"
        if MARKER in content:
            idx = content.index(MARKER) + len(MARKER)
            content = content[:idx] + entry + content[idx:]
        else:
            content += f"\n{MARKER}\n{entry}"

        doc_path.write_text(content, encoding="utf-8")
        _push_doc_to_github(content)
    except Exception as e:
        app.logger.error("Failed to inject observation into STRATEGY_DOC.md: %s", e)


def _push_doc_to_github(content: str) -> None:
    """Synchronously push STRATEGY_DOC.md to GitHub via the Contents API."""
    token = config.GITHUB_TOKEN
    owner = config.GITHUB_OWNER
    if not token or not owner:
        return

    repo = config.GITHUB_REPO
    branch = config.GITHUB_BRANCH
    file_path = config.STRATEGY_DOC_PATH

    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    )
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "polymarket-trading-agent",
        "Content-Type": "application/json",
    }

    current_sha: str | None = None
    try:
        req = urllib.request.Request(
            f"{api_url}?ref={branch}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            current_sha = json.loads(resp.read()).get("sha")
    except Exception:
        pass

    payload: dict = {
        "message": "[dashboard] Add field observation",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if current_sha:
        payload["sha"] = current_sha

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            api_url, data=data, headers=headers, method="PUT"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        app.logger.warning("GitHub push for observation failed: %s", e)


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
