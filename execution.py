"""
Execution layer: real order placement against Polymarket's CLOB via py-clob-client.
All orders are limit orders (GTC). Includes retry logic and spread checks.
"""

import asyncio
import functools
import json
import logging
import time
from datetime import datetime
from typing import Any

import aiohttp
import aiosqlite
import websockets

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.builder import BUY, SELL
from py_clob_client.constants import POLYGON

import config
import database

logger = logging.getLogger("execution")

# Emit a one-time log at import time so Railway logs show exactly what
# constants the running process has loaded.
logger.info("py-clob-client constants loaded: BUY=%r  SELL=%r", BUY, SELL)


class OrderExecutor:
    """
    Handles all order execution against Polymarket's CLOB API.
    - Places limit orders via py-clob-client (handles EIP-712 signing internally).
    - Streams order book data via WebSocket for price discovery.
    - Implements retry logic and spread validation.
    - Paper trading mode simulates fills without touching live API.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._session: aiohttp.ClientSession | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]
        self._order_book_cache: dict[str, dict[str, Any]] = {}
        self._clob_client: ClobClient | None = None
        self._running = False

    async def start(self) -> None:
        """Initialize HTTP session and CLOB client for live order execution."""
        self._session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._running = True

        if not config.PAPER_TRADING:
            await self._init_clob_client()

        # Log startup diagnostics so we can verify which code Railway is running.
        try:
            import subprocess, sys
            git_hash = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            git_hash = "unknown"

        try:
            import importlib.metadata
            clob_ver = importlib.metadata.version("py-clob-client")
        except Exception:
            clob_ver = "unknown"

        logger.info(
            "Order executor started | paper=%s | git=%s | py-clob-client=%s | BUY=%r SELL=%r",
            config.PAPER_TRADING, git_hash, clob_ver, BUY, SELL,
        )

    async def _init_clob_client(self) -> None:
        """
        Initialize the py-clob-client with real credentials.
        Uses explicit creds from env if provided; otherwise derives them
        from the private key via EIP-712 signature (same result as Polymarket UI).
        """
        loop = asyncio.get_event_loop()

        # Always derive fresh credentials from the private key.
        # Explicit env-var creds (SECRET/PASSPHRASE) can go stale;
        # derivation is deterministic and always produces valid creds.
        logger.info("Deriving API credentials from private key...")
        tmp = ClobClient(
            host=config.POLYMARKET_REST_BASE,
            chain_id=POLYGON,
            key=config.POLYMARKET_PRIVATE_KEY,
        )
        try:
            creds = await loop.run_in_executor(
                None, tmp.create_or_derive_api_creds
            )
        except AttributeError:
            creds = await loop.run_in_executor(None, tmp.derive_api_key)
        logger.info(
            "API credentials derived (key=%s...)", creds.api_key[:8] if creds else "?"
        )

        # funder = the Polymarket proxy wallet address (differs from signing key).
        # signature_type=1 (POLY_PROXY) tells the order builder that the maker
        # is a proxy wallet controlled by a different EOA signer.
        POLY_PROXY = 1
        funder = config.POLYMARKET_WALLET_ADDRESS or None
        try:
            self._clob_client = ClobClient(
                host=config.POLYMARKET_REST_BASE,
                chain_id=POLYGON,
                key=config.POLYMARKET_PRIVATE_KEY,
                creds=creds,
                signature_type=POLY_PROXY,
                funder=funder,
            )
        except TypeError:
            self._clob_client = ClobClient(
                host=config.POLYMARKET_REST_BASE,
                chain_id=POLYGON,
                key=config.POLYMARKET_PRIVATE_KEY,
                creds=creds,
            )
        logger.info(
            "CLOB client initialized (wallet=%s... sig_type=%s)",
            (funder or "")[:12], POLY_PROXY,
        )

    async def stop(self) -> None:
        """Clean shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("Order executor stopped")

    # ── Order Book ──────────────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """
        Fetch current order book for a token from CLOB REST API.
        Returns: {bids, asks, spread, mid_price, best_bid, best_ask}
        """
        if config.PAPER_TRADING:
            return self._simulated_order_book(token_id)

        url = f"{config.POLYMARKET_REST_BASE}/book"
        params = {"token_id": token_id}

        try:
            assert self._session is not None
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error("Order book fetch failed: %d", resp.status)
                    return self._empty_order_book()
                data = await resp.json()
                return self._parse_order_book(data)
        except Exception as e:
            logger.error("Order book error for %s: %s", token_id[:16], e)
            return self._empty_order_book()

    def _parse_order_book(self, data: dict[str, Any]) -> dict[str, Any]:
        bids = [
            {"price": float(b.get("price", 0)), "size": float(b.get("size", 0))}
            for b in data.get("bids", [])
        ]
        asks = [
            {"price": float(a.get("price", 0)), "size": float(a.get("size", 0))}
            for a in data.get("asks", [])
        ]
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 1.0
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.5

        return {
            "bids": bids, "asks": asks,
            "best_bid": best_bid, "best_ask": best_ask,
            "spread": spread, "mid_price": mid_price,
        }

    def _simulated_order_book(self, token_id: str) -> dict[str, Any]:
        import hashlib
        h = int(hashlib.md5(token_id.encode()).hexdigest()[:8], 16)
        base_price = 0.30 + (h % 40) / 100
        spread = 0.01
        best_bid = round(base_price - spread / 2, 4)
        best_ask = round(base_price + spread / 2, 4)
        bids = [{"price": round(best_bid - i * 0.005, 4), "size": 100 + i * 50} for i in range(5)]
        asks = [{"price": round(best_ask + i * 0.005, 4), "size": 100 + i * 50} for i in range(5)]
        return {
            "bids": bids, "asks": asks,
            "best_bid": best_bid, "best_ask": best_ask,
            "spread": spread, "mid_price": round((best_bid + best_ask) / 2, 4),
        }

    def _empty_order_book(self) -> dict[str, Any]:
        return {"bids": [], "asks": [], "best_bid": 0.0, "best_ask": 1.0,
                "spread": 1.0, "mid_price": 0.5}

    # ── Price Calculation ───────────────────────────────────────────────────

    def calculate_limit_price(self, order_book: dict[str, Any], side: str) -> float:
        """
        Best price if spread <= 2%, mid-price otherwise.
        """
        if order_book["spread"] <= config.MAX_SPREAD_FOR_BEST_PRICE_PCT:
            return order_book["best_ask"] if side == "BUY" else order_book["best_bid"]
        return order_book["mid_price"]

    # ── Order Placement ─────────────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        market_id: str,
        outcome: str,
        side: str,
        price: float,
        size: float,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Place a limit order. Returns execution result dict with 'status' key.
        Retries once on transient failure.
        """
        order_id = await database.insert_order(
            self._db, position_id, market_id, outcome, side, price, size,
        )

        for attempt in range(config.ORDER_MAX_RETRIES + 1):
            try:
                if config.PAPER_TRADING:
                    result = await self._simulate_fill(order_id, token_id, side, price, size)
                else:
                    result = await self._execute_live_order(order_id, token_id, side, price, size)

                final_status = result.get("status", "failed")
                await database.update_order_status(
                    self._db, order_id, final_status,
                    polymarket_order_id=result.get("order_id"),
                )

                if final_status in ("filled", "pending"):
                    logger.info(
                        "Order %s: %s %s %.4f @ $%.4f (polymarket_id=%s)",
                        final_status, side, token_id[:16], size, price,
                        result.get("order_id", "paper"),
                    )
                return result

            except Exception as e:
                logger.warning("Order attempt %d failed for %s: %s", attempt + 1, token_id[:16], e)
                if attempt < config.ORDER_MAX_RETRIES:
                    await asyncio.sleep(config.ORDER_RETRY_DELAY_SEC)
                else:
                    await database.update_order_status(
                        self._db, order_id, "failed", error_message=str(e)
                    )
                    logger.error("Order permanently failed after %d attempts: %s", attempt + 1, e)
                    return {"status": "failed", "error": str(e)}

        return {"status": "failed", "error": "max retries exceeded"}

    async def _simulate_fill(
        self, order_id: int, token_id: str, side: str, price: float, size: float
    ) -> dict[str, Any]:
        logger.info(
            "[PAPER] Simulated fill: %s %.2f @ $%.4f (token=%s)",
            side, size, price, token_id[:16],
        )
        return {
            "status": "filled",
            "order_id": f"paper-{order_id}",
            "price": price,
            "size": size,
        }

    async def _execute_live_order(
        self, order_id: int, token_id: str, side: str, price: float, size: float
    ) -> dict[str, Any]:
        """
        Place a real limit order via py-clob-client.
        Uses ClobClient.create_and_post_order which handles tick-size,
        neg-risk, fee-rate resolution and EIP-712 signing internally.
        """
        assert self._clob_client is not None, "CLOB client not initialized"
        loop = asyncio.get_event_loop()

        tick = 0.01
        price_rounded = round(round(price / tick) * tick, 4)
        size_rounded = round(size, 4)

        if price_rounded <= 0 or price_rounded >= 1:
            raise ValueError(f"Price {price_rounded} out of valid range (0, 1)")
        if size_rounded < 5:
            raise ValueError(f"Size {size_rounded} below CLOB minimum of 5 shares")

        side_val = BUY if side == "BUY" else SELL
        logger.info(
            "Placing order: token=%s side=%r price=%.4f size=%.4f",
            token_id[:16], side_val, price_rounded, size_rounded,
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=price_rounded,
            size=size_rounded,
            side=side_val,
        )

        # create_and_post_order handles tick-size, neg-risk, fee-rate, signing,
        # and posting in one call. Run in executor to keep async loop free.
        resp = await loop.run_in_executor(
            None,
            functools.partial(self._clob_client.create_and_post_order, order_args),
        )

        if resp.get("errorMsg"):
            raise RuntimeError(f"CLOB rejected order: {resp['errorMsg']}")

        order_status = resp.get("status", "")
        success = resp.get("success") is True or order_status in ("matched", "delayed")

        return {
            "status": "filled" if success else "failed",
            "order_id": resp.get("orderID", ""),
            "price": price_rounded,
            "size": size_rounded,
            "clob_status": order_status,
        }

    # ── Market Data Helpers ─────────────────────────────────────────────────

    async def get_market_info(self, market_id: str) -> dict[str, Any] | None:
        """
        Fetch market details from Polymarket Gamma API.
        Handles both hex condition IDs (0x...) and Gamma integer IDs.
        """
        if config.PAPER_TRADING:
            return self._simulated_market(market_id)

        # Condition IDs (from CLOB / positions API) start with 0x
        # Gamma API supports querying by condition_id as a query param
        if market_id.startswith("0x"):
            url = f"{config.POLYMARKET_GAMMA_API}/markets"
            params: dict[str, Any] = {"condition_id": market_id}
        else:
            url = f"{config.POLYMARKET_GAMMA_API}/markets/{market_id}"
            params = {}

        try:
            assert self._session is not None
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    if isinstance(data, dict):
                        return data
                else:
                    logger.debug("Market info %d for %s", resp.status, market_id[:16])
                return None
        except Exception as e:
            logger.error("Market info fetch error for %s: %s", market_id[:16], e)
            return None

    async def get_active_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch active markets from Gamma API sorted by volume."""
        if config.PAPER_TRADING:
            return self._simulated_active_markets(limit)

        url = f"{config.POLYMARKET_GAMMA_API}/markets"
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": "volume",
            "ascending": "false",
        }
        try:
            assert self._session is not None
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()  # type: ignore[no-any-return]
                logger.error("Active markets fetch failed: %d", resp.status)
                return []
        except Exception as e:
            logger.error("Active markets fetch error: %s", e)
            return []

    def _simulated_market(self, market_id: str) -> dict[str, Any]:
        return {
            "id": market_id,
            "question": f"Simulated Market {market_id[:8]}",
            "slug": f"simulated-{market_id[:8]}",
            "active": True, "closed": False,
            "end_date_iso": "2027-12-31T00:00:00Z",
            "tokens": [
                {"token_id": f"{market_id}-yes", "outcome": "Yes"},
                {"token_id": f"{market_id}-no", "outcome": "No"},
            ],
            "volume": 50000,
        }

    def _simulated_active_markets(self, limit: int) -> list[dict[str, Any]]:
        categories = ["politics", "crypto", "sports", "tech", "economy",
                      "science", "entertainment", "world", "finance", "climate"]
        markets = []
        for i in range(min(limit, 20)):
            mid = f"sim-market-{i:04d}"
            markets.append({
                "id": mid, "conditionId": mid,
                "question": f"Will {categories[i % len(categories)]} event {i} happen?",
                "slug": f"sim-{categories[i % len(categories)]}-{i}",
                "active": True, "closed": False,
                "end_date_iso": "2027-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": f"{mid}-yes", "outcome": "Yes"},
                    {"token_id": f"{mid}-no", "outcome": "No"},
                ],
                "volume": 10000 + i * 5000,
            })
        return markets

    # ── WebSocket Order Book Stream ─────────────────────────────────────────

    async def stream_order_books(self, token_ids: list[str], callback: Any) -> None:
        """Connect to Polymarket WebSocket and stream order book updates."""
        if config.PAPER_TRADING:
            logger.info("[PAPER] WebSocket streaming skipped in paper mode")
            return

        while self._running:
            try:
                async with websockets.connect(config.POLYMARKET_WS_URL) as ws:  # type: ignore[attr-defined]
                    self._ws = ws
                    sub_msg = {"type": "subscribe", "channel": "book", "assets_ids": token_ids}
                    await ws.send(json.dumps(sub_msg))
                    logger.info("WebSocket subscribed to %d tokens", len(token_ids))

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            if data.get("event_type") == "book":
                                asset_id = data.get("asset_id", "")
                                parsed = self._parse_order_book(data)
                                self._order_book_cache[asset_id] = parsed
                                await callback(asset_id, parsed)
                        except json.JSONDecodeError:
                            continue

            except websockets.exceptions.ConnectionClosed:  # type: ignore[attr-defined]
                logger.warning("WebSocket disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("WebSocket error: %s, reconnecting in 10s...", e)
                await asyncio.sleep(10)
