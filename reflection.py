"""
reflection.py — Nightly self-improvement loop for polymarket-trading-agent

Reads resolved trade outcomes and updates adaptive parameters so future
decisions are informed by actual historical performance.

Run nightly via cron:
    0 2 * * * cd /path/to/polymarket-trading-agent && python reflection.py >> logs/reflection.log 2>&1

Or scheduled automatically by agent.py (see _nightly_reflection_loop).

Phase 1 (this file): Pure SQLite analysis, no external dependencies.
Phase 2 (future):    ChromaDB for semantic memory retrieval at decision time.
                     LangGraph for multi-step reflection with LLM insights.
                     Claude API for narrative lessons generation.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reflection")

LESSONS_FILE = "lessons.md"
MIN_SAMPLES_FOR_THRESHOLD = 15   # don't adjust threshold without enough data
MIN_WIN_RATE_FOR_SIGNAL = 0.55   # require 55%+ win rate to treat spike as actionable


# ── Analysis functions ───────────────────────────────────────────────────────

async def compute_adaptive_spike_threshold(db: "DB") -> tuple[float, str]:
    """
    Find the lowest spike ratio where historical win rate >= MIN_WIN_RATE_FOR_SIGNAL.
    Returns (threshold, reason_string).

    Why: a 3x spike might be a coin flip. A 7x spike might be 70% predictive.
    This finds the empirical cutoff from your own data.
    """
    rows = await db.execute_fetchall(
        """
        SELECT
            CAST(ROUND(spike_magnitude, 0) AS INT) AS bin,
            COUNT(*)                                AS n,
            ROUND(AVG(CAST(outcome_correct AS FLOAT)), 3) AS win_rate
        FROM spike_events
        WHERE resolved_at IS NOT NULL
          AND outcome_correct IS NOT NULL
        GROUP BY bin
        HAVING n >= ?
        ORDER BY bin ASC
        """,
        (MIN_SAMPLES_FOR_THRESHOLD,),
    )

    for row in rows:
        if row["win_rate"] >= MIN_WIN_RATE_FOR_SIGNAL:
            reason = (
                f"Lowest spike ratio with win_rate>={MIN_WIN_RATE_FOR_SIGNAL} "
                f"based on {row['n']} samples"
            )
            return float(row["bin"]), reason

    return 3.0, "Insufficient data for adaptive threshold, using default"


async def analyze_copy_trade_performance(db: "DB") -> dict:
    """Segment copy trade performance by source wallet (last 30 days)."""
    rows = await db.execute_fetchall(
        """
        SELECT
            source_wallet,
            COUNT(*)                                             AS total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)            AS wins,
            COALESCE(SUM(pnl), 0)                                AS total_pnl,
            ROUND(AVG(pnl), 2)                                   AS avg_pnl
        FROM positions
        WHERE strategy = 'copy_trade'
          AND status = 'closed'
          AND closed_at >= datetime('now', '-30 days')
        GROUP BY source_wallet
        ORDER BY total_pnl DESC
        """,
    )
    return {r["source_wallet"]: dict(r) for r in rows}


async def analyze_spike_performance(db: "DB") -> list:
    """Bin spike performance by magnitude."""
    return await db.execute_fetchall(
        """
        SELECT
            CAST(ROUND(spike_magnitude, 0) AS INT)          AS spike_bin,
            COUNT(*)                                         AS total,
            COALESCE(SUM(outcome_correct), 0)                AS correct,
            ROUND(AVG(CAST(outcome_correct AS FLOAT))*100,1) AS win_rate_pct,
            ROUND(AVG(price_move)*100, 2)                    AS avg_price_move_pct
        FROM spike_events
        WHERE resolved_at IS NOT NULL
          AND outcome_correct IS NOT NULL
        GROUP BY spike_bin
        ORDER BY spike_bin
        """,
    )


# ── Lessons document ─────────────────────────────────────────────────────────

def generate_lessons_markdown(
    spike_stats: list,
    wallet_stats: dict,
    new_threshold: float,
    old_threshold: float,
) -> str:
    """Generate the lessons.md file content."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Trading Agent Lessons",
        f"*Last updated: {now}*",
        "",
        "## Volume Spike Performance by Magnitude",
        "",
        "| Spike Ratio | Trades | Win Rate | Avg Price Move |",
        "|-------------|--------|----------|----------------|",
    ]

    for row in spike_stats:
        lines.append(
            f"| {row['spike_bin']}x | {row['total']} "
            f"| {row['win_rate_pct']}% | {row['avg_price_move_pct']}% |"
        )

    lines += [
        "",
        f"**Adaptive threshold**: {old_threshold}x → **{new_threshold}x**",
        "",
        "## Copy Trade Wallet Performance (Last 30 Days)",
        "",
    ]

    if wallet_stats:
        lines += [
            "| Wallet | Trades | Win Rate | PnL |",
            "|--------|--------|----------|-----|",
        ]
        for addr, s in sorted(
            wallet_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True
        )[:10]:
            win_rate = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
            lines.append(
                f"| `{addr[:10]}...` | {s['total']} "
                f"| {win_rate:.0f}% | ${s['total_pnl']:.0f} |"
            )
    else:
        lines.append("*No closed copy trades in last 30 days.*")

    lines += [
        "",
        "---",
        "",
        "## Future Enhancements (Not Yet Implemented)",
        "",
        "- **Phase 2 — ChromaDB semantic memory**: At decision time, retrieve the 5 most",
        "  similar past markets and what happened. Helps avoid repeating mistakes on",
        "  similar market types. Install: `pip install chromadb`.",
        "",
        "- **Phase 2 — LangGraph orchestration**: Replace the single-agent loop with a",
        "  LangGraph graph: Market Scanner → Bull Researcher → Bear Researcher →",
        "  Trader → Risk Manager. The debate between Bull/Bear reduces overconfidence.",
        "",
        "- **Phase 3 — Claude API reflection**: Replace `generate_lessons_markdown()`",
        "  with a Claude API call that reads the raw data and writes narrative insights.",
    ]

    return "\n".join(lines)


def generate_market_memory_context(market_question: str) -> str:
    """
    ⭐ PHASE 2 STUB — ChromaDB semantic memory retrieval.

    In Phase 2, replace this with:

        import chromadb
        client = chromadb.PersistentClient(path="data/chroma")
        collection = client.get_or_create_collection("trade_memory")

        results = collection.query(
            query_texts=[market_question],
            n_results=5
        )
        return "\\n".join(results["documents"][0])

    And after each trade resolves, store it:
        collection.add(
            documents=[f"{market_question} | outcome: {outcome} | pnl: {pnl}"],
            ids=[trade_id]
        )

    This gives the agent context like:
        "In 5 similar past markets, 3 resolved YES, 2 NO. Key difference was..."
    """
    return ""  # Phase 1: no memory retrieval yet


# ── Internal DB wrapper ──────────────────────────────────────────────────────

class DB:
    """Thin async wrapper around an aiosqlite connection for reflection queries."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute_fetchall(self, query: str, params: tuple = ()) -> list:
        async with self._conn.execute(query, params) as cursor:
            return await cursor.fetchall()

    async def execute_fetchone(self, query: str, params: tuple = ()) -> any:
        async with self._conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def set_adaptive_param(
        self, key: str, value: float, reason: str = ""
    ) -> None:
        await self._conn.execute(
            """INSERT INTO adaptive_params (key, value, reason, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                   value      = excluded.value,
                   reason     = excluded.reason,
                   updated_at = excluded.updated_at""",
            (key, value, reason),
        )
        await self._conn.commit()

    async def get_adaptive_param(
        self, key: str, default: float | None = None
    ) -> float | None:
        row = await self.execute_fetchone(
            "SELECT value FROM adaptive_params WHERE key = ?", (key,)
        )
        return float(row["value"]) if row else default


# ── Entry point ──────────────────────────────────────────────────────────────

async def run_reflection() -> dict:
    logger.info("Starting nightly reflection...")

    async with aiosqlite.connect(config.SQLITE_DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        db = DB(conn)

        old_threshold = await db.get_adaptive_param("spike_threshold", default=3.0)

        new_threshold, reason = await compute_adaptive_spike_threshold(db)
        spike_stats = await analyze_spike_performance(db)
        wallet_stats = await analyze_copy_trade_performance(db)

        await db.set_adaptive_param("spike_threshold", new_threshold, reason)
        logger.info(
            "Spike threshold: %.1fx → %.1fx (%s)", old_threshold, new_threshold, reason
        )

        lessons = generate_lessons_markdown(
            spike_stats, wallet_stats, new_threshold, old_threshold or 3.0
        )
        Path(LESSONS_FILE).write_text(lessons)
        logger.info("Written %s", LESSONS_FILE)

        # Commit lessons to git if running as a standalone cron job.
        import subprocess
        subprocess.run(["git", "add", "lessons.md"], check=False)
        subprocess.run(
            ["git", "commit", "-m", f"nightly reflection: spike_threshold={new_threshold}"],
            check=False,
        )

        logger.info("Reflection complete.")
        return {"new_threshold": new_threshold, "reason": reason}


if __name__ == "__main__":
    asyncio.run(run_reflection())
