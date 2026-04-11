"""
Strategy 2: Volume Spike & Whale Detection

Detects abnormal trading activity across ALL active Polymarket markets by
comparing 24-hour volume against average daily volume (from the Gamma API
directly — no historical DB accumulation needed).

Also monitors for whale-sized positions appearing on spiking markets.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
import aiosqlite

import config
import database
from execution import OrderExecutor
from wallet import WalletManager

logger = logging.getLogger("strategy.volume_spike")

SCAN_INTERVAL_SEC = 300  # 5 minutes between full scans
MARKET_SCAN_LIMIT = 500  # Scan top 500 active markets by volume
MIN_DAILY_AVG_USD = 500  # Ignore markets with < $500/day average volume
MIN_MARKET_AGE_DAYS = 3  # Need at least 3 days of history for a meaningful average
SPIKE_MULTIPLIER = 3.0   # Flag if 24hr volume > 3x average daily volume
WHALE_THRESHOLD_USD = 50_000  # Flag positions >= $50k as whale activity
WHALE_CHECK_TOP_N = 10   # Check whale activity on top-N spiking markets


class VolumeSpikeStrategy:
    """
    Scans all active markets every 5 minutes. For each market:
      1. Compute avg_daily_volume = total_volume / age_in_days
      2. spike_ratio = volume_24hr / avg_daily_volume
      3. If spike_ratio >= threshold → flag as spike
      4. On spike: check for whale positions and analyze order book
      5. Trade or log based on signal quality

    No DB history required — works immediately on a fresh deploy.
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

        # Track recently alerted spikes to avoid duplicate logging
        self._alerted_markets: dict[str, float] = {}

    async def start(self) -> None:
        self._running = True
        # Load adaptive threshold from DB; fall back to compile-time constant.
        db_threshold = await database.get_adaptive_param(
            self._db, "spike_threshold", default=None
        )
        self.spike_threshold: float = db_threshold if db_threshold is not None else SPIKE_MULTIPLIER
        logger.info(
            "Volume spike strategy starting — scanning %d markets every %ds, "
            "spike threshold %.1fx%s, whale threshold $%dk",
            MARKET_SCAN_LIMIT, SCAN_INTERVAL_SEC,
            self.spike_threshold,
            " (adaptive)" if db_threshold is not None else " (default)",
            WHALE_THRESHOLD_USD // 1000,
        )

    async def stop(self) -> None:
        self._running = False
        logger.info("Volume spike strategy stopped")

    async def run(self) -> None:
        while self._running:
            try:
                spikes = await self._scan_for_spikes()

                if spikes:
                    logger.info(
                        "Spike scan complete: %d spike(s) detected", len(spikes),
                    )
                    whale_markets = spikes[:WHALE_CHECK_TOP_N]
                    await self._check_whale_activity(whale_markets)

                    for spike in spikes:
                        await self._analyze_and_maybe_trade(spike)
                else:
                    logger.debug("Spike scan complete: 0 spikes")

                self._prune_old_alerts()
                await asyncio.sleep(SCAN_INTERVAL_SEC)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Volume spike loop error: %s", e)
                await asyncio.sleep(60)

    # ── Spike Detection (API-native, no DB history needed) ───────────────

    async def _scan_for_spikes(self) -> list[dict[str, Any]]:
        """Fetch active markets and flag those with unusual 24hr volume."""
        markets = await self._executor.get_active_markets(limit=MARKET_SCAN_LIMIT)
        logger.debug("Fetched %d active markets for spike scan", len(markets))

        spikes: list[dict[str, Any]] = []

        for m in markets:
            condition_id = (
                m.get("conditionId") or m.get("condition_id", "")
            )
            if not condition_id:
                continue
            market_id = condition_id

            total_volume = float(m.get("volume", 0))
            volume_24hr = float(m.get("volume24hr", 0))

            if total_volume <= 0 or volume_24hr <= 0:
                continue

            created_at = m.get("createdAt") or m.get("created_at", "")
            age_days = self._market_age_days(created_at)
            if age_days < MIN_MARKET_AGE_DAYS:
                continue

            avg_daily = total_volume / age_days
            if avg_daily < MIN_DAILY_AVG_USD:
                continue

            spike_ratio = volume_24hr / avg_daily

            if spike_ratio >= self.spike_threshold:
                prev = self._alerted_markets.get(market_id, 0)
                if spike_ratio <= prev * 1.2:
                    continue  # Already alerted at similar magnitude

                self._alerted_markets[market_id] = spike_ratio
                question = m.get("question", "")
                liq = float(m.get("liquidityNum", 0))
                spikes.append({
                    "market_id": market_id,
                    "question": question,
                    "slug": m.get("slug", ""),
                    "spike_ratio": spike_ratio,
                    "volume_24hr": volume_24hr,
                    "avg_daily": avg_daily,
                    "total_volume": total_volume,
                    "liquidity": liq,
                    "tokens": m.get("tokens", []),
                    "market": m,
                })
                logger.info(
                    "SPIKE: %.1fx on '%s' — 24h=$%.0f vs avg=$%.0f/day "
                    "(total=$%.0f, liq=$%.0f)",
                    spike_ratio, question[:60],
                    volume_24hr, avg_daily, total_volume, liq,
                )

        spikes.sort(key=lambda s: s["spike_ratio"], reverse=True)
        return spikes

    # ── Whale Detection ──────────────────────────────────────────────────

    async def _check_whale_activity(
        self, spikes: list[dict[str, Any]]
    ) -> None:
        """For top spiking markets, look for large positions via the data API."""
        for spike in spikes:
            tokens = spike.get("tokens", [])
            if not tokens:
                clob_market = await self._executor.get_clob_market(spike["market_id"])
                if clob_market:
                    tokens = clob_market.get("tokens", [])
                    spike["tokens"] = tokens
            if not tokens:
                continue

            for token in tokens:
                token_id = token.get("token_id", "")
                if not token_id:
                    continue

                positions = await self._fetch_top_positions(token_id)
                whales = [
                    p for p in positions
                    if float(p.get("currentValue", 0)) >= WHALE_THRESHOLD_USD
                ]
                if whales:
                    for w in whales:
                        addr = w.get("proxyWallet", w.get("user", "?"))[:12]
                        val = float(w.get("currentValue", 0))
                        size = float(w.get("size", 0))
                        logger.info(
                            "WHALE on '%s': %s holds %.0f shares ($%.0f) "
                            "outcome=%s",
                            spike["question"][:50], addr,
                            size, val, token.get("outcome", "?"),
                        )
                    spike["whale_count"] = spike.get("whale_count", 0) + len(whales)
                    spike["whale_value_usd"] = spike.get("whale_value_usd", 0) + sum(
                        float(w.get("currentValue", 0)) for w in whales
                    )

    async def _fetch_top_positions(self, token_id: str) -> list[dict[str, Any]]:
        """Fetch the largest positions on a given token."""
        url = "https://data-api.polymarket.com/positions"
        params = {
            "market": token_id,
            "sizeThreshold": "1000",
            "limit": "20",
            "sortBy": "currentValue",
            "sortOrder": "desc",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.debug("Top positions fetch failed for %s: %s", token_id[:16], e)
        return []

    # ── Signal Analysis & Trade ──────────────────────────────────────────

    async def _analyze_and_maybe_trade(
        self, spike: dict[str, Any]
    ) -> None:
        """Analyze a spike for actionable signals and optionally trade."""
        market_id = spike["market_id"]
        tokens = spike.get("tokens", [])

        if not tokens:
            clob_market = await self._executor.get_clob_market(market_id)
            if clob_market:
                tokens = clob_market.get("tokens", [])
                spike["tokens"] = tokens

        if not tokens:
            logger.debug("No tokens found for spike on '%s', skipping", spike["question"][:40])
            return

        best_token = max(tokens, key=lambda t: float(t.get("price", 0)))
        outcome = best_token.get("outcome", "YES").upper()
        token_id = best_token.get("token_id", "")

        if not token_id:
            return

        order_book = await self._executor.get_order_book(token_id)
        price_wall = self._detect_price_wall(order_book, outcome)

        whale_count = spike.get("whale_count", 0)
        whale_value = spike.get("whale_value_usd", 0)

        decision, rationale = self._decide_trade(
            spike["spike_ratio"], price_wall, whale_count, whale_value,
        )

        logger.info(
            "Spike analysis '%s': wall=%s, whales=%d ($%.0f) -> %s — %s",
            spike["question"][:40],
            "YES" if price_wall else "NO",
            whale_count, whale_value,
            decision, rationale,
        )

        position_id = None
        if decision in ("enter", "fade"):
            position_id = await self._execute_spike_trade(
                spike, outcome, order_book, token_id, decision,
            )

        try:
            await database.insert_spike_event(
                self._db,
                market_id=market_id,
                market_slug=spike.get("slug", ""),
                outcome=outcome,
                spike_magnitude=spike["spike_ratio"],
                rolling_avg=spike["avg_daily"],
                recent_volume=spike["volume_24hr"],
                price_wall=price_wall,
                trend_aligned=(whale_count > 0),
                trade_decision=decision,
                rationale=rationale,
                position_id=position_id,
            )
        except Exception as e:
            logger.debug("Failed to record spike event: %s", e)

    def _detect_price_wall(
        self, order_book: dict[str, Any], outcome: str,
    ) -> bool:
        """True if >50% of order book depth clusters at the top 2 price levels."""
        levels = order_book.get("asks" if outcome == "YES" else "bids", [])
        if len(levels) < 2:
            return False

        total_size = sum(float(l.get("size", 0)) for l in levels)
        if total_size == 0:
            return False

        sorted_levels = sorted(
            levels, key=lambda x: float(x.get("size", 0)), reverse=True,
        )
        top2_size = sum(float(l.get("size", 0)) for l in sorted_levels[:2])
        return (top2_size / total_size) >= config.VOLUME_SPIKE_PRICE_WALL_PCT

    def _decide_trade(
        self,
        spike_ratio: float,
        price_wall: bool,
        whale_count: int,
        whale_value_usd: float,
    ) -> tuple[str, str]:
        """
        Trade decision matrix:
        - Strong enter: whale-backed + price wall + big spike
        - Enter: price wall + spike ≥ 5x (strong conviction without whale data)
        - Fade: no wall, no whales, noisy spike → trade against
        - Skip: ambiguous signals
        """
        has_whales = whale_count > 0 and whale_value_usd >= WHALE_THRESHOLD_USD

        if has_whales and price_wall:
            return "enter", (
                f"Whale-backed conviction: {whale_count} whale(s) "
                f"(${whale_value_usd:,.0f}) + price wall + "
                f"{spike_ratio:.1f}x volume spike"
            )
        if price_wall and spike_ratio >= 5.0:
            return "enter", (
                f"Strong wall: {spike_ratio:.1f}x spike with price wall "
                f"(no whale confirmation)"
            )
        if has_whales and not price_wall:
            return "skip", (
                f"Whale activity (${whale_value_usd:,.0f}) but no price wall — "
                f"watching for follow-through"
            )
        if not price_wall and not has_whales and config.VOLUME_SPIKE_FADE_ENABLED:
            return "fade", (
                f"Retail noise: {spike_ratio:.1f}x spike, no wall, "
                f"no whales — fading"
            )
        return "skip", (
            f"Ambiguous: {spike_ratio:.1f}x spike, wall={price_wall}, "
            f"whales={whale_count}"
        )

    # ── Trade Execution ──────────────────────────────────────────────────

    async def _execute_spike_trade(
        self,
        spike: dict[str, Any],
        outcome: str,
        order_book: dict[str, Any],
        token_id: str,
        decision: str,
    ) -> int | None:
        market_id = spike["market_id"]
        market = spike.get("market")

        if decision == "fade":
            outcome = "NO" if outcome == "YES" else "YES"
            opposite_token = self._get_token_id(market, outcome)
            if opposite_token:
                order_book = await self._executor.get_order_book(opposite_token)
                token_id = opposite_token

        # Scale position size with spike magnitude: 5% base, up to 15%
        base_pct = 0.05
        scale = min(spike["spike_ratio"] / self.spike_threshold, 3.0)
        target_pct = min(base_pct * scale, config.VOLUME_SPIKE_MAX_POSITION_PCT)

        price = self._executor.calculate_limit_price(order_book, "BUY")
        if price <= 0 or price >= 1:
            price = min(max(price, 0.01), 0.99)
            if price <= 0:
                logger.warning("Skip spike trade: invalid price for %s", market_id[:16])
                return None

        price = min(price, 0.99)

        size_usd = self._wallet.calculate_position_size(target_pct, "volume_spike")
        size_tokens = size_usd / price
        cost = price * size_tokens

        if size_tokens < 5.0:
            logger.info(
                "Skip spike trade: size %.2f tokens ($%.2f) below CLOB minimum",
                size_tokens, size_usd,
            )
            return None

        allowed, reason = self._wallet.can_open_position(cost, "volume_spike")
        if not allowed:
            logger.info("Skip spike trade: %s", reason)
            return None

        if self._wallet.get_position_for_market(market_id):
            logger.info("Skip spike trade: already in %s", market_id[:16])
            return None

        question = spike.get("question", "")
        logger.info(
            "Executing spike trade: BUY %s %.1f tokens @ $%.4f ($%.2f) on '%s'",
            outcome, size_tokens, price, cost, question[:50],
        )

        position_id = await database.insert_position(
            self._db,
            market_id=market_id,
            market_slug=spike.get("slug", ""),
            market_question=question,
            outcome=outcome,
            side="BUY",
            entry_price=price,
            size=size_tokens,
            strategy="volume_spike",
            notes=(
                f"Spike {decision}: {spike['spike_ratio']:.1f}x volume, "
                f"whales={spike.get('whale_count', 0)}, "
                f"scaled {target_pct*100:.1f}%"
            ),
        )

        result = await self._executor.place_order(
            token_id=token_id,
            market_id=market_id,
            outcome=outcome,
            side="BUY",
            price=price,
            size=size_tokens,
            position_id=position_id,
        )

        if result["status"] not in ("filled", "pending"):
            await self._db.execute(
                "UPDATE positions SET status='cancelled' WHERE id=?",
                (position_id,),
            )
            await self._db.commit()
            logger.warning(
                "Spike trade order failed for %s: %s",
                market_id[:16], result.get("error"),
            )
            return None

        await self._wallet.refresh()
        return position_id

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _market_age_days(created_at: str) -> float:
        """Parse ISO date and return market age in days."""
        if not created_at:
            return 0.0
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max((now - created).total_seconds() / 86400, 0.1)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _get_token_id(
        market: dict[str, Any] | None, outcome: str,
    ) -> str:
        if not market:
            return ""
        for token in market.get("tokens", []):
            if token.get("outcome", "").upper() == outcome.upper():
                return token.get("token_id", "")
        tokens = market.get("tokens", [])
        return tokens[0].get("token_id", "") if tokens else ""

    def _prune_old_alerts(self) -> None:
        """Remove stale alert entries to allow re-alerting after cooldown."""
        if len(self._alerted_markets) > 1000:
            oldest = sorted(
                self._alerted_markets, key=self._alerted_markets.get  # type: ignore
            )[:500]
            for k in oldest:
                del self._alerted_markets[k]
