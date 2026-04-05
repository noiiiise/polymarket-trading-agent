"""
Redemption manager: auto-redeems winning positions on Polymarket's CTF contract.

How it works:
1. Every 15 minutes, checks open positions for markets that have resolved.
2. Calls redeemPositions() on the CTF contract via web3 (direct on-chain tx).
3. Marks positions as closed in the DB with correct exit price (1.0 = won, 0.0 = lost).

On-chain notes:
- CTF contract: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
- redeemPositions burns ALL tokens for the condition — no amount parameter.
- If tokens are in a Gnosis Safe proxy wallet (not the EOA), the tx will revert.
  In that case, a clear warning is logged and redemption falls back to manual via UI.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
import aiosqlite
from web3 import Web3

import config
import database

logger = logging.getLogger("redemption")

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
REDEEM_CHECK_INTERVAL_SEC = 900  # 15 minutes

REDEEM_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class RedemptionManager:
    """
    Polls for resolved markets and auto-redeems winning tokens on-chain.
    Closes positions in the DB with the correct P&L regardless of whether
    the on-chain redemption succeeds (so accounting is always accurate).
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._session: aiohttp.ClientSession | None = None
        self._w3: Web3 | None = None
        self._ctf: Any | None = None
        self._account: Any | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        if not config.PAPER_TRADING:
            self._setup_web3()
        logger.info("Redemption manager started (check interval=%ds)", REDEEM_CHECK_INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()
        logger.info("Redemption manager stopped")

    def _setup_web3(self) -> None:
        """Connect to Polygon RPC and initialise the CTF contract handle."""
        self._w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
        self._account = self._w3.eth.account.from_key(config.POLYMARKET_PRIVATE_KEY)
        self._ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=REDEEM_ABI,
        )
        logger.info(
            "Web3 initialised for redemption | account=%s... | connected=%s",
            self._account.address[:12], self._w3.is_connected(),
        )

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        while self._running:
            try:
                await self._check_and_redeem()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Redemption loop error: %s", e)

            try:
                await asyncio.sleep(REDEEM_CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                break

    async def _check_and_redeem(self) -> None:
        """Scan open positions for resolved markets and redeem them."""
        open_positions = await database.get_open_positions(self._db)
        if not open_positions:
            return

        market_ids = list({p["market_id"] for p in open_positions})
        logger.debug("Checking %d unique market(s) for resolution", len(market_ids))

        for market_id in market_ids:
            try:
                resolved, winning_outcome = await self._check_market_resolved(market_id)
                if not resolved:
                    continue

                positions_in_market = [p for p in open_positions if p["market_id"] == market_id]
                logger.info(
                    "Market resolved | id=%s | winner=%s | our positions=%d",
                    market_id[:20], winning_outcome, len(positions_in_market),
                )

                # Attempt on-chain redemption (logs a warning if proxy wallet blocks it).
                # Only close positions in DB once we've confirmed the redemption was
                # submitted (or we're in paper mode). If the tx reverts or fails, keep
                # positions open so we don't lose track of tokens that weren't redeemed.
                redeemed_ok = True
                if not config.PAPER_TRADING:
                    redeemed_ok = await self._redeem_on_chain(market_id)

                if redeemed_ok:
                    for pos in positions_in_market:
                        won = (pos["outcome"].upper() == (winning_outcome or "").upper())
                        exit_price = 1.0 if won else 0.0
                        pnl = await database.close_position(self._db, pos["id"], exit_price)
                        logger.info(
                            "%s | market=%s | outcome=%s | entry=$%.4f | size=%.2f | P&L=$%.2f",
                            "WON ✓" if won else "LOST ✗",
                            market_id[:16], pos["outcome"],
                            pos["entry_price"], pos["size"], pnl,
                        )
                else:
                    logger.warning(
                        "Skipping DB close for market=%s — on-chain redemption did not "
                        "succeed. Redeem manually at polymarket.com then the next "
                        "resolution check will retry.",
                        market_id[:16],
                    )

            except Exception as e:
                logger.error("Error processing market %s: %s", market_id[:16], e)

    # ── Market resolution check ─────────────────────────────────────────────

    async def _check_market_resolved(
        self, market_id: str
    ) -> tuple[bool, str | None]:
        """
        Query Gamma API to check if a market has resolved.
        Returns (resolved, winning_outcome_string).
        """
        if not self._session:
            return False, None

        if market_id.startswith("0x"):
            url = f"{config.POLYMARKET_GAMMA_API}/markets"
            params: dict[str, Any] = {"condition_id": market_id}
        else:
            url = f"{config.POLYMARKET_GAMMA_API}/markets/{market_id}"
            params = {}

        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return False, None
                data = await resp.json()
                market = data[0] if isinstance(data, list) and data else data
                if not isinstance(market, dict):
                    return False, None

                if not (market.get("resolved") or market.get("closed")):
                    return False, None

                # Find the winning token outcome
                winning_outcome: str | None = None
                for token in market.get("tokens", []):
                    if token.get("winner"):
                        winning_outcome = token.get("outcome", "YES").upper()
                        break

                return True, winning_outcome

        except Exception as e:
            logger.debug("Resolution check failed for %s: %s", market_id[:16], e)
            return False, None

    # ── On-chain redemption ─────────────────────────────────────────────────

    async def _redeem_on_chain(self, market_id: str) -> bool:
        """
        Call CTF.redeemPositions() for the given market on Polygon.

        Sends the transaction signed by the private key EOA.

        IMPORTANT: If your Polymarket account uses a proxy/Safe wallet
        (POLYMARKET_WALLET_ADDRESS != EOA address), the EOA doesn't hold
        the tokens directly and this tx will revert. In that case a
        warning is logged — redeem manually via polymarket.com until
        Safe-based redemption is supported.
        """
        if not self._w3 or not self._ctf or not self._account:
            logger.warning("Web3 not initialised, skipping on-chain redemption for %s", market_id[:16])
            return False

        # Convert condition ID to bytes32
        try:
            if market_id.startswith("0x"):
                condition_bytes = bytes.fromhex(market_id[2:].zfill(64))
            else:
                raw = market_id.encode()
                condition_bytes = raw.rjust(32, b"\x00")
        except Exception as e:
            logger.error("Invalid condition ID %s: %s", market_id, e)
            return False

        loop = asyncio.get_event_loop()

        def _build_and_send() -> tuple[str, int]:
            assert self._w3 is not None
            assert self._ctf is not None
            assert self._account is not None

            nonce = self._w3.eth.get_transaction_count(self._account.address)
            gas_price = int(self._w3.eth.gas_price * 1.1)  # 10% tip for faster inclusion

            txn = self._ctf.functions.redeemPositions(
                Web3.to_checksum_address(config.USDC_E_ADDRESS),
                bytes(32),       # parentCollectionId — always zero for Polymarket
                condition_bytes,
                [1, 2],          # index sets — redeem both outcomes (only winner pays)
            ).build_transaction({
                "from": self._account.address,
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": 250_000,
                "chainId": config.CHAIN_ID,
            })

            signed = self._account.sign_transaction(txn)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash.hex(), receipt["status"]

        try:
            tx_hash, status = await loop.run_in_executor(None, _build_and_send)
            if status == 1:
                logger.info("Redemption confirmed | market=%s | tx=%s", market_id[:16], tx_hash)
                return True
            else:
                logger.error(
                    "Redemption tx reverted | market=%s | tx=%s\n"
                    "If tokens are in a Safe proxy wallet, redeem at polymarket.com",
                    market_id[:16], tx_hash,
                )
                return False

        except Exception as e:
            logger.error(
                "On-chain redemption failed | market=%s | error=%s\n"
                "Tip: if POLYMARKET_WALLET_ADDRESS is a Safe proxy wallet, "
                "tokens are not held by the EOA. Redeem manually at polymarket.com",
                market_id[:16], e,
            )
            return False
