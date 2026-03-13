"""
Execution layer: WebSocket-based order placement against Polymarket's CLOB.
All orders are limit orders. Includes retry logic, spread checks, and paper trading simulation.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any

import aiohttp
import aiosqlite
import websockets

import config
import database

logger = logging.getLogger("execution")


class OrderExecutor:
    """
    Handles all order execution against Polymarket's CLOB API.
    - Places limit orders via REST (CLOB doesn't use WS for order placement).
    - Streams order book data via WebSocket for price discovery.
    - Implements retry logic and spread validation.
    - Paper trading mode simulates fills at requested price.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._session: aiohttp.ClientSession | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]
        self._order_book_cache: dict[str, dict[str, Any]] = {}
        self._running = False

    async def start(self) -> None:
        """Initialize HTTP session and connect to WebSocket."""
        self._session = aiohttp.ClientSession(
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._running = True
        logger.info("Order executor started (paper=%s)", config.PAPER_TRADING)

    async def stop(self) -> None:
        """Clean shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("Order executor stopped")

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers for Polymarket CLOB API."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if config.POLYMARKET_API_KEY:
            headers["Authorization"] = f"Bearer {config.POLYMARKET_API_KEY}"
        return headers

    # ── Order Book ──────────────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """
        Fetch current order book for a token (outcome) from CLOB REST API.
        Returns: {bids: [{price, size}], asks: [{price, size}], spread, mid_price, best_bid, best_ask}
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
            logger.error("Order book error for %s: %s", token_id, e)
            return self._empty_order_book()

    def _parse_order_book(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse raw CLOB order book response."""
        bids = [
            {"price": float(b.get("price", 0)), "size": float(b.get("size", 0))}
            for b in data.get("bids", [])
        ]
        asks = [
            {"price": float(a.get("price", 0)), "size": float(a.get("size", 0))}
            for a in data.get("asks", [])
        ]

        # Sort: bids descending, asks ascending
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 1.0
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.5

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid_price": mid_price,
        }

    def _simulated_order_book(self, token_id: str) -> dict[str, Any]:
        """Generate a realistic simulated order book for paper trading."""
        # Use token_id hash for deterministic but varied prices
        h = int(hashlib.md5(token_id.encode()).hexdigest()[:8], 16)
        base_price = 0.30 + (h % 40) / 100  # 0.30 to 0.70 range

        spread = 0.01
        best_bid = round(base_price - spread / 2, 4)
        best_ask = round(base_price + spread / 2, 4)

        bids = [
            {"price": round(best_bid - i * 0.005, 4), "size": 100 + i * 50}
            for i in range(5)
        ]
        asks = [
            {"price": round(best_ask + i * 0.005, 4), "size": 100 + i * 50}
            for i in range(5)
        ]

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid_price": round((best_bid + best_ask) / 2, 4),
        }

    def _empty_order_book(self) -> dict[str, Any]:
        return {
            "bids": [], "asks": [],
            "best_bid": 0.0, "best_ask": 1.0,
            "spread": 1.0, "mid_price": 0.5,
        }

    # ── Price Calculation ───────────────────────────────────────────────────

    def calculate_limit_price(
        self, order_book: dict[str, Any], side: str
    ) -> float:
        """
        Determine limit price based on spread rules:
        - If spread <= 2%: use best price (best_ask for buy, best_bid for sell).
        - If spread > 2%: use mid-price.
        """
        spread_pct = order_book["spread"]

        if spread_pct <= config.MAX_SPREAD_FOR_BEST_PRICE_PCT:
            if side == "BUY":
                return order_book["best_ask"]
            else:
                return order_book["best_bid"]
        else:
            return order_book["mid_price"]

    # ── Order Placement ─────────────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        market_id: str,
        side: str,
        price: float,
        size: float,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Place a limit order. Returns execution result.
        Retries once on failure, then logs and skips.
        """
        # Log order attempt
        order_id = await database.insert_order(
            self._db, position_id, market_id,
            "YES",  # Will be refined when we know the outcome
            side, price, size,
        )

        for attempt in range(config.ORDER_MAX_RETRIES + 1):
            try:
                if config.PAPER_TRADING:
                    result = await self._simulate_fill(
                        order_id, token_id, side, price, size
                    )
                else:
                    result = await self._execute_live_order(
                        order_id, token_id, side, price, size
                    )

                if result["status"] == "filled":
                    await database.update_order_status(
                        self._db, order_id, "filled",
                        polymarket_order_id=result.get("order_id"),
                    )
                    logger.info(
                        "Order filled: %s %s %.4f @ $%.4f (order_id=%s)",
                        side, token_id[:12], size, price, result.get("order_id", "paper"),
                    )
                    return result

                # If not filled but no error, treat as pending/cancelled
                await database.update_order_status(
                    self._db, order_id, result.get("status", "cancelled"),
                )
                return result

            except Exception as e:
                logger.warning(
                    "Order attempt %d failed for %s: %s",
                    attempt + 1, token_id[:12], e,
                )
                if attempt < config.ORDER_MAX_RETRIES:
                    await asyncio.sleep(config.ORDER_RETRY_DELAY_SEC)
                else:
                    await database.update_order_status(
                        self._db, order_id, "failed",
                        error_message=str(e),
                    )
                    logger.error(
                        "Order permanently failed after %d attempts: %s",
                        attempt + 1, e,
                    )
                    return {"status": "failed", "error": str(e)}

        return {"status": "failed", "error": "Max retries exceeded"}

    async def _simulate_fill(
        self,
        order_id: int,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict[str, Any]:
        """Simulate an immediate fill for paper trading."""
        logger.info(
            "[PAPER] Simulated fill: %s %.2f units @ $%.4f (token=%s)",
            side, size, price, token_id[:16],
        )
        return {
            "status": "filled",
            "order_id": f"paper-{order_id}",
            "price": price,
            "size": size,
            "filled_at": datetime.utcnow().isoformat(),
        }

    async def _execute_live_order(
        self,
        order_id: int,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict[str, Any]:
        """
        Place a real limit order via Polymarket CLOB REST API.
        Uses API key auth + signed order payload.
        """
        assert self._session is not None

        # Build order payload per CLOB API spec
        order_payload = {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side.upper(),
            "type": "GTC",  # Good-til-cancelled
        }

        # Sign the order with private key
        if config.POLYMARKET_PRIVATE_KEY:
            order_payload["signature"] = self._sign_order(order_payload)

        url = f"{config.POLYMARKET_REST_BASE}/order"

        async with self._session.post(url, json=order_payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "status": "filled" if data.get("success") else "failed",
                    "order_id": data.get("orderID", ""),
                    "price": price,
                    "size": size,
                }
            else:
                body = await resp.text()
                raise RuntimeError(
                    f"CLOB order failed ({resp.status}): {body}"
                )

    def _sign_order(self, payload: dict[str, Any]) -> str:
        """
        Sign order payload with private key.
        In production, this uses EIP-712 typed data signing.
        Placeholder: actual signing requires py_clob_client or web3.py.
        """
        # This is a placeholder — real signing uses the py-clob-client library
        # or manual EIP-712 signing with the private key.
        # The actual implementation would be:
        #   from py_clob_client.clob_types import OrderArgs
        #   from py_clob_client.order_builder import OrderBuilder
        #   builder = OrderBuilder(private_key, chain_id=137)
        #   signed = builder.create_order(args)
        logger.debug("Order signing placeholder — use py_clob_client for production")
        return "0x_placeholder_signature"

    # ── Market Data Helpers ─────────────────────────────────────────────────

    async def get_market_info(self, market_id: str) -> dict[str, Any] | None:
        """Fetch market details from Polymarket Gamma API."""
        if config.PAPER_TRADING:
            return self._simulated_market(market_id)

        url = f"{config.POLYMARKET_GAMMA_API}/markets/{market_id}"
        try:
            assert self._session is not None
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error("Failed to fetch market %s: %s", market_id, e)
            return None

    async def get_active_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch list of active markets from Gamma API."""
        if config.PAPER_TRADING:
            return self._simulated_active_markets(limit)

        url = f"{config.POLYMARKET_GAMMA_API}/markets"
        params = {"limit": limit, "active": "true", "closed": "false"}
        try:
            assert self._session is not None
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()  # type: ignore[no-any-return]
                return []
        except Exception as e:
            logger.error("Failed to fetch active markets: %s", e)
            return []

    def _simulated_market(self, market_id: str) -> dict[str, Any]:
        """Generate a simulated market for paper trading."""
        return {
            "id": market_id,
            "question": f"Simulated Market {market_id[:8]}",
            "slug": f"simulated-{market_id[:8]}",
            "active": True,
            "closed": False,
            "end_date_iso": "2025-12-31T00:00:00Z",
            "tokens": [
                {"token_id": f"{market_id}-yes", "outcome": "Yes"},
                {"token_id": f"{market_id}-no", "outcome": "No"},
            ],
            "volume": 50000,
            "volume_num": 50000,
        }

    def _simulated_active_markets(self, limit: int) -> list[dict[str, Any]]:
        """Generate simulated active markets for paper trading."""
        markets = []
        categories = [
            "politics", "crypto", "sports", "tech", "economy",
            "science", "entertainment", "world", "finance", "climate",
        ]
        for i in range(min(limit, 20)):
            mid = f"sim-market-{i:04d}"
            markets.append({
                "id": mid,
                "condition_id": mid,
                "question": f"Will {categories[i % len(categories)]} event {i} happen?",
                "slug": f"sim-{categories[i % len(categories)]}-{i}",
                "active": True,
                "closed": False,
                "end_date_iso": "2025-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": f"{mid}-yes", "outcome": "Yes"},
                    {"token_id": f"{mid}-no", "outcome": "No"},
                ],
                "volume": 10000 + i * 5000,
                "volume_num": 10000 + i * 5000,
            })
        return markets

    # ── WebSocket Order Book Stream ─────────────────────────────────────────

    async def stream_order_books(
        self, token_ids: list[str], callback: Any
    ) -> None:
        """
        Connect to Polymarket WebSocket and stream order book updates.
        Calls callback(token_id, order_book_update) on each message.
        """
        if config.PAPER_TRADING:
            logger.info("[PAPER] WebSocket streaming simulated — using polling")
            return

        while self._running:
            try:
                async with websockets.connect(config.POLYMARKET_WS_URL) as ws:  # type: ignore[attr-defined]
                    self._ws = ws

                    # Subscribe to order book channels
                    sub_msg = {
                        "type": "subscribe",
                        "channel": "book",
                        "assets_ids": token_ids,
                    }
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
