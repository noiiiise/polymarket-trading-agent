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
    - Fetches live USDC balance from Polygon (or paper balance).
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
            self.balance = await self._fetch_onchain_balance()
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

    async def _fetch_onchain_balance(self) -> float:
        """Fetch USDC balance from Polygon via RPC."""
        if not config.POLYMARKET_WALLET_ADDRESS:
            logger.warning("No wallet address configured, returning 0 balance")
            return 0.0

        # USDC on Polygon: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

        # ERC-20 balanceOf(address) selector
        call_data = (
            "0x70a08231"
            + config.POLYMARKET_WALLET_ADDRESS.lower().replace("0x", "").zfill(64)
        )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": usdc_contract, "data": call_data},
                "latest",
            ],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.POLYGON_RPC_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                    hex_balance = result.get("result", "0x0")
                    # USDC has 6 decimals
                    raw = int(hex_balance, 16)
                    return raw / 1_000_000
        except Exception as e:
            logger.error("Failed to fetch on-chain balance: %s", e)
            return self.balance  # Return last known balance on error

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
