// ─── Polymarket API Routes ──────────────────────────────────────────────────

import { Hono } from "hono";
import { zValidator } from "@hono/zod-validator";
import { engine } from "../polymarket/engine";
import * as polyClient from "../polymarket/client";
import {
  TradeRequestSchema,
  MarketsQuerySchema,
  PriceSideSchema,
  LogsQuerySchema,
  ManualSignalSchema,
  ConfirmOrderSchema,
  RejectOrderSchema,
} from "../types";

const polymarketRouter = new Hono();

// ─── Engine Control ─────────────────────────────────────────────────────────

/** GET /status - Engine status (running, balance, exposure, P&L) */
polymarketRouter.get("/status", async (c) => {
  await engine.fetchLiveBalance();
  return c.json({ data: engine.getStatus() });
});

/** POST /start - Start the trading engine */
polymarketRouter.post("/start", async (c) => {
  try {
    await engine.start();
    return c.json({ data: engine.getStatus() });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to start engine";
    return c.json({ error: { message, code: "ENGINE_START_ERROR" } }, 500);
  }
});

/** POST /stop - Stop the trading engine */
polymarketRouter.post("/stop", (c) => {
  engine.stop();
  return c.json({ data: engine.getStatus() });
});

/** GET /config - Get current configuration / thresholds */
polymarketRouter.get("/config", (c) => {
  return c.json({ data: engine.getConfig() });
});

/** GET /logs - Get recent engine logs */
polymarketRouter.get(
  "/logs",
  zValidator("query", LogsQuerySchema),
  (c) => {
    const { limit } = c.req.valid("query");
    return c.json({ data: engine.getLogs(limit) });
  }
);

// ─── Market Data ────────────────────────────────────────────────────────────

/** GET /markets - Browse active markets (proxy to Gamma API) */
polymarketRouter.get(
  "/markets",
  zValidator("query", MarketsQuerySchema),
  async (c) => {
    try {
      const { query, limit, offset, active } = c.req.valid("query");
      const markets = await polyClient.getMarkets(limit, offset, query, active);
      return c.json({ data: markets });
    } catch (err) {
      console.error("[Polymarket] Error fetching markets:", err);
      const message = err instanceof Error ? err.message : "Failed to fetch markets";
      return c.json({ error: { message, code: "GAMMA_API_ERROR" } }, 502);
    }
  }
);

/** GET /market/:id - Get single market details */
polymarketRouter.get("/market/:id", async (c) => {
  try {
    const id = c.req.param("id");
    const market = await polyClient.getMarket(id);
    return c.json({ data: market });
  } catch (err) {
    console.error("[Polymarket] Error fetching market:", err);
    const message = err instanceof Error ? err.message : "Market not found";
    return c.json({ error: { message, code: "MARKET_NOT_FOUND" } }, 404);
  }
});

/** GET /orderbook/:tokenId - Get orderbook for a token */
polymarketRouter.get("/orderbook/:tokenId", async (c) => {
  try {
    const tokenId = c.req.param("tokenId");
    const orderbook = await polyClient.getOrderBook(tokenId);
    return c.json({ data: orderbook });
  } catch (err) {
    console.error("[Polymarket] Error fetching orderbook:", err);
    const message = err instanceof Error ? err.message : "Failed to fetch orderbook";
    return c.json({ error: { message, code: "CLOB_ERROR" } }, 500);
  }
});

// ─── Positions & Orders ─────────────────────────────────────────────────────

/** GET /positions - Get current positions */
polymarketRouter.get("/positions", (c) => {
  // Engine-tracked positions (works in paper mode too)
  const enginePositions = engine.getPositions();
  return c.json({ data: enginePositions });
});

/** GET /orders - Get open orders */
polymarketRouter.get("/orders", async (c) => {
  try {
    const orders = await polyClient.getOpenOrders();
    return c.json({ data: orders });
  } catch (err) {
    console.error("[Polymarket] Error fetching orders:", err);
    const message = err instanceof Error ? err.message : "Failed to fetch orders";
    return c.json({ error: { message, code: "CLOB_ERROR" } }, 500);
  }
});

/** GET /trades - Get trade history from engine */
polymarketRouter.get("/trades", (c) => {
  return c.json({ data: engine.getTrades() });
});

// ─── Trading ────────────────────────────────────────────────────────────────

/** POST /trade - Manual trade placement */
polymarketRouter.post(
  "/trade",
  zValidator("json", TradeRequestSchema),
  async (c) => {
    try {
      const { tokenId, side, price, size } = c.req.valid("json");
      const trade = await engine.manualTrade(tokenId, side, price, size);
      return c.json({ data: trade });
    } catch (err) {
      console.error("[Polymarket] Error placing trade:", err);
      const message = err instanceof Error ? err.message : "Failed to place trade";
      return c.json({ error: { message, code: "TRADE_ERROR" } }, 500);
    }
  }
);

/** POST /cancel/:orderId - Cancel specific order */
polymarketRouter.post("/cancel/:orderId", async (c) => {
  try {
    const orderId = c.req.param("orderId");
    const result = await polyClient.cancelOrder(orderId);
    return c.json({ data: { success: true, result } });
  } catch (err) {
    console.error("[Polymarket] Error cancelling order:", err);
    const message = err instanceof Error ? err.message : "Failed to cancel order";
    return c.json({ error: { message, code: "CANCEL_ERROR" } }, 500);
  }
});

/** POST /cancel-all - Cancel all orders */
polymarketRouter.post("/cancel-all", async (c) => {
  try {
    const result = await polyClient.cancelAllOrders();
    return c.json({ data: { success: true, result } });
  } catch (err) {
    console.error("[Polymarket] Error cancelling all orders:", err);
    const message = err instanceof Error ? err.message : "Failed to cancel all orders";
    return c.json({ error: { message, code: "CANCEL_ERROR" } }, 500);
  }
});

// ─── Price Data ─────────────────────────────────────────────────────────────

/** GET /price/:tokenId - Get current price for a token */
polymarketRouter.get(
  "/price/:tokenId",
  zValidator("query", PriceSideSchema),
  async (c) => {
    try {
      const tokenId = c.req.param("tokenId");
      const { side } = c.req.valid("query");
      const price = await polyClient.getPrice(tokenId, side);
      return c.json({ data: { price } });
    } catch (err) {
      console.error("[Polymarket] Error fetching price:", err);
      const message = err instanceof Error ? err.message : "Failed to fetch price";
      return c.json({ error: { message, code: "CLOB_ERROR" } }, 500);
    }
  }
);

// ─── Trade Signals ─────────────────────────────────────────────────────────

/** GET /signals - Recent trade signals/considerations */
polymarketRouter.get("/signals", (c) => {
  const limit = Number(c.req.query("limit") ?? 50);
  return c.json({ data: engine.getSignals(limit) });
});

/** POST /signals - Add a manual signal (from X/Twitter, etc.) */
polymarketRouter.post(
  "/signals",
  zValidator("json", ManualSignalSchema),
  (c) => {
    const { market, note } = c.req.valid("json");
    const signal = engine.addManualSignal(market, note);
    return c.json({ data: signal });
  }
);

// ─── Browser Order Relay ─────────────────────────────────────────────────────

/** GET /pending-orders - Get pending signed orders for browser to submit */
polymarketRouter.get("/pending-orders", (c) => {
  const pending = engine.getPendingOrders();
  return c.json({ data: pending });
});

/** POST /confirm-order - Frontend reports successful order submission */
polymarketRouter.post(
  "/confirm-order",
  zValidator("json", ConfirmOrderSchema),
  (c) => {
    const { id, result } = c.req.valid("json");
    engine.confirmOrder(id, result);
    return c.json({ data: { success: true } });
  }
);

/** POST /reject-order - Frontend reports failed order submission */
polymarketRouter.post(
  "/reject-order",
  zValidator("json", RejectOrderSchema),
  (c) => {
    const { id, error } = c.req.valid("json");
    engine.rejectOrder(id, error);
    return c.json({ data: { success: true } });
  }
);

export { polymarketRouter };
