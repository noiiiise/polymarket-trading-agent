"""
Strategy 1: Copy Trading
Replicates trades from top-performing Polymarket wallets.

Leaderboard API:
  GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=MONTH&orderBy=PNL&limit=20

Positions API:
  GET https://data-api.polymarket.com/positions?user={address}&sizeThreshold=0.01
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

LEADERBOARD_API = "https://data-api.polymarket.com/v1/leaderboard"
POSITIONS_API = "https://data-api.polymarket.com/positions"


class CopyTradeStrategy:
    """
    Monitors top-performing Polymarket wallets and replicates their trades.

    Flow:
    1. Fetch top 10 wallets by monthly PnL from the Polymarket leaderboard API.
    2. Poll their current positions every 60s via data-api.
    3. When a tracked wallet opens a NEW position, mirror it with risk caps.
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

        self._tracked_wallets: dict[str, dict[str, Any]] = {}
        # address -> {token_id: position_info}  (keyed by token so YES and NO are separate)
        self._known_positions: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_leaderboard_refresh: datetime | None = None
        # Markets entered in the current polling cycle — prevents duplicate copies
        # when multiple tracked wallets hold the same market simultaneously.
        self._markets_entered_this_cycle: set[str] = set()

    async def start(self) -> None:
        self._running = True
        logger.info("Copy trade strategy starting...")
        await self._refresh_leaderboard()
        logger.info("Tracking %d wallets", len(self._tracked_wallets))

    async def stop(self) -> None:
        self._running = False
        logger.info("Copy trade strategy stopped")

    async def run(self) -> None:
        while self._running:
            try:
                if self._should_refresh_leaderboard():
                    await self._refresh_leaderboard()

                # Reset per-cycle dedup set so we never copy the same market twice
                # in a single polling round, even if multiple tracked wallets hold it.
                self._markets_entered_this_cycle: set[str] = set()

                for address, info in self._tracked_wallets.items():
                    try:
                        await self._check_wallet_positions(address, info)
                    except Exception as e:
                        logger.error("Error checking wallet %s: %s", address[:12], e)

                await asyncio.sleep(config.COPY_TRADE_POLL_INTERVAL_SEC)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Copy trade loop error: %s", e)
                await asyncio.sleep(config.COPY_TRADE_POLL_INTERVAL_SEC)

    # ── Leaderboard ─────────────────────────────────────────────────────────

    def _should_refresh_leaderboard(self) -> bool:
        if self._last_leaderboard_refresh is None:
            return True
        elapsed = datetime.utcnow() - self._last_leaderboard_refresh
        return elapsed.total_seconds() > config.COPY_TRADE_LEADERBOARD_REFRESH_HOURS * 3600

    async def _refresh_leaderboard(self) -> None:
        logger.info("Refreshing leaderboard...")

        if config.COPY_TRADE_PINNED_WALLETS:
            wallets = [
                {"address": addr, "userName": addr[:10], "pnl": 0, "vol": 0}
                for addr in config.COPY_TRADE_PINNED_WALLETS
            ]
        elif config.PAPER_TRADING:
            wallets = self._simulated_leaderboard()
        else:
            wallets = await self._fetch_leaderboard()
            wallets = [w for w in wallets if w.get("pnl", 0) > 10_000]

        top = wallets[:config.COPY_TRADE_TOP_WALLETS_COUNT]

        self._tracked_wallets = {}
        for i, w in enumerate(top):
            addr = w["address"]
            self._tracked_wallets[addr] = {
                "rank": i + 1,
                "userName": w.get("userName", "anon"),
                "pnl": w.get("pnl", 0),
                "vol": w.get("vol", 0),
            }
            await self._db.execute(
                """INSERT INTO tracked_wallets
                   (address, rank, total_profit, trade_count, win_rate, last_refreshed)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(address) DO UPDATE SET
                   rank=excluded.rank, total_profit=excluded.total_profit,
                   trade_count=excluded.trade_count, win_rate=excluded.win_rate,
                   last_refreshed=excluded.last_refreshed""",
                (addr, i + 1, w.get("pnl", 0), 0, 0, datetime.utcnow().isoformat()),
            )

        await self._db.commit()
        self._last_leaderboard_refresh = datetime.utcnow()

        top5 = [f"{w.get('userName', 'anon')}: ${w.get('pnl', 0)/1000:.0f}k" for w in top[:5]]
        logger.info(
            "Leaderboard refreshed — %d wallets tracked. Top 5: %s",
            len(self._tracked_wallets), ", ".join(top5),
        )

    async def _fetch_leaderboard(self) -> list[dict[str, Any]]:
        params = {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": "50"}
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
                    logger.error("Leaderboard API returned %d", resp.status)
                    return []
        except Exception as e:
            logger.error("Leaderboard fetch error: %s", e)
            return []

    def _simulated_leaderboard(self) -> list[dict[str, Any]]:
        return [
            {
                "address": f"0x{'%040x' % (0xDEAD0000 + i)}",
                "userName": f"sim_trader_{i}",
                "pnl": 500_000 - i * 15_000 + (i % 3) * 5_000,
                "vol": 1_000_000 + i * 50_000,
            }
            for i in range(30)
        ]

    # ── Position Monitoring ─────────────────────────────────────────────────

    async def _check_wallet_positions(
        self, address: str, info: dict[str, Any]
    ) -> None:
        current_positions = await self._fetch_wallet_positions(address)

        # known is empty on the very first scan — all positions are treated as
        # actionable so existing holdings are copied immediately on startup.
        known = self._known_positions.get(address, {})

        for pos in current_positions:
            token_id = pos["token_id"]
            if token_id in known:
                continue

            logger.info(
                "Position to copy from %s (rank #%d): %s %s in %s",
                info.get("userName", address[:12]), info["rank"],
                pos.get("outcome", "?"), pos.get("market_id", "?")[:16],
                pos.get("market_question", "")[:40] or pos.get("market_id", "")[:16],
            )
            await self._evaluate_and_copy(address, info, pos)

        self._known_positions[address] = {p["token_id"]: p for p in current_positions}

    async def _fetch_wallet_positions(self, address: str) -> list[dict[str, Any]]:
        """
        Fetch current open positions for a wallet.
        Uses data-api.polymarket.com which returns positions with direct token IDs.
        """
        if config.PAPER_TRADING:
            return self._simulated_wallet_positions(address)

        params = {
            "user": address.lower(),
            "sizeThreshold": "0.01",
            "limit": "100",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    POSITIONS_API,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [
                            {
                                "token_id": p.get("asset", ""),
                                "market_id": p.get("conditionId", ""),
                                "market_slug": "",
                                "market_question": p.get("title", ""),
                                "outcome": p.get("outcome", "YES").upper(),  # use API value directly
                                "side": "BUY",
                                "size": float(p.get("size", 0)),
                                "avg_price": float(p.get("avgPrice", p.get("averagePrice", 0))),
                                "current_value": float(p.get("currentValue", 0)),
                            }
                            for p in data
                            if float(p.get("size", 0)) > 0 and p.get("asset")
                        ]
                    logger.debug("Positions API returned %d for %s", resp.status, address[:12])
                    return []
        except Exception as e:
            logger.error("Position fetch error for %s: %s", address[:12], e)
            return []

    def _simulated_wallet_positions(self, address: str) -> list[dict[str, Any]]:
        import hashlib, random
        seed = int(hashlib.md5(
            f"{address}{datetime.utcnow().hour}".encode()
        ).hexdigest()[:8], 16)
        rng = random.Random(seed)
        positions = []
        for i in range(rng.randint(2, 6)):
            mid = f"sim-market-{rng.randint(0, 19):04d}"
            outcome = rng.choice(["YES", "NO"])
            token_id = f"{mid}-{'yes' if outcome == 'YES' else 'no'}"
            positions.append({
                "token_id": token_id,
                "market_id": mid,
                "market_slug": f"sim-slug-{mid}",
                "market_question": f"Simulated question for {mid}?",
                "outcome": outcome,
                "side": "BUY",
                "size": rng.uniform(50, 500),
                "avg_price": rng.uniform(0.30, 0.70),
                "current_value": rng.uniform(1000, 50000),
            })
        return positions

    # ── Trade Evaluation & Execution ────────────────────────────────────────

    async def _evaluate_and_copy(
        self,
        source_address: str,
        wallet_info: dict[str, Any],
        position: dict[str, Any],
    ) -> None:
        market_id = position["market_id"]
        token_id = position["token_id"]

        # 1. Fetch market info for slug, question, and resolution time check.
        # NOTE: Polymarket neg-risk markets return wrong/empty token data from the
        # Gamma API. Only apply Gamma enrichment when tokens are present; otherwise
        # fall back to the outcome and token_id from the positions API, which are
        # always correct.
        market = await self._executor.get_market_info(market_id)
        if market and market.get("tokens"):
            outcome = _get_outcome_for_token(market, token_id)
            position["outcome"] = outcome
            position["market_question"] = market.get("question", position.get("market_question", ""))
            position["market_slug"] = market.get("slug", "")

            end_date_str = market.get("end_date_iso", "")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hours_until = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_until < config.COPY_TRADE_MIN_RESOLUTION_HOURS:
                        logger.info(
                            "Skip copy: market resolves in %.1fh (min %.1fh)",
                            hours_until, config.COPY_TRADE_MIN_RESOLUTION_HOURS,
                        )
                        return
                except (ValueError, TypeError):
                    pass

            if not token_id:
                token_id = _get_token_id(market, position["outcome"])
                position["token_id"] = token_id

        # 2. Already have a position in this market? Check both the DB-backed wallet
        #    state AND the in-cycle set (catches cases where wallet hasn't refreshed yet).
        if self._wallet.get_position_for_market(market_id):
            logger.info("Skip copy: already have position in %s", market_id[:16])
            return
        if market_id in self._markets_entered_this_cycle:
            logger.info("Skip copy: already copied %s this cycle", market_id[:16])
            return

        # 3. Spread check
        order_book = await self._executor.get_order_book(token_id)
        if order_book["spread"] > config.COPY_TRADE_MAX_SPREAD_PCT:
            logger.info(
                "Skip copy: spread %.2f%% > max %.2f%%",
                order_book["spread"] * 100, config.COPY_TRADE_MAX_SPREAD_PCT * 100,
            )
            return

        # 4. Position size.
        # The Polymarket positions API returns current_value for each individual
        # position, not the wallet's total portfolio value — so we cannot reliably
        # compute a proportional allocation. Use a fixed per-trade cap instead;
        # the risk checks below still enforce exposure limits.
        size_usd = self._wallet.calculate_position_size(
            config.COPY_TRADE_MAX_POSITION_PCT, "copy_trade"
        )
        price = self._executor.calculate_limit_price(order_book, "BUY")

        if price <= 0:
            logger.warning("Skip copy: invalid price %.4f for %s", price, market_id[:16])
            return

        # CLOB accepts prices in (0, 1) exclusive at 0.01 tick size → max is 0.99
        price = min(price, 0.99)

        size_tokens = size_usd / price
        cost = price * size_tokens

        # 5. Minimum size guard — CLOB rejects orders below 1 share.
        if size_tokens < 1.0:
            logger.info(
                "Skip copy: position size %.4f tokens ($%.2f) below CLOB minimum of 1 share",
                size_tokens, size_usd,
            )
            return

        # 6. Risk check
        allowed, reason = self._wallet.can_open_position(cost, "copy_trade")
        if not allowed:
            logger.info("Skip copy: risk check failed — %s", reason)
            return

        # 7. Execute
        outcome = position.get("outcome", "YES")
        logger.info(
            "Executing copy trade: BUY %s %.2f tokens @ $%.4f ($%.2f) from %s (rank #%d)",
            outcome, size_tokens, price, cost,
            wallet_info.get("userName", source_address[:12]), wallet_info["rank"],
        )

        position_id = await database.insert_position(
            self._db,
            market_id=market_id,
            market_slug=position.get("market_slug", ""),
            market_question=position.get("market_question", ""),
            outcome=outcome,
            side="BUY",
            entry_price=price,
            size=size_tokens,
            strategy="copy_trade",
            source_wallet=source_address,
            notes=f"Copied from {wallet_info.get('userName', 'anon')} (rank #{wallet_info['rank']})",
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
                "UPDATE positions SET status='cancelled' WHERE id=?", (position_id,)
            )
            await self._db.commit()
            logger.warning("Copy trade order failed for %s: %s", market_id[:16], result.get("error"))
        else:
            # Immediately mark this market as entered so subsequent wallets in this
            # cycle don't copy it again before the wallet manager refreshes.
            self._markets_entered_this_cycle.add(market_id)
            await self._wallet.refresh()


# ── Module-level helpers ─────────────────────────────────────────────────────

def _get_outcome_for_token(market: dict[str, Any], token_id: str) -> str:
    """Resolve YES/NO outcome by matching token_id against market tokens."""
    for token in market.get("tokens", []):
        if token.get("token_id") == token_id:
            return token.get("outcome", "YES").upper()
    return "YES"


def _get_token_id(market: dict[str, Any], outcome: str) -> str:
    """Get token_id for a given outcome from market data."""
    outcome_lower = outcome.lower()
    for token in market.get("tokens", []):
        if token.get("outcome", "").lower() == outcome_lower:
            return token.get("token_id", "")
    if market.get("tokens"):
        return market["tokens"][0].get("token_id", "")
    return ""
