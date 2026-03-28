"""
Wallet manager: real-time balance tracking, position management, and risk enforcement.
Fetches live balance from Polygon chain (or simulates in paper mode).
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
import aiosqlite

import config
import database

logger = logging.getLogger("wallet")


class WalletManager:
    """
    Central wallet state manager.
    - Fetches live USDC.e balance from Polygon via on-chain RPC (primary method).
    - Falls back to Gamma API and data-api if RPC fails.
    - Tracks all open positions from the database.
    - Enforces position caps and total exposure limits before every trade.
    """

    def __init__(self) -> None:
        self.balance: float = 0.0
        self.positions: list[dict[str, Any]] = []
        self.total_exposure: float = 0.0
        self.last_refresh: datetime | None = None
        self._db: aiosqlite.Connection | None = None
        self._running: bool = False
        self._wallet_address: str = config.POLYMARKET_WALLET_ADDRESS

        # RPC rotation state — start with the primary, then cycle through fallbacks
        self._rpc_pool: list[str] = [config.POLYGON_RPC_URL] + [
            u for u in config.POLYGON_RPC_FALLBACKS if u != config.POLYGON_RPC_URL
        ]
        self._rpc_index: int = 0

        # Paper trading state
        if config.PAPER_TRADING:
            self.balance = config.PAPER_TRADING_INITIAL_BALANCE
            logger.info("Paper trading mode: starting balance $%.2f", self.balance)

    async def start(self, db: aiosqlite.Connection) -> None:
        """Initialize the wallet manager with a database connection."""
        self._db = db
        await self.refresh()
        self._running = True
        logger.info(
            "Wallet manager started — balance: $%.2f, exposure: $%.2f",
            self.balance, self.total_exposure,
        )

    async def stop(self) -> None:
        """Stop the wallet manager."""
        self._running = False
        logger.info("Wallet manager stopped")

    async def refresh(self) -> None:
        """Refresh balance and positions from chain/db."""
        if not config.PAPER_TRADING:
            # Balance = cash (USDC.e on-chain) + portfolio value (positions inside Polymarket).
            # Most funds are typically inside Polymarket contracts, not as raw USDC.e.
            cash = await self._fetch_balance_onchain()
            portfolio = await self._fetch_portfolio_value()
            total = cash + portfolio

            if total > 0.0:
                self.balance = total
                logger.info("Wallet balance: $%.2f (cash=$%.2f + positions=$%.2f)", total, cash, portfolio)
            else:
                logger.error("All balance fetch methods returned 0, keeping last known: $%.2f", self.balance)
        else:
            # In paper mode, balance = initial - open exposure + realized P&L
            await self._recalculate_paper_balance()

        if self._db:
            self.positions = await database.get_open_positions(self._db)
            self.total_exposure = await database.get_total_exposure(self._db)

            # Snapshot balance for historical tracking
            await database.record_balance_snapshot(
                self._db, self.balance, self.total_exposure
            )

        self.last_refresh = datetime.utcnow()
        logger.debug(
            "Wallet refreshed — balance: $%.2f, exposure: $%.2f, positions: %d",
            self.balance, self.total_exposure, len(self.positions),
        )

    async def _recalculate_paper_balance(self) -> None:
        """Recalculate paper trading balance from trade history."""
        if not self._db:
            return

        # Start with initial balance
        base = config.PAPER_TRADING_INITIAL_BALANCE

        # Subtract cost basis of open positions
        open_exposure = await database.get_total_exposure(self._db)

        # Add realized P&L from closed positions
        rows = await self._db.execute_fetchall(
            "SELECT COALESCE(SUM(pnl), 0) as realized FROM positions WHERE status='closed'"
        )
        realized_pnl = float(rows[0]["realized"])

        self.balance = base - open_exposure + realized_pnl

    async def _fetch_balance_onchain(self) -> float:
        """
        Fetch USDC.e balance from Polygon via RPC.
        Rotates through a pool of free public endpoints when one is rate-limited.
        USDC.e has 6 decimals.
        """
        if not self._wallet_address:
            logger.warning("No wallet address configured, returning 0 balance")
            return 0.0

        addr_padded = self._wallet_address.replace("0x", "").lower().zfill(64)
        call_data = f"{config.BALANCE_OF_SELECTOR}{addr_padded}"
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": config.USDC_E_ADDRESS, "data": call_data}, "latest"],
            "id": 1,
        }

        # Try each RPC in the pool, starting from the last known-good index
        attempts = len(self._rpc_pool)
        for i in range(attempts):
            idx = (self._rpc_index + i) % attempts
            rpc_url = self._rpc_pool[idx]
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        rpc_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "result" in data and data["result"] and data["result"] != "0x":
                                raw = int(data["result"], 16)
                                balance = raw / 1e6
                                if idx != self._rpc_index:
                                    logger.info("RPC rotated to %s", rpc_url)
                                    self._rpc_index = idx
                                logger.debug("On-chain USDC.e balance: $%.2f via %s", balance, rpc_url)
                                return balance
                        elif resp.status in (429, 403):
                            logger.debug("RPC %s rate-limited (%d), trying next", rpc_url, resp.status)
                        else:
                            logger.debug("RPC %s returned %d", rpc_url, resp.status)
            except Exception as e:
                logger.debug("RPC %s failed: %s", rpc_url, e)

        logger.warning("All %d RPC endpoints failed for balance fetch", attempts)
        return 0.0

    async def _fetch_portfolio_value(self) -> float:
        """
        Fetch total value of open positions inside Polymarket via the positions API.
        This captures funds deployed into markets (not visible as raw USDC.e).
        """
        if not self._wallet_address:
            return 0.0

        addr = self._wallet_address.lower()
        url = f"https://data-api.polymarket.com/positions"
        params = {"user": addr, "sizeThreshold": "0.001", "limit": "200"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        total = sum(float(p.get("currentValue", 0)) for p in data)
                        logger.debug("Portfolio value from positions API: $%.2f (%d positions)", total, len(data))
                        return total
        except Exception as e:
            logger.debug("Portfolio value fetch failed: %s", e)
        return 0.0

    async def _fetch_balance_gamma(self) -> float:
        """
        Fallback: Fetch balance from Gamma API /profiles/{address}.
        NOTE: This returns 404 for most wallets — unreliable.
        """
        if not self._wallet_address:
            return 0.0

        url = f"{config.POLYMARKET_GAMMA_API}/profiles/{self._wallet_address}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        balance = float(data.get("collateral_balance", 0))
                        logger.debug("Gamma API balance: $%.2f", balance)
                        return balance
                    else:
                        logger.debug("Gamma API /profiles returned %d", resp.status)
        except Exception as e:
            logger.debug("Gamma API fallback failed: %s", e)

        return 0.0

    async def _fetch_balance_data_api(self) -> float:
        """
        Fallback: Fetch balance from data-api.polymarket.com.
        NOTE: Requires lowercase address — checksummed addresses return 'invalid'.
        """
        if not self._wallet_address:
            return 0.0

        addr = self._wallet_address.lower()
        url = f"https://data-api.polymarket.com/value?user={addr}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, (int, float)):
                            logger.debug("Data API balance: $%.2f", float(data))
                            return float(data)
                        elif isinstance(data, dict) and "value" in data:
                            logger.debug("Data API balance: $%.2f", float(data["value"]))
                            return float(data["value"])
        except Exception as e:
            logger.debug("Data API fallback failed: %s", e)

        return 0.0

    # ── Risk checks ─────────────────────────────────────────────────────────

    def available_balance(self) -> float:
        """Cash available for new trades (balance minus open exposure)."""
        return max(0.0, self.balance - self.total_exposure)

    def can_open_position(self, cost: float, strategy: str) -> tuple[bool, str]:
        """
        Check if a new position passes all risk rules.
        Returns (allowed, reason_if_denied).
        """
        # Check strategy-specific cap
        if strategy == "copy_trade":
            max_pct = config.COPY_TRADE_MAX_POSITION_PCT
        elif strategy == "volume_spike":
            max_pct = config.VOLUME_SPIKE_MAX_POSITION_PCT
        else:
            return False, f"Unknown strategy: {strategy}"

        max_cost = self.balance * max_pct
        if cost > max_cost:
            return False, (
                f"Position cost ${cost:.2f} exceeds {strategy} cap of "
                f"{max_pct*100:.0f}% (${max_cost:.2f})"
            )

        # Check total exposure cap
        new_exposure = self.total_exposure + cost
        max_exposure = self.balance * config.MAX_TOTAL_EXPOSURE_PCT
        if new_exposure > max_exposure:
            return False, (
                f"New total exposure ${new_exposure:.2f} would exceed "
                f"{config.MAX_TOTAL_EXPOSURE_PCT*100:.0f}% cap (${max_exposure:.2f})"
            )

        # Check available balance
        if cost > self.available_balance():
            return False, (
                f"Insufficient available balance: need ${cost:.2f}, "
                f"have ${self.available_balance():.2f}"
            )

        return True, "OK"

    def calculate_position_size(
        self, wallet_pct: float, strategy: str
    ) -> float:
        """
        Calculate position size in USDC.
        wallet_pct: the desired percentage of wallet (e.g., from copied wallet's allocation).
        Caps at strategy-specific maximum.
        """
        if strategy == "copy_trade":
            capped_pct = min(wallet_pct, config.COPY_TRADE_MAX_POSITION_PCT)
        elif strategy == "volume_spike":
            capped_pct = min(wallet_pct, config.VOLUME_SPIKE_MAX_POSITION_PCT)
        else:
            capped_pct = min(wallet_pct, 0.05)  # Conservative fallback

        return self.balance * capped_pct

    def get_position_for_market(self, market_id: str) -> dict[str, Any] | None:
        """Check if we already have an open position in a market."""
        for pos in self.positions:
            if pos["market_id"] == market_id and pos["status"] == "open":
                return pos
        return None

    async def refresh_loop(self) -> None:
        """Continuously refresh wallet state."""
        while self._running:
            try:
                await self.refresh()
            except Exception as e:
                logger.error("Wallet refresh error: %s", e)
            await asyncio.sleep(config.BALANCE_REFRESH_INTERVAL_SEC)
