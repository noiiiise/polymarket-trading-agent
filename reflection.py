"""
Nightly self-improvement and reflection module.

Analyzes recent trade outcomes to detect regime patterns, attribute losses,
and update adaptive parameters that guard entry decisions in strategies.

Regime detection for Polymarket (no VIX/ADX — adapted for prediction markets):
  trending  = high spike activity, whale presence, recent win rate ≥ 50%
  neutral   = moderate activity, insufficient signal
  choppy    = few spikes, low activity — momentum signals unreliable
  risk_off  = many spikes but poor outcomes — irrational / noisy market

⭐ LANGGRAPH NOTE: In Phase 2, the nightly reflection loop and regime detector
   become LangGraph nodes in a self-improvement graph:
     DataCollector → RegimeDetector → LossAttributor → ParamUpdater → DocWriter
   Each node passes state forward; the graph replaces this sequential module.
   For now, this runs as a plain async loop called from agent.py every 24h.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiosqlite

import database

logger = logging.getLogger("reflection")


# ── Regime Detection ──────────────────────────────────────────────────────────

def detect_market_regime(
    recent_spike_count: int,
    entered_count: int,
    recent_win_rate: float,
    avg_spike_magnitude: float,
) -> str:
    """
    Detect current market regime from Polymarket activity metrics.

    Unlike equity markets, Polymarket has no VIX or ADX. Instead:
    - "trending"  = active spikes + whales + decent win rate → momentum works
    - "choppy"    = few spikes → signals unreliable, skip entries
    - "risk_off"  = many spikes but poor outcomes → noisy / irrational market
    - "neutral"   = everything else / insufficient data

    Thresholds:
      ADX > 25 analogue  → spike_count ≥ 5 AND avg_magnitude ≥ 5×
      ADX < 20 analogue  → spike_count < 3 (choppy)
      VIX > 25 analogue  → spike_count ≥ 10 AND win_rate < 35% (risk_off)

    ⭐ LANGGRAPH NOTE: In Phase 2, this becomes the first node in a LangGraph
       decision graph: RegimeDetector → SignalScorer → RiskManager → Executor
       LangGraph handles state passing and conditional branching between nodes.
       For now, this is a standalone function called before each trade entry.
    """
    if recent_spike_count == 0:
        return "choppy"

    if recent_spike_count >= 5 and avg_spike_magnitude >= 5.0 and recent_win_rate >= 0.50:
        return "trending"

    if recent_spike_count >= 10 and recent_win_rate < 0.35:
        return "risk_off"

    if recent_spike_count < 3:
        return "choppy"

    return "neutral"


async def detect_market_regime_from_db(db: aiosqlite.Connection) -> str:
    """
    Query the database for recent spike activity and compute the current regime.
    Looks at spike_events in the last 6 hours and closed positions over 30 days.
    """
    try:
        spike_rows = await db.execute_fetchall("""
            SELECT
                COUNT(*) AS total_spikes,
                SUM(CASE WHEN trade_decision = 'enter' THEN 1 ELSE 0 END) AS entered,
                COALESCE(AVG(spike_magnitude), 0) AS avg_magnitude
            FROM spike_events
            WHERE detected_at >= datetime('now', '-6 hours')
        """)
        row = dict(spike_rows[0]) if spike_rows else {}
        total_spikes = int(row.get("total_spikes") or 0)
        entered = int(row.get("entered") or 0)
        avg_mag = float(row.get("avg_magnitude") or 0)

        wr_rows = await db.execute_fetchall("""
            SELECT COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0.5) AS win_rate
            FROM positions
            WHERE status = 'closed'
              AND closed_at >= datetime('now', '-30 days')
        """)
        recent_win_rate = float(wr_rows[0]["win_rate"] if wr_rows else 0.5)

        return detect_market_regime(total_spikes, entered, recent_win_rate, avg_mag)
    except Exception as e:
        logger.debug("Regime detection query failed: %s", e)
        return "unknown"


# ── Loss Attribution ──────────────────────────────────────────────────────────

async def attribute_losses(db: aiosqlite.Connection) -> dict[str, int]:
    """
    Classify loss reasons for positions closed in the last 30 days.

    Categories for a Polymarket momentum bot:
    - wrong_direction:              spike/whale signal fired but market resolved against
    - right_direction_wrong_timing: had positive MFE but position reversed before resolution
    - right_idea_wrong_regime:      entered during choppy / risk_off conditions
    - slippage:                     spread cost ate the edge (small loss ≤ 5% of cost basis)
    - size_too_large:               correct direction, but loss > 20% of cost basis

    max_favorable_excursion and max_adverse_excursion are populated at close time
    (see close_position in database.py). For positions without excursion data the
    logic falls back to regime and loss-size heuristics.
    """
    losses = await db.execute_fetchall("""
        SELECT market_regime, cost_basis, pnl,
               max_favorable_excursion, max_adverse_excursion,
               entry_price, strategy
        FROM positions
        WHERE pnl < 0 AND status = 'closed'
          AND closed_at >= datetime('now', '-30 days')
    """)

    attribution: dict[str, int] = {
        "wrong_direction": 0,
        "right_direction_wrong_timing": 0,
        "right_idea_wrong_regime": 0,
        "slippage": 0,
        "size_too_large": 0,
    }

    for _row in losses:
        trade = dict(_row)
        regime = trade.get("market_regime") or "unknown"
        cost = float(trade.get("cost_basis") or 1.0)
        pnl = float(trade.get("pnl") or 0.0)
        mfe = float(trade.get("max_favorable_excursion") or 0.0)
        mae = float(trade.get("max_adverse_excursion") or 0.0)
        loss_pct = abs(pnl) / max(cost, 0.01)

        if regime in ("choppy", "risk_off"):
            attribution["right_idea_wrong_regime"] += 1
        elif loss_pct <= 0.05:
            attribution["slippage"] += 1
        elif loss_pct > 0.20:
            attribution["size_too_large"] += 1
        elif mfe > 0 and mfe > mae * 0.5:
            attribution["right_direction_wrong_timing"] += 1
        else:
            attribution["wrong_direction"] += 1

    return attribution


# ── Nightly Reflection Job ────────────────────────────────────────────────────

async def run_nightly_reflection(db: aiosqlite.Connection) -> None:
    """
    Full nightly self-improvement job. Should be called once per 24h.

    1. Compute per-regime win rates  → stored in adaptive_params["regime_win_rates"]
    2. Attribute recent losses       → stored in adaptive_params["loss_attribution"]
    3. Backfill signal_performance   → joins spike_events + closed positions
    4. Log human-readable summary
    """
    logger.info("=== Nightly Reflection Starting ===")

    # ── 1. Regime win rates ────────────────────────────────────────────────
    regime_rows = await db.execute_fetchall("""
        SELECT market_regime,
               COUNT(*) AS trades,
               ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 4) AS win_rate,
               ROUND(SUM(pnl), 2) AS total_pnl
        FROM positions
        WHERE status = 'closed'
          AND market_regime IS NOT NULL
          AND market_regime != 'unknown'
        GROUP BY market_regime
        ORDER BY total_pnl DESC
    """)

    if regime_rows:
        regime_win_rates = {
            row["market_regime"]: float(row["win_rate"])
            for row in regime_rows
        }
        await database.set_adaptive_param_json(db, "regime_win_rates", regime_win_rates)

        logger.info("Regime performance summary:")
        for row in regime_rows:
            logger.info(
                "  %-12s  trades=%d  win_rate=%.0f%%  pnl=$%.2f",
                row["market_regime"], int(row["trades"]),
                float(row["win_rate"]) * 100, float(row["total_pnl"]),
            )
    else:
        logger.info("No tagged trades yet — regime win rates not computed.")

    # ── 2. Loss attribution ────────────────────────────────────────────────
    attribution = await attribute_losses(db)
    total_losses = sum(attribution.values())

    if total_losses > 0:
        logger.info("Loss attribution (%d losses in last 30 days):", total_losses)
        for reason, count in sorted(attribution.items(), key=lambda x: -x[1]):
            pct = count / total_losses * 100
            logger.info("  %-34s %d  (%.0f%%)", reason, count, pct)
        await database.set_adaptive_param_json(db, "loss_attribution", attribution)
    else:
        logger.info("No recent losses to attribute.")

    # ── 3. Backfill signal_performance from closed spike-linked positions ──
    unrecorded = await db.execute_fetchall("""
        SELECT p.id          AS position_id,
               p.market_regime,
               p.pnl,
               se.spike_magnitude,
               se.price_wall,
               se.trend_aligned
        FROM positions p
        JOIN spike_events se ON se.position_id = p.id
        WHERE p.status = 'closed'
          AND p.pnl IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM signal_performance sp
              WHERE sp.position_id = p.id
                AND sp.signal_name = 'spike_ratio'
          )
        LIMIT 200
    """)

    inserted = 0
    for row in unrecorded:
        pos_id = int(row["position_id"])
        regime = row["market_regime"] or "unknown"
        pnl = float(row["pnl"] or 0)
        profitable = pnl > 0
        mag = float(row["spike_magnitude"] or 0)
        price_wall = bool(row["price_wall"])
        whale_backed = bool(row["trend_aligned"])

        # spike_ratio signal — bucket by magnitude
        if mag < 5:
            bucket = "low"
        elif mag < 10:
            bucket = "medium"
        else:
            bucket = "high"
        await database.record_signal_performance(
            db, "spike_ratio", mag, bucket, pos_id, regime, profitable, pnl
        )

        # price_wall signal
        await database.record_signal_performance(
            db, "price_wall", float(price_wall),
            "has_wall" if price_wall else "no_wall",
            pos_id, regime, profitable, pnl
        )

        # whale_backed signal
        await database.record_signal_performance(
            db, "whale_backed", float(whale_backed),
            "has_whales" if whale_backed else "no_whales",
            pos_id, regime, profitable, pnl
        )

        inserted += 1

    if inserted:
        logger.info("Backfilled signal_performance for %d closed positions.", inserted)

    # ── 4. Signal attribution summary (min 3 trades) ──────────────────────
    sig_rows = await db.execute_fetchall("""
        SELECT signal_name, signal_bucket, market_regime,
               COUNT(*) AS trades,
               ROUND(AVG(was_profitable) * 100, 1) AS win_rate,
               ROUND(SUM(pnl), 2) AS total_pnl
        FROM signal_performance
        GROUP BY signal_name, signal_bucket, market_regime
        HAVING trades >= 3
        ORDER BY signal_name, win_rate DESC
    """)

    if sig_rows:
        logger.info("Signal attribution (min 3 trades):")
        for row in sig_rows:
            logger.info(
                "  %-20s  %-12s  %-10s  win=%.0f%%  pnl=$%.2f  n=%d",
                row["signal_name"], row["signal_bucket"], row["market_regime"],
                float(row["win_rate"]), float(row["total_pnl"]), int(row["trades"]),
            )

    logger.info("=== Nightly Reflection Complete ===")


async def reflection_loop(
    db: aiosqlite.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Run nightly reflection every 24 hours. Starts immediately on first run."""
    while not shutdown_event.is_set():
        try:
            await run_nightly_reflection(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Reflection job failed: %s", e, exc_info=True)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=86400)
            break
        except asyncio.TimeoutError:
            continue
