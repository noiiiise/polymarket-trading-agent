"""
Strategy 1: Copy Trading
Replicates trades from top-performing Polymarket wallets.

Uses the real Polymarket leaderboard API:
GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=MONTH&orderBy=PNL&limit=20
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

logger = logging.getLogger("strategy.copy_trade")

# Polymarket leaderboard API endpoint
LEADERBOARD_API = "https://data-api.polymarket.com/v1/leaderboard"


class CopyTradeStrategy:
    """
    Monitors top-performing Polymarket wallets and replicates their trades.

    Flow:
    1. Fetch top 20 wallets by monthly PnL from the Polymarket leaderboard API.
    2. Poll their positions every 60s.
    3. When a tracked wallet opens a new position, mirror it with risk caps.
    4. Log all decisions and outcomes to the database.
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

        # Tracked wallets: address -> wallet info
        self._tracked_wallets: dict[str, dict[str, Any]] = {}
        # Last known positions per wallet: address -> {market_id: position_info}
        self._known_positions: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_leaderboard_refresh: datetime | None = None

    async def start(self) -> None:
        """Start the copy trade strategy loop."""
        self._running = True
        logger.info("Copy trade strategy starting...")
        await self._refresh_leaderboard()
        logger.info("Tracking %d wallets", len(self._tracked_wallets))

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False
        logger.info("Copy trade strategy stopped")

    async def run(self) -> None:
        """Main strategy loop: poll tracked wallets for new positions."""
        while self._running:
            try:
                # Refresh leaderboard periodically
                if self._should_refresh_leaderboard():
                    await self._refresh_leaderboard()

                # Check each tracked wallet for new positions
                for address, info in self._tracked_wallets.items():
                    try:
                        await self._check_wallet_positions(address, info)
                    except Exception as e:
                        logger.error(
                            "Error checking wallet %s: %s", address[:12], e
                        )

                await asyncio.sleep(config.COPY_TRADE_POLL_INTERVAL_SEC)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Copy trade loop error: %s", e)
                await asyncio.sleep(config.COPY_TRADE_POLL_INTERVAL_SEC)

    # ── Leaderboard ─────────────────────────────────────────────────────────

    def _should_refresh_leaderboard(self) -> bool:
        """Check if it's time to refresh the leaderboard."""
        if self._last_leaderboard_refresh is None:
            return True
        elapsed = datetime.utcnow() - self._last_leaderboard_refresh
        return elapsed.total_seconds() > (
            config.COPY_TRADE_LEADERBOARD_REFRESH_HOURS * 3600
        )

    async def _refresh_leaderboard(self) -> None:
        """Fetch and rank top wallets from Polymarket's leaderboard API."""
        logger.info("Refreshing leaderboard from Polymarket data API...")

        if config.PAPER_TRADING:
            wallets = self._simulated_leaderboard()
        else:
            wallets = await self._fetch_leaderboard()

        # Filter: must have positive PnL and reasonable trade count
        qualified = [
            w for w in wallets
            if w.get("pnl", 0) > 10000  # Only profitable wallets
        ]

        # Take top N
        top = qualified[:config.COPY_TRADE_TOP_WALLETS_COUNT]

        # Update tracked wallets
        self._tracked_wallets = {}
        for i, w in enumerate(top):
            addr = w["address"]
            self._tracked_wallets[addr] = {
                "rank": i + 1,
                "userName": w.get("userName", "anon"),
                "pnl": w.get("pnl", 0),
                "vol": w.get("vol", 0),
            }

            # Persist to DB
            await self._db.execute(
                """INSERT INTO tracked_wallets
                   (address, rank, total_profit, trade_count, win_rate, last_refreshed)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(address) DO UPDATE SET
                   rank=excluded.rank, total_profit=excluded.total_profit,
                   trade_count=excluded.trade_count, win_rate=excluded.win_rate,
                   last_refreshed=excluded.last_refreshed""",
                (addr, i + 1, w.get("pnl", 0),
                 0, 0,  # trade_count and win_rate not in API
                 datetime.utcnow().isoformat()),
            )

        await self._db.commit()
        self._last_leaderboard_refresh = datetime.utcnow()

        # Log top 5 for visibility
        top5 = [f"{w.get('userName', 'anon')}: ${w.get('pnl', 0)/1000:.0f}k" for w in top[:5]]
        logger.info(
            "Leaderboard updated: %d wallets tracked. Top 5: %s",
            len(self._tracked_wallets), ", ".join(top5),
        )

    async def _fetch_leaderboard(self) -> list[dict[str, Any]]:
        """Fetch leaderboard data from Polymarket's data API."""
        params = {
            "category": "OVERALL",
            "timePeriod": "MONTH",
            "orderBy": "PNL",
            "limit": "50",
        }
        url = f"{LEADERBOARD_API}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [
                            {
                                "address": entry.get("proxyWallet", ""),
                                "userName": entry.get("userName", "anon"),
                                "pnl": float(entry.get("pnl", 0)),
                                "vol": float(entry.get("vol", 0)),
                            }
                            for entry in data
                            if entry.get("proxyWallet")
                        ]
                    else:
                        logger.error("Leaderboard API returned %d", resp.status)
                        return []
        except Exception as e:
            logger.error("Leaderboard fetch error: %s", e)
            return []

    def _simulated_leaderboard(self) -> list[dict[str, Any]]:
        """Generate simulated leaderboard for paper trading."""
        wallets = []
        for i in range(30):
            wallets.append({
                "address": f"0x{'%040x' % (0xDEAD0000 + i)}",
                "userName": f"sim_trader_{i}",
                "pnl": 500000 - i * 15000 + (i % 3) * 5000,
                "vol": 1000000 + i * 50000,
            })
        return wallets

    # ── Position Monitoring ─────────────────────────────────────────────────

    async def _check_wallet_positions(
        self, address: str, info: dict[str, Any]
    ) -> None:
        """Check a tracked wallet for new positions and copy them."""
        current_positions = await self._fetch_wallet_positions(address)

        if address not in self._known_positions:
            # First scan: just record, don't copy (avoid copying stale positions)
            self._known_positions[address] = {
                p["market_id"]: p for p in current_positions
            }
            logger.debug(
                "Initial scan for %s (%s): %d positions",
                address[:12], info.get("userName", "anon"), len(current_positions),
            )
            return

        known = self._known_positions[address]

        for pos in current_positions:
            market_id = pos["market_id"]

            if market_id in known:
                continue  # Already known position

            # New position detected!
            logger.info(
                "New position detected from %s (rank #%d): %s %s in %s",
                info.get("userName", address[:12]), info["rank"],
                pos.get("side", "BUY"), pos.get("outcome", "?"),
                market_id[:12],
            )

            await self._evaluate_and_copy(address, info, pos)

        # Update known positions
        self._known_positions[address] = {
            p["market_id"]: p for p in current_positions
        }

    async def _fetch_wallet_positions(
        self, address: str
    ) -> list[dict[str, Any]]:
        """Fetch current positions for a wallet."""
        if config.PAPER_TRADING:
            return self._simulated_wallet_positions(address)

        # Use Gamma API for positions
        url = f"{config.POLYMARKET_GAMMA_API}/positions"
        params = {"user": address, "sizeThreshold": "0.01"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [
                            {
                                "market_id": p.get("market", {}).get("id", p.get("conditionId", "")),
                                "market_slug": p.get("market", {}).get("slug", ""),
                                "market_question": p.get("market", {}).get("question", ""),
                                "outcome": p.get("outcome", "YES"),
                                "side": "BUY",
                                "size": float(p.get("size", 0)),
                                "avg_price": float(p.get("avgPrice", 0)),
                                "wallet_value": float(p.get("currentValue", 0)),
                            }
                            for p in data
                        ]
                    return []
        except Exception as e:
            logger.error("Position fetch error for %s: %s", address[:12], e)
            return []

    def _simulated_wallet_positions(
        self, address: str
    ) -> list[dict[str, Any]]:
        """Generate simulated positions for paper trading."""
        import hashlib
        import random

        # Use address + current hour for slowly-changing positions
        seed = int(hashlib.md5(
            f"{address}{datetime.utcnow().hour}".encode()
        ).hexdigest()[:8], 16)
        rng = random.Random(seed)

        positions = []
        num = rng.randint(2, 6)
        for i in range(num):
            mid = f"sim-market-{rng.randint(0, 19):04d}"
            positions.append({
                "market_id": mid,
                "market_slug": f"sim-slug-{mid}",
                "market_question": f"Simulated question for {mid}?",
                "outcome": rng.choice(["YES", "NO"]),
                "side": "BUY",
                "size": rng.uniform(50, 500),
                "avg_price": rng.uniform(0.30, 0.70),
                "wallet_value": rng.uniform(1000, 50000),
            })
        return positions

    # ── Trade Evaluation & Execution ────────────────────────────────────────

    async def _evaluate_and_copy(
        self,
        source_address: str,
        wallet_info: dict[str, Any],
        position: dict[str, Any],
    ) -> None:
        """Evaluate whether to copy a trade and execute if approved."""
        market_id = position["market_id"]

        # 1. Check market resolution time
        market = await self._executor.get_market_info(market_id)
        if market:
            end_date_str = market.get("end_date_iso", "")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                    hours_until = (
                        end_date - datetime.now(timezone.utc)
                    ).total_seconds() / 3600
                    if hours_until < config.COPY_TRADE_MIN_RESOLUTION_HOURS:
                        logger.info(
                            "Skip copy: market %s resolves in %.1f hours (min: %.1f)",
                            market_id[:12], hours_until,
                            config.COPY_TRADE_MIN_RESOLUTION_HOURS,
                        )
                        return
                except (ValueError, TypeError):
                    pass

        # 2. Check if we already have a position in this market
        if self._wallet.get_position_for_market(market_id):
            logger.info("Skip copy: already have position in %s", market_id[:12])
            return

        # 3. Get order book and check spread
        token_id = self._get_token_id(market, position["outcome"])
        order_book = await self._executor.get_order_book(token_id)

        if order_book["spread"] > config.COPY_TRADE_MAX_SPREAD_PCT:
            logger.info(
                "Skip copy: spread %.2f%% > max %.2f%% for %s",
                order_book["spread"] * 100,
                config.COPY_TRADE_MAX_SPREAD_PCT * 100,
                market_id[:12],
            )
            return

        # 4. Calculate position size
        # Use same percentage as the tracked wallet's allocation
        wallet_value = position.get("wallet_value", 1)
        if wallet_value > 0:
            position_pct = position["size"] * position["avg_price"] / wallet_value
        else:
            position_pct = 0.02  # Default 2%

        size_usd = self._wallet.calculate_position_size(position_pct, "copy_trade")
        price = self._executor.calculate_limit_price(order_book, "BUY")

        if price <= 0:
            logger.warning("Skip copy: invalid price for %s", market_id[:12])
            return

        size_tokens = size_usd / price

        # 5. Risk check
        cost = price * size_tokens
        allowed, reason = self._wallet.can_open_position(cost, "copy_trade")
        if not allowed:
            logger.info("Skip copy: risk check failed — %s", reason)
            return

        # 6. Execute
        logger.info(
            "Executing copy trade: %s %s %.2f tokens @ $%.4f ($%.2f) from %s",
            "BUY", position["outcome"], size_tokens, price, cost,
            wallet_info.get("userName", source_address[:12]),
        )

        # Record position in DB
        question = position.get("market_question", "")
        slug = position.get("market_slug", "")
        position_id = await database.insert_position(
            self._db,
            market_id=market_id,
            market_slug=slug,
            market_question=question,
            outcome=position["outcome"],
            side="BUY",
            entry_price=price,
            size=size_tokens,
            strategy="copy_trade",
            source_wallet=source_address,
            notes=f"Copied from {wallet_info.get('userName', 'anon')} (rank #{wallet_info['rank']})",
        )

        # Place order
        result = await self._executor.place_order(
            token_id=token_id,
            market_id=market_id,
            side="BUY",
            price=price,
            size=size_tokens,
            position_id=position_id,
        )

        if result["status"] != "filled":
            # Cancel the position if order failed
            await self._db.execute(
                "UPDATE positions SET status='cancelled' WHERE id=?",
                (position_id,),
            )
            await self._db.commit()
            logger.warning("Copy trade order failed for %s", market_id[:12])

        # Refresh wallet state
        await self._wallet.refresh()

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

        # Fallback: return first token
        if tokens:
            return tokens[0].get("token_id", "")
        return f"{market.get('id', 'unknown')}-{outcome.lower()}"
