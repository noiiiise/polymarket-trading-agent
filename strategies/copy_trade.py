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


def compute_wallet_score(stats: dict) -> float:
    """
    Composite score rewarding consistent edge over lucky streaks.
    Wallets with <5 trades get heavily discounted (no track record with us yet).

    Why: sorting by raw PnL promotes a whale who made $50K in one lucky bet
    over a trader who made $30K through 40 consistent wins. The latter has
    real edge; the former might not.
    """
    total_trades = stats.get("wins", 0) + stats.get("losses", 0)
    if total_trades == 0:
        return 0.0

    win_rate = stats["wins"] / total_trades
    roi_proxy = stats.get("total_pnl", 0) / max(total_trades, 1)

    # Credibility: 50 trades = ~50% weight, 10 trades = ~17%, 5 = ~9%
    # This prevents one-hit wonders from dominating the score.
    credibility = total_trades / (total_trades + 50)

    score = (win_rate * 0.6 + min(roi_proxy / 500, 0.4)) * credibility
    return max(score, 0.0)


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
        await self._reconcile_own_positions()
        await self._place_exits_for_existing_positions()

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

            # Re-rank by composite score (win-rate × credibility) so consistent
            # traders beat one-hit whales.  Fall back to 0.05 for wallets with
            # no history in our copy_trade_stats table.
            scored: list[tuple[dict, float]] = []
            for w in wallets:
                addr = w["address"]
                rows = await self._db.execute_fetchall(
                    "SELECT * FROM copy_trade_stats WHERE wallet_address = ?",
                    (addr,),
                )
                if rows:
                    score = compute_wallet_score(dict(rows[0]))
                else:
                    score = 0.05  # no history with us yet
                scored.append((w, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            wallets = [w for w, _ in scored]

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

        logger.info(
            "Evaluating copy: market=%s token=%s outcome=%s question=%s",
            market_id[:16], token_id[:16], position.get("outcome"),
            (position.get("market_question") or "")[:40],
        )

        # 1. Fetch market info for slug, question, and resolution time check.
        # NOTE: Polymarket neg-risk markets return wrong/empty token data from the
        # Gamma API. Only apply Gamma enrichment when tokens are present; otherwise
        # fall back to the outcome and token_id from the positions API, which are
        # always correct.
        market = await self._executor.get_market_info(market_id)
        has_tokens = bool(market and market.get("tokens"))
        logger.info("Gamma lookup: has_tokens=%s, question=%s", has_tokens,
                     (market.get("question", "") if market else "no market")[:40])

        if has_tokens:
            outcome = _get_outcome_for_token(market, token_id)
            position["outcome"] = outcome
            position["market_question"] = market.get("question", position.get("market_question", ""))
            position["market_slug"] = market.get("slug", "")

            end_date_str = market.get("end_date_iso", "")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hours_until = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                    days_until = hours_until / 24.0
                    if hours_until < config.COPY_TRADE_MIN_RESOLUTION_HOURS:
                        logger.info(
                            "Skip copy: market resolves too soon in %.1fh (min %.1fh)",
                            hours_until, config.COPY_TRADE_MIN_RESOLUTION_HOURS,
                        )
                        return
                    if days_until > config.COPY_TRADE_MAX_RESOLUTION_DAYS:
                        logger.info(
                            "Skip copy: market resolves too far out in %.1fd (max %.0fd)",
                            days_until, config.COPY_TRADE_MAX_RESOLUTION_DAYS,
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
        logger.info(
            "Order book: bid=%.4f ask=%.4f spread=%.4f",
            order_book["best_bid"], order_book["best_ask"], order_book["spread"],
        )
        if order_book["spread"] > config.COPY_TRADE_MAX_SPREAD_PCT:
            logger.info(
                "Skip copy: spread %.2f%% > max %.2f%%",
                order_book["spread"] * 100, config.COPY_TRADE_MAX_SPREAD_PCT * 100,
            )
            return

        # 4. Position size.
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

        logger.info(
            "Sizing: size_usd=%.2f price=%.4f size_tokens=%.4f cost=%.4f",
            size_usd, price, size_tokens, cost,
        )

        # 5. Minimum size guard — CLOB minimum varies by market (typically 5 shares).
        if size_tokens < 5.0:
            logger.info(
                "Skip copy: position size %.4f tokens ($%.2f) below CLOB minimum of 5 shares",
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

        exit_target = round(min(price * (1.0 + config.COPY_TRADE_EXIT_PROFIT_PCT), 0.99), 2)

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
            token_id=token_id,
            exit_target=exit_target,
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
            self._markets_entered_this_cycle.add(market_id)
            await self._wallet.refresh()
            # Place limit SELL at +5% so we exit on any 5% rally without waiting for resolution.
            await self._place_exit_order(position_id, token_id, exit_target, size_tokens)


    # ── Exit order helpers ───────────────────────────────────────────────────

    async def _place_exit_order(
        self,
        position_id: int,
        token_id: str,
        exit_price: float,
        size: float,
    ) -> None:
        """Place a +5% profit-taking limit SELL and record target on the position."""
        if size < 5.0:
            logger.info(
                "Exit order skipped: size %.2f below CLOB minimum (position_id=%d)",
                size, position_id,
            )
            return
        if not token_id:
            logger.warning("Exit order skipped: no token_id for position %d", position_id)
            return
        try:
            result = await self._executor.place_exit_sell(token_id, exit_price, size)
            logger.info(
                "Exit order placed: position=%d sell %.2f shares @ $%.4f (+%.0f%% target)",
                position_id, size, exit_price,
                config.COPY_TRADE_EXIT_PROFIT_PCT * 100,
            )
            logger.debug("Exit order CLOB response: %s", result)
        except Exception as e:
            e_str = str(e)
            # "balance is not enough" where balance == sum of active orders means
            # a sell order for this position is ALREADY in the CLOB order book.
            # Treat as success — just record the target.
            if "sum of active orders" in e_str and "balance is not enough" in e_str:
                logger.info(
                    "Exit order already active in CLOB for position %d (%.2f @ $%.4f)",
                    position_id, size, exit_price,
                )
            else:
                logger.warning(
                    "Exit order placement failed for position %d: %s", position_id, e
                )
                return  # Don't record exit_target if placement genuinely failed

        await self._db.execute(
            "UPDATE positions SET exit_target=? WHERE id=?",
            (exit_price, position_id),
        )
        await self._db.commit()

    async def _reconcile_own_positions(self) -> None:
        """
        Fetch the agent wallet's live positions from the Data API and insert DB
        records for any that aren't tracked.  This handles positions placed before
        DB tracking was reliable, or positions whose initial order record was
        cancelled despite the on-chain trade going through.
        """
        own_addr = config.POLYMARKET_WALLET_ADDRESS
        if not own_addr:
            return

        live_positions = await self._fetch_wallet_positions(own_addr)
        if not live_positions:
            logger.info("Reconcile: no live positions found for own wallet.")
            return

        reconciled = 0
        for pos in live_positions:
            current_value = float(pos.get("current_value", 0))
            if current_value <= 0.01:
                continue  # Skip worthless/expired positions

            market_id = pos.get("market_id", "")
            token_id = pos.get("token_id", "")
            outcome = pos.get("outcome", "YES")
            entry_price = float(pos.get("avg_price", 0))
            size = float(pos.get("size", 0))

            if not market_id or entry_price <= 0 or size <= 0:
                continue

            # Normalize outcome — DB only stores 'YES'/'NO'
            outcome_norm = outcome.upper()
            if outcome_norm not in ("YES", "NO"):
                continue  # Sports markets ('Ducks', 'Blues', etc.) — skip

            outcome = outcome_norm

            existing = await self._db.execute_fetchall(
                "SELECT id FROM positions WHERE market_id=? AND status='open'",
                (market_id,),
            )
            if existing:
                continue  # Already tracked

            exit_target = round(min(entry_price * (1.0 + config.COPY_TRADE_EXIT_PROFIT_PCT), 0.99), 2)
            pos_id = await database.insert_position(
                self._db,
                market_id=market_id,
                market_slug=pos.get("market_slug", ""),
                market_question=pos.get("market_question", ""),
                outcome=outcome,
                side="BUY",
                entry_price=entry_price,
                size=size,
                strategy="copy_trade",
                source_wallet="0x7f3c8979d0afa00007bae4747d5347122af05613",
                token_id=token_id,
                exit_target=None,
                notes="Reconciled from wallet (pre-tracking position)",
            )
            logger.info(
                "Reconciled position #%d: %s %s entry=%.4f size=%.2f exit_target=%.4f",
                pos_id, outcome, (pos.get("market_question") or market_id)[:40],
                entry_price, size, exit_target,
            )
            reconciled += 1

        if reconciled:
            logger.info("Reconciled %d untracked positions from wallet.", reconciled)

    async def _place_exits_for_existing_positions(self) -> None:
        """
        On startup, place exit SELL orders for any open copy-trade positions
        that don't yet have one (e.g. positions opened before this feature,
        or exits that failed on a previous run).
        """
        rows = await self._db.execute_fetchall(
            """SELECT id, market_id, outcome, entry_price, size, token_id, exit_target
               FROM positions
               WHERE status='open' AND strategy='copy_trade'
                 AND exit_target IS NULL"""
        )
        if not rows:
            logger.info("No existing positions need exit orders.")
            return

        logger.info(
            "Placing exit orders for %d existing copy-trade positions…", len(rows)
        )
        for row in rows:
            pos_id = row["id"]
            token_id: str = row["token_id"] or ""
            entry_price = float(row["entry_price"])
            size = float(row["size"])
            outcome = row["outcome"]

            # Re-fetch token_id from CLOB if not stored
            if not token_id:
                clob_market = await self._executor.get_clob_market(row["market_id"])
                if clob_market:
                    token_id = _get_token_id(clob_market, outcome)
                    if token_id:
                        await self._db.execute(
                            "UPDATE positions SET token_id=? WHERE id=?",
                            (token_id, pos_id),
                        )
                        await self._db.commit()

            exit_price = round(min(entry_price * (1.0 + config.COPY_TRADE_EXIT_PROFIT_PCT), 0.99), 2)
            await self._place_exit_order(pos_id, token_id, exit_price, size)


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
