"""
Strategy 2: Order-Book Volume Spike Detection
Detects abnormal trading activity and trades momentum/fade based on conviction signals.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import aiosqlite

import config
import database
from execution import OrderExecutor
from wallet import WalletManager

logger = logging.getLogger("strategy.volume_spike")


class VolumeSpikeStrategy:
    """
    Monitors order book volume across all active Polymarket markets.
    Detects spikes (>2x rolling average) and analyzes conviction signals.

    Flow:
    1. Track volume per market/outcome in 12-hour buckets.
    2. Every 12 hours, compare recent volume to rolling average.
    3. On spike: analyze price concentration and trend alignment.
    4. Enter, fade, or skip based on signal quality.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        wallet: WalletManager,
        executor: OrderExecutor,
    ) -> None:
        self._db = db
        self._wallet = wallet
        self._executor = executor
        self._running = False

        # In-memory volume accumulator between DB flushes
        self._volume_accumulator: dict[str, dict[str, float]] = {}
        # market_id -> {YES: vol, NO: vol}
        self._trade_count_accumulator: dict[str, dict[str, int]] = {}

    async def start(self) -> None:
        """Start the volume spike strategy."""
        self._running = True
        logger.info("Volume spike strategy starting...")

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False
        logger.info("Volume spike strategy stopped")

    async def run(self) -> None:
        """
        Main strategy loop.
        - Collects volume data continuously via polling.
        - Runs spike detection every 12 hours.
        """
        last_spike_check = datetime.min

        while self._running:
            try:
                # Collect volume data
                await self._collect_volume_data()

                # Flush accumulated volume to DB every cycle
                await self._flush_volume_to_db()

                # Run spike detection every 12 hours
                now = datetime.utcnow()
                if (now - last_spike_check).total_seconds() >= config.VOLUME_SPIKE_CHECK_INTERVAL_SEC:
                    await self._run_spike_detection()
                    last_spike_check = now

                # Poll interval: check volume every 5 minutes
                await asyncio.sleep(300)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Volume spike loop error: %s", e)
                await asyncio.sleep(60)

    # ── Volume Collection ───────────────────────────────────────────────────

    async def _collect_volume_data(self) -> None:
        """Fetch current volume data for active markets."""
        markets = await self._executor.get_active_markets(limit=50)

        for market in markets:
            market_id = market.get("id", market.get("condition_id", ""))
            if not market_id:
                continue

            volume = float(market.get("volume", market.get("volume_num", 0)))

            # Split volume estimate between YES and NO (rough 50/50 if no breakdown)
            for outcome in ["YES", "NO"]:
                key = f"{market_id}:{outcome}"
                if market_id not in self._volume_accumulator:
                    self._volume_accumulator[market_id] = {"YES": 0, "NO": 0}
                    self._trade_count_accumulator[market_id] = {"YES": 0, "NO": 0}

                # Accumulate volume (divide by 2 for YES/NO split estimate)
                self._volume_accumulator[market_id][outcome] += volume / 2
                self._trade_count_accumulator[market_id][outcome] += 1

    async def _flush_volume_to_db(self) -> None:
        """Flush accumulated volume into 12-hour buckets in the database."""
        now = datetime.utcnow()
        # Calculate current bucket boundaries
        bucket_hour = (now.hour // config.VOLUME_SPIKE_BUCKET_HOURS) * config.VOLUME_SPIKE_BUCKET_HOURS
        bucket_start = now.replace(
            hour=bucket_hour, minute=0, second=0, microsecond=0
        )
        bucket_end = bucket_start + timedelta(hours=config.VOLUME_SPIKE_BUCKET_HOURS)

        for market_id, volumes in self._volume_accumulator.items():
            for outcome in ["YES", "NO"]:
                vol = volumes.get(outcome, 0)
                count = self._trade_count_accumulator.get(market_id, {}).get(outcome, 0)
                if vol > 0:
                    await database.insert_volume_bucket(
                        self._db,
                        market_id=market_id,
                        outcome=outcome,
                        bucket_start=bucket_start.isoformat(),
                        bucket_end=bucket_end.isoformat(),
                        volume=vol,
                        trade_count=count,
                    )

        # Reset accumulators
        self._volume_accumulator = {}
        self._trade_count_accumulator = {}

    # ── Spike Detection ─────────────────────────────────────────────────────

    async def _run_spike_detection(self) -> None:
        """
        Compare most recent 12-hour bucket volume against 24-hour rolling average.
        Flag any market/outcome where recent > 2x average.
        """
        logger.info("Running spike detection scan...")

        now = datetime.utcnow()
        bucket_hours = config.VOLUME_SPIKE_BUCKET_HOURS

        # Time boundaries
        recent_start = (
            now - timedelta(hours=bucket_hours)
        ).isoformat()
        rolling_start = (
            now - timedelta(hours=bucket_hours * 2)
        ).isoformat()  # 24 hours back

        # Get all markets with volume data
        rows = await self._db.execute_fetchall(
            """SELECT DISTINCT market_id, outcome FROM volume_buckets
               WHERE bucket_start >= ?""",
            (rolling_start,),
        )

        spikes_found = 0
        for row in rows:
            market_id = row["market_id"]
            outcome = row["outcome"]

            # Get recent volume (last 12h)
            recent_rows = await self._db.execute_fetchall(
                """SELECT COALESCE(SUM(volume), 0) as vol
                   FROM volume_buckets
                   WHERE market_id=? AND outcome=? AND bucket_start >= ?""",
                (market_id, outcome, recent_start),
            )
            recent_vol = float(recent_rows[0]["vol"])

            # Get rolling average (last 24h = 2 buckets)
            _, rolling_avg = await database.get_rolling_volume(
                self._db, market_id, outcome, rolling_start,
            )

            if rolling_avg <= 0:
                continue

            # Calculate spike magnitude
            magnitude = recent_vol / rolling_avg

            if magnitude >= config.VOLUME_SPIKE_THRESHOLD_MULTIPLIER:
                spikes_found += 1
                logger.info(
                    "SPIKE DETECTED: %s %s — %.1fx volume (recent: %.0f, avg: %.0f)",
                    market_id[:12], outcome, magnitude, recent_vol, rolling_avg,
                )
                await self._analyze_spike(
                    market_id, outcome, magnitude, rolling_avg, recent_vol,
                )

        logger.info("Spike detection complete: %d spikes found", spikes_found)

    # ── Signal Analysis ─────────────────────────────────────────────────────

    async def _analyze_spike(
        self,
        market_id: str,
        outcome: str,
        magnitude: float,
        rolling_avg: float,
        recent_volume: float,
    ) -> None:
        """
        Analyze a detected spike for conviction signals.
        Checks price concentration (walls) and trend alignment.
        """
        # Get market info
        market = await self._executor.get_market_info(market_id)
        market_slug = market.get("slug", "") if market else ""

        # Get token ID for this outcome
        token_id = self._get_token_id(market, outcome)

        # 1. Check price concentration (limit walls)
        order_book = await self._executor.get_order_book(token_id)
        price_wall = self._detect_price_wall(order_book, outcome)

        # 2. Check trend alignment
        trend_aligned = await self._check_trend_alignment(
            market_id, outcome, order_book
        )

        # 3. Make trade decision
        trade_decision, rationale = self._decide_trade(
            magnitude, price_wall, trend_aligned
        )

        logger.info(
            "Spike analysis for %s %s: wall=%s, trend=%s → %s (%s)",
            market_id[:12], outcome,
            "YES" if price_wall else "NO",
            "ALIGNED" if trend_aligned else "NOT ALIGNED",
            trade_decision, rationale,
        )

        # Record spike event
        position_id = None

        if trade_decision in ("enter", "fade"):
            position_id = await self._execute_spike_trade(
                market_id, market_slug, outcome, order_book,
                token_id, magnitude, trade_decision, market,
            )

        await database.insert_spike_event(
            self._db,
            market_id=market_id,
            market_slug=market_slug,
            outcome=outcome,
            spike_magnitude=magnitude,
            rolling_avg=rolling_avg,
            recent_volume=recent_volume,
            price_wall=price_wall,
            trend_aligned=trend_aligned,
            trade_decision=trade_decision,
            rationale=rationale,
            position_id=position_id,
        )

    def _detect_price_wall(
        self, order_book: dict[str, Any], outcome: str
    ) -> bool:
        """
        Check if spiked volume clusters at 1-2 price levels (limit wall).
        Returns True if >50% of volume is concentrated at top 2 levels.
        """
        # For BUY side (outcome trending up), check bid side
        # For a YES spike, look at the asks (people buying YES)
        levels = order_book.get("asks" if outcome == "YES" else "bids", [])

        if len(levels) < 2:
            return False

        total_size = sum(l["size"] for l in levels)
        if total_size == 0:
            return False

        # Top 2 levels by size
        sorted_levels = sorted(levels, key=lambda x: x["size"], reverse=True)
        top2_size = sum(l["size"] for l in sorted_levels[:2])

        concentration = top2_size / total_size
        return concentration >= config.VOLUME_SPIKE_PRICE_WALL_PCT

    async def _check_trend_alignment(
        self,
        market_id: str,
        outcome: str,
        current_book: dict[str, Any],
    ) -> bool:
        """
        Check if the spike aligns with the 24-hour price trend.
        A YES spike is aligned if mid-price has been trending up.
        A NO spike is aligned if mid-price has been trending down.
        """
        # Get historical price from volume bucket data (rough proxy)
        # In production, this would use price history API
        now = datetime.utcnow()
        day_ago = (now - timedelta(hours=24)).isoformat()

        rows = await self._db.execute_fetchall(
            """SELECT bucket_start, volume FROM volume_buckets
               WHERE market_id=? AND outcome=? AND bucket_start >= ?
               ORDER BY bucket_start ASC""",
            (market_id, outcome, day_ago),
        )

        if len(rows) < 2:
            return False  # Not enough data to determine trend

        # Use volume trend as proxy for price trend
        # Increasing volume in an outcome suggests price moving toward it
        first_half_vol = sum(float(r["volume"]) for r in rows[:len(rows)//2])
        second_half_vol = sum(float(r["volume"]) for r in rows[len(rows)//2:])

        if outcome == "YES":
            return second_half_vol > first_half_vol  # Trending up
        else:
            return second_half_vol > first_half_vol  # Volume increasing for NO

    def _decide_trade(
        self,
        magnitude: float,
        price_wall: bool,
        trend_aligned: bool,
    ) -> tuple[str, str]:
        """
        Decide whether to enter, fade, or skip based on signal analysis.

        - Conviction (enter): price wall + trend aligned
        - Fade: spread volume (no wall) → trade against spike
        - Skip: wall but no trend alignment
        """
        if price_wall and trend_aligned:
            return "enter", (
                f"Strong conviction: price wall detected with {magnitude:.1f}x "
                f"volume spike aligned with trend"
            )
        elif not price_wall and config.VOLUME_SPIKE_FADE_ENABLED:
            return "fade", (
                f"Retail noise: {magnitude:.1f}x spike with spread volume "
                f"(no wall) — fading the move"
            )
        elif price_wall and not trend_aligned:
            return "skip", (
                f"Price wall detected ({magnitude:.1f}x spike) but "
                f"not aligned with trend — watching"
            )
        else:
            return "skip", (
                f"No clear signal: {magnitude:.1f}x spike, "
                f"wall={price_wall}, trend={trend_aligned}"
            )

    # ── Trade Execution ─────────────────────────────────────────────────────

    async def _execute_spike_trade(
        self,
        market_id: str,
        market_slug: str,
        outcome: str,
        order_book: dict[str, Any],
        token_id: str,
        magnitude: float,
        decision: str,
        market: dict[str, Any] | None,
    ) -> int | None:
        """Execute a trade based on spike analysis."""
        # Determine side and outcome based on decision
        if decision == "enter":
            side = "BUY"
            trade_outcome = outcome
        elif decision == "fade":
            side = "BUY"
            trade_outcome = "NO" if outcome == "YES" else "YES"
            # Get the opposite outcome's order book
            opposite_token = self._get_token_id(market, trade_outcome)
            order_book = await self._executor.get_order_book(opposite_token)
            token_id = opposite_token
        else:
            return None

        # Calculate position size scaled to spike magnitude
        # Base: 5% of wallet, scaled up to 15% max based on magnitude
        base_pct = 0.05
        scale_factor = min(magnitude / config.VOLUME_SPIKE_THRESHOLD_MULTIPLIER, 3.0)
        target_pct = min(
            base_pct * scale_factor,
            config.VOLUME_SPIKE_MAX_POSITION_PCT,
        )

        price = self._executor.calculate_limit_price(order_book, side)
        if price <= 0:
            logger.warning("Skip spike trade: invalid price for %s", market_id[:12])
            return None

        size_usd = self._wallet.calculate_position_size(target_pct, "volume_spike")
        size_tokens = size_usd / price
        cost = price * size_tokens

        # Risk check
        allowed, reason = self._wallet.can_open_position(cost, "volume_spike")
        if not allowed:
            logger.info("Skip spike trade: %s", reason)
            return None

        # Check existing position
        if self._wallet.get_position_for_market(market_id):
            logger.info("Skip spike trade: already have position in %s", market_id[:12])
            return None

        # Record and execute
        question = market.get("question", "") if market else ""
        position_id = await database.insert_position(
            self._db,
            market_id=market_id,
            market_slug=market_slug,
            market_question=question,
            outcome=trade_outcome,
            side=side,
            entry_price=price,
            size=size_tokens,
            strategy="volume_spike",
            notes=f"Spike {decision}: {magnitude:.1f}x volume, scaled {target_pct*100:.1f}%",
        )

        result = await self._executor.place_order(
            token_id=token_id,
            market_id=market_id,
            side=side,
            price=price,
            size=size_tokens,
            position_id=position_id,
        )

        if result["status"] != "filled":
            await self._db.execute(
                "UPDATE positions SET status='cancelled' WHERE id=?",
                (position_id,),
            )
            await self._db.commit()
            logger.warning("Spike trade order failed for %s", market_id[:12])
            return None

        await self._wallet.refresh()
        return position_id

    def _get_token_id(
        self, market: dict[str, Any] | None, outcome: str
    ) -> str:
        """Extract the token ID for a given outcome from market data."""
        if not market:
            return "unknown-token"

        tokens = market.get("tokens", [])
        outcome_lower = outcome.lower()
        for token in tokens:
            token_outcome = token.get("outcome", "").lower()
            if token_outcome == outcome_lower:
                return token.get("token_id", "")

        if tokens:
            return tokens[0].get("token_id", "")
        return f"{market.get('id', 'unknown')}-{outcome.lower()}"
