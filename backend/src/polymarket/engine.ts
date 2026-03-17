// ─── Polymarket Trading Engine ──────────────────────────────────────────────
// Singleton engine that orchestrates copy-trade and volume-spike strategies,
// manages positions, tracks P&L, and logs all actions.

import {
  COPY_TRADE,
  VOLUME_SPIKE,
  RISK,
  PAPER_TRADING,
  ENGINE,
  GAMMA_API,
  EXECUTION,
} from "./config";
import * as polyClient from "./client";
import type { ClobSamplingMarket } from "./client";
import type { PolymarketMarket } from "../types";
import {
  loadTrades,
  loadPositions,
  loadOpenOrders,
  saveState,
} from "./persistence";
import type { OpenOrder } from "./persistence";

// ─── Types ──────────────────────────────────────────────────────────────────

export type EngineState = "stopped" | "starting" | "running" | "error";

export interface LogEntry {
  timestamp: string;
  level: "info" | "warn" | "error" | "trade";
  source: string;
  message: string;
  data?: unknown;
}

export interface PaperPosition {
  tokenId: string;
  marketQuestion: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  entryTime: string;
  currentPrice?: number;
  pnl?: number;
}

export interface TradeRecord {
  id: string;
  timestamp: string;
  tokenId: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  strategy: "copy_trade" | "volume_spike" | "manual";
  paper: boolean;
  result?: unknown;
}

export interface TradeSignal {
  id: string;
  timestamp: string;
  strategy: "copy_trade" | "volume_spike" | "manual_signal";
  market: string;
  marketId: string;
  action: "watching" | "considering" | "passed" | "executed";
  side?: "BUY" | "SELL";
  price?: number;
  reason: string;
  confidence?: number;
  data?: unknown;
}

export interface PendingOrder {
  id: string;
  timestamp: string;
  tokenId: string;
  marketQuestion: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  strategy: "copy_trade" | "volume_spike" | "manual";
  signedOrder: unknown;
  authHeaders: Record<string, string>;
  status: "pending" | "submitting" | "confirmed" | "rejected";
  error?: string;
  result?: unknown;
}

export interface EngineStatus {
  state: EngineState;
  paperTrading: boolean;
  clientStatus: string;
  balance: number;
  totalExposure: number;
  exposurePct: number;
  pnl: number;
  positionCount: number;
  openOrderCount: number;
  tradeCount: number;
  uptime: number;
  lastError?: string;
}

export interface EngineConfig {
  copyTrade: typeof COPY_TRADE;
  volumeSpike: typeof VOLUME_SPIKE;
  risk: typeof RISK;
  execution: typeof EXECUTION;
  paperTrading: typeof PAPER_TRADING;
}

// ─── Volume tracking for spike detection ────────────────────────────────────

interface VolumeBucket {
  marketId: string;
  question: string;
  volume: number;
  timestamp: number;
}

// ─── Engine Singleton ───────────────────────────────────────────────────────

class TradingEngine {
  private state: EngineState = "stopped";
  private logs: LogEntry[] = [];
  private positions: PaperPosition[] = [];
  private trades: TradeRecord[] = [];
  private balance: number = 0;
  private cachedBalance: number | null = null;
  private lastBalanceFetch: number = 0;
  private startTime: number = 0;
  private lastError?: string;

  // Interval handles
  private copyTradeInterval: ReturnType<typeof setInterval> | null = null;
  private volumeSpikeInterval: ReturnType<typeof setInterval> | null = null;
  private balanceRefreshInterval: ReturnType<typeof setInterval> | null = null;

  // Trade signals
  private signals: TradeSignal[] = [];

  // Pending orders for browser relay
  private pendingOrders: PendingOrder[] = [];

  // Open/unfilled orders on the CLOB
  private openOrders: OpenOrder[] = [];

  // Volume history for spike detection
  private volumeHistory: Map<string, VolumeBucket[]> = new Map();

  // Tracked wallets for copy trading (placeholder - would be populated from leaderboard)
  private trackedWallets: string[] = [];
  private leaderboardRefreshTime: number = 0;

  // ─── Logging ────────────────────────────────────────────────────────────

  private log(level: LogEntry["level"], source: string, message: string, data?: unknown): void {
    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      level,
      source,
      message,
      data,
    };
    this.logs.push(entry);
    if (this.logs.length > ENGINE.maxLogEntries) {
      this.logs = this.logs.slice(-ENGINE.maxLogEntries);
    }
    const prefix = `[Engine:${source}]`;
    if (level === "error") {
      console.error(prefix, message, data ?? "");
    } else {
      console.log(prefix, `[${level}]`, message, data ?? "");
    }
  }

  /** Persist trades, positions, and open orders to disk. */
  private persistState(): void {
    try {
      saveState(this.trades, this.positions, this.openOrders);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("error", "persistence", `Failed to save state: ${msg}`);
    }
  }

  // ─── Status ─────────────────────────────────────────────────────────────

  getStatus(): EngineStatus {
    const totalExposure = this.positions.reduce((sum, p) => sum + p.price * p.size, 0);
    const pnl = this.positions.reduce((sum, p) => sum + (p.pnl ?? 0), 0);
    const bal = this.cachedBalance ?? this.balance;
    const exposurePct = bal > 0 ? totalExposure / bal : 0;

    return {
      state: this.state,
      paperTrading: PAPER_TRADING.enabled,
      clientStatus: polyClient.getClientState().status,
      balance: bal,
      totalExposure,
      exposurePct,
      pnl,
      positionCount: this.positions.length,
      openOrderCount: this.openOrders.length,
      tradeCount: this.trades.length,
      uptime: this.state === "running" ? Date.now() - this.startTime : 0,
      lastError: this.lastError,
    };
  }

  /** Fetch real on-chain balance. Called by the status route. */
  async fetchLiveBalance(): Promise<void> {
    const now = Date.now();
    // Cache for 15 seconds to avoid spamming RPC on every poll
    if (this.cachedBalance !== null && now - this.lastBalanceFetch < 15_000) {
      return;
    }
    try {
      const bal = await polyClient.getBalance();
      // -1 means the fetch failed — keep existing cached balance
      if (bal < 0) {
        return;
      }
      this.cachedBalance = bal;
      this.lastBalanceFetch = now;
      // Also update the engine's working balance if not in paper mode
      if (!PAPER_TRADING.enabled) {
        this.balance = bal;
      }
    } catch {
      // Silently fail — status will use last cached value or engine balance
    }
  }

  getLogs(limit = 100): LogEntry[] {
    return this.logs.slice(-limit);
  }

  getPositions(): PaperPosition[] {
    return [...this.positions];
  }

  getTrades(limit = 50): TradeRecord[] {
    return this.trades.slice(-limit);
  }

  getOpenTrackedOrders(): OpenOrder[] {
    return [...this.openOrders];
  }

  getConfig(): EngineConfig {
    return {
      copyTrade: COPY_TRADE,
      volumeSpike: VOLUME_SPIKE,
      risk: RISK,
      execution: EXECUTION,
      paperTrading: PAPER_TRADING,
    };
  }

  // ─── Trade Signals ──────────────────────────────────────────────────────

  private addSignal(params: Omit<TradeSignal, "id" | "timestamp">): TradeSignal {
    const signal: TradeSignal = {
      id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      ...params,
    };
    this.signals.push(signal);
    if (this.signals.length > 200) {
      this.signals = this.signals.slice(-200);
    }
    return signal;
  }

  getSignals(limit = 50): TradeSignal[] {
    return this.signals.slice(-limit);
  }

  addManualSignal(market: string, note: string): TradeSignal {
    const signal: TradeSignal = {
      id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      strategy: "manual_signal",
      market,
      marketId: "",
      action: "watching",
      reason: note,
    };
    this.signals.push(signal);
    if (this.signals.length > 200) {
      this.signals = this.signals.slice(-200);
    }
    return signal;
  }

  // ─── Start / Stop ──────────────────────────────────────────────────────

  async start(): Promise<void> {
    if (this.state === "running") {
      this.log("warn", "engine", "Engine already running");
      return;
    }

    this.state = "starting";
    this.startTime = Date.now();
    this.log("info", "engine", "Starting trading engine...", {
      paperTrading: PAPER_TRADING.enabled,
    });

    // Load persisted state from disk
    try {
      const savedTrades = loadTrades();
      const savedPositions = loadPositions();
      const savedOpenOrders = loadOpenOrders();
      if (savedTrades.length > 0) {
        this.trades = savedTrades;
        this.log("info", "engine", `Loaded ${savedTrades.length} trades from disk`);
      }
      if (savedPositions.length > 0) {
        this.positions = savedPositions;
        this.log("info", "engine", `Loaded ${savedPositions.length} positions from disk`);
      }
      if (savedOpenOrders.length > 0) {
        this.openOrders = savedOpenOrders;
        this.log("info", "engine", `Loaded ${savedOpenOrders.length} open orders from disk`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("warn", "engine", `Failed to load persisted state: ${msg}`);
    }

    // Always try to fetch real wallet balance for dashboard visibility
    let realBalance = 0;
    try {
      const bal = await polyClient.getBalance();
      // -1 means fetch failed
      realBalance = bal >= 0 ? bal : 0;
      if (bal >= 0) {
        this.log("info", "engine", `Real wallet balance: $${realBalance.toFixed(2)}`);
      } else {
        this.log("warn", "engine", "Could not fetch real wallet balance from RPC");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("warn", "engine", `Could not fetch real wallet balance: ${msg}`);
    }

    // Set engine balance based on mode
    if (PAPER_TRADING.enabled) {
      this.balance = PAPER_TRADING.startingBalance;
      this.log("info", "engine", `Paper trading mode: using simulated balance $${this.balance} (real wallet: $${realBalance.toFixed(2)})`);
    } else {
      this.balance = realBalance;
      this.cachedBalance = realBalance;
      this.lastBalanceFetch = Date.now();
      this.log("info", "engine", `Live trading mode: balance $${this.balance.toFixed(2)}`);
    }

    // Try connecting the CLOB client (non-fatal if it fails)
    try {
      await polyClient.initClient();
      this.log("info", "engine", "CLOB client connected");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("warn", "engine", `CLOB client failed to connect (non-fatal): ${msg}`);
    }

    // Start strategy intervals
    this.copyTradeInterval = setInterval(
      () => this.runCopyTradeStrategy().catch((e) => this.handleStrategyError("copy_trade", e)),
      COPY_TRADE.pollIntervalMs
    );

    this.volumeSpikeInterval = setInterval(
      () => this.runVolumeSpikeStrategy().catch((e) => this.handleStrategyError("volume_spike", e)),
      VOLUME_SPIKE.checkIntervalMs
    );

    this.balanceRefreshInterval = setInterval(
      () => this.refreshBalance().catch((e) => this.log("error", "balance", String(e))),
      RISK.balanceRefreshMs
    );

    this.state = "running";
    this.log("info", "engine", "Trading engine started successfully");

    // Run initial cycle
    this.runCopyTradeStrategy().catch((e) => this.handleStrategyError("copy_trade", e));
    this.runVolumeSpikeStrategy().catch((e) => this.handleStrategyError("volume_spike", e));
  }

  stop(): void {
    if (this.state === "stopped") {
      this.log("warn", "engine", "Engine already stopped");
      return;
    }

    this.log("info", "engine", "Stopping trading engine...");

    if (this.copyTradeInterval) {
      clearInterval(this.copyTradeInterval);
      this.copyTradeInterval = null;
    }
    if (this.volumeSpikeInterval) {
      clearInterval(this.volumeSpikeInterval);
      this.volumeSpikeInterval = null;
    }
    if (this.balanceRefreshInterval) {
      clearInterval(this.balanceRefreshInterval);
      this.balanceRefreshInterval = null;
    }

    this.state = "stopped";
    this.log("info", "engine", "Trading engine stopped");
  }

  // ─── Manual Trade ──────────────────────────────────────────────────────

  async manualTrade(
    tokenId: string,
    side: "BUY" | "SELL",
    price: number,
    size: number
  ): Promise<TradeRecord> {
    this.log("info", "manual", `Manual ${side} order: ${size}@${price} for ${tokenId}`);

    // Risk check
    const status = this.getStatus();
    const orderValue = price * size;
    if (status.totalExposure + orderValue > this.balance * RISK.totalExposureCap) {
      throw new Error(
        `Order would exceed ${RISK.totalExposureCap * 100}% exposure cap. ` +
        `Current: $${status.totalExposure.toFixed(2)}, Order: $${orderValue.toFixed(2)}, ` +
        `Cap: $${(this.balance * RISK.totalExposureCap).toFixed(2)}`
      );
    }

    const trade: TradeRecord = {
      id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      tokenId,
      side,
      price,
      size,
      strategy: "manual",
      paper: PAPER_TRADING.enabled,
    };

    if (PAPER_TRADING.enabled) {
      // Simulate the trade
      this.positions.push({
        tokenId,
        marketQuestion: "Manual trade",
        side,
        price,
        size,
        entryTime: trade.timestamp,
      });
      if (side === "BUY") {
        this.balance -= orderValue;
      } else {
        this.balance += orderValue;
      }
      trade.result = { status: "paper_filled", orderId: trade.id };
      this.log("trade", "manual", `Paper trade filled: ${side} ${size}@${price}`, trade);
    } else {
      // LIVE: Place order via CLOB, then verify fill
      try {
        this.log("info", "manual", `Placing LIVE manual order: ${side} ${size}@${price}`);
        const result = await polyClient.placeOrder(tokenId, side, price, size);
        const resultObj = result as Record<string, unknown>;
        const orderId = (resultObj?.orderID ?? resultObj?.id ?? "") as string;

        this.log("info", "manual", `Order submitted, orderID=${orderId}`, result);

        // Wait 2 seconds then check if order was filled or still open
        const filled = await this.verifyOrderFill(orderId, tokenId);

        if (filled) {
          // Order was filled -- record position and deduct balance
          this.positions.push({
            tokenId,
            marketQuestion: "Manual trade",
            side,
            price,
            size,
            entryTime: trade.timestamp,
          });
          if (side === "BUY") {
            this.balance -= orderValue;
          } else {
            this.balance += orderValue;
          }
          trade.result = { status: "filled", orderId, result };
          this.log("trade", "manual", `Manual order FILLED: ${side} ${size}@${price}`, result);
        } else {
          // Order is still open/unfilled -- track it but do NOT record a position
          this.openOrders.push({
            orderId,
            tokenId,
            marketQuestion: "Manual trade",
            side,
            price,
            size,
            strategy: "manual",
            submittedAt: trade.timestamp,
            status: "submitted",
          });
          trade.result = { status: "submitted_unfilled", orderId, result };
          this.log("warn", "manual", `Manual order OPEN (unfilled): ${side} ${size}@${price} orderId=${orderId}`, result);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.log("error", "manual", `Trade failed: ${msg}`);
        throw err;
      }
    }

    this.trades.push(trade);
    this.persistState();
    return trade;
  }

  // ─── Pending Orders (Browser Relay) ───────────────────────────────────────

  getPendingOrders(): PendingOrder[] {
    return this.pendingOrders.filter(o => o.status === "pending");
  }

  getAllPendingOrders(): PendingOrder[] {
    return [...this.pendingOrders];
  }

  confirmOrder(id: string, result: unknown): void {
    const order = this.pendingOrders.find(o => o.id === id);
    if (!order) {
      this.log("warn", "relay", `confirmOrder: order ${id} not found`);
      return;
    }
    order.status = "confirmed";
    order.result = result;

    // NOW record the position (only after browser confirms CLOB accepted the order)
    this.positions.push({
      tokenId: order.tokenId,
      marketQuestion: order.marketQuestion,
      side: order.side,
      price: order.price,
      size: order.size,
      entryTime: order.timestamp,
    });
    this.balance -= order.price * order.size;

    this.log("trade", "relay", `Order CONFIRMED by browser relay: ${order.side} ${order.size.toFixed(2)}@${order.price} on "${order.marketQuestion.slice(0, 50)}"`, result);
    this.persistState();
  }

  rejectOrder(id: string, error: string): void {
    const order = this.pendingOrders.find(o => o.id === id);
    if (!order) {
      this.log("warn", "relay", `rejectOrder: order ${id} not found`);
      return;
    }
    order.status = "rejected";
    order.error = error;

    this.log("error", "relay", `Order REJECTED by browser relay: ${order.side} ${order.size.toFixed(2)}@${order.price} - ${error}`);
  }

  // ─── Strategy Trade Execution ─────────────────────────────────────────────

  private async executeStrategyTrade(
    tokenId: string,
    marketQuestion: string,
    side: "BUY" | "SELL",
    price: number,
    size: number,
    strategy: "copy_trade" | "volume_spike"
  ): Promise<void> {
    const orderValue = price * size;

    // Final risk check
    const status = this.getStatus();
    if (status.totalExposure + orderValue > this.balance * RISK.totalExposureCap) {
      this.log("warn", strategy, `Trade blocked: would exceed exposure cap`);
      return;
    }

    const trade: TradeRecord = {
      id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      tokenId,
      side,
      price,
      size,
      strategy,
      paper: PAPER_TRADING.enabled,
    };

    this.log("trade", strategy, `EXECUTING: ${side} ${size.toFixed(2)} @ $${price.toFixed(3)} = $${orderValue.toFixed(2)} on "${marketQuestion.slice(0, 50)}"`);

    if (PAPER_TRADING.enabled) {
      // Simulate the trade
      this.positions.push({
        tokenId,
        marketQuestion,
        side,
        price,
        size,
        entryTime: trade.timestamp,
      });
      this.balance -= orderValue;
      trade.result = { status: "paper_filled", orderId: trade.id };
      this.log("trade", strategy, `Paper trade filled: ${side} ${size.toFixed(2)}@${price}`, trade);
    } else {
      // LIVE: Place order via CLOB, then verify fill
      try {
        this.log("info", strategy, `Placing LIVE order: ${side} ${size.toFixed(2)}@${price}`);
        const result = await polyClient.placeOrder(tokenId, side, price, size);
        const resultObj = result as Record<string, unknown>;
        const orderId = (resultObj?.orderID ?? resultObj?.id ?? "") as string;

        this.log("info", strategy, `Order submitted, orderID=${orderId}`, result);

        // Wait 2 seconds then check if order was filled or still open
        const filled = await this.verifyOrderFill(orderId, tokenId);

        if (filled) {
          // Order was filled -- record position and deduct balance
          this.positions.push({
            tokenId,
            marketQuestion,
            side,
            price,
            size,
            entryTime: trade.timestamp,
          });
          this.balance -= orderValue;
          trade.result = { status: "filled", orderId, result };
          this.log("trade", strategy, `Order FILLED: ${side} ${size.toFixed(2)}@${price} on "${marketQuestion.slice(0, 50)}"`, result);
        } else {
          // Order is still open/unfilled -- track it but do NOT create a position
          this.openOrders.push({
            orderId,
            tokenId,
            marketQuestion,
            side,
            price,
            size,
            strategy,
            submittedAt: trade.timestamp,
            status: "submitted",
          });
          trade.result = { status: "submitted_unfilled", orderId, result };
          this.log("warn", strategy, `Order OPEN (unfilled): ${side} ${size.toFixed(2)}@${price} orderId=${orderId} on "${marketQuestion.slice(0, 50)}"`, result);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.log("error", strategy, `Failed to place order: ${msg}`);
        trade.result = { status: "failed", error: msg };
      }
    }

    this.trades.push(trade);
    this.persistState();
  }

  // ─── Fill Verification ─────────────────────────────────────────────────────

  /**
   * After placing an order, wait briefly then check if it was filled.
   * Returns true if order is filled (not in open orders), false if still open.
   */
  private async verifyOrderFill(orderId: string, _tokenId: string): Promise<boolean> {
    if (!orderId) {
      this.log("warn", "fill_check", "No orderId returned from CLOB, assuming unfilled");
      return false;
    }

    // Wait 2 seconds for the order to potentially match
    await new Promise((resolve) => setTimeout(resolve, 2000));

    try {
      // First try: check the specific order status via getOrder
      try {
        const order = await polyClient.getOrder(orderId);
        const orderObj = order as unknown as Record<string, unknown>;
        const status = (orderObj?.status ?? orderObj?.order_status ?? "") as string;
        const sizeMatched = Number(orderObj?.size_matched ?? orderObj?.matched_size ?? 0);
        const originalSize = Number(orderObj?.original_size ?? orderObj?.size ?? 0);

        this.log("info", "fill_check", `Order ${orderId} status=${status}, matched=${sizeMatched}/${originalSize}`, orderObj);

        // If status explicitly says matched/filled
        if (status === "MATCHED" || status === "FILLED" || status === "matched" || status === "filled") {
          return true;
        }

        // If all size has been matched
        if (originalSize > 0 && sizeMatched >= originalSize * 0.99) {
          return true;
        }

        // If status explicitly says live/open
        if (status === "LIVE" || status === "OPEN" || status === "live" || status === "open") {
          return false;
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.log("warn", "fill_check", `getOrder failed (will try getOpenOrders): ${msg}`);
      }

      // Second try: check open orders list -- if our order is NOT in the open orders, it was filled
      try {
        const openOrders = await polyClient.getOpenOrders();
        const openOrdersList = Array.isArray(openOrders) ? openOrders : [];

        const stillOpen = openOrdersList.some((o: unknown) => {
          const obj = o as Record<string, unknown>;
          const oid = (obj?.id ?? obj?.orderID ?? obj?.order_id ?? "") as string;
          return oid === orderId;
        });

        this.log("info", "fill_check", `Open orders check: ${openOrdersList.length} open orders, ours ${stillOpen ? "IS" : "NOT"} in list`);
        return !stillOpen;
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.log("warn", "fill_check", `getOpenOrders failed: ${msg}`);
      }

      // If both checks failed, assume unfilled to be safe (don't phantom-record a position)
      this.log("warn", "fill_check", `Could not verify fill for ${orderId}, assuming unfilled`);
      return false;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("error", "fill_check", `Fill verification error for ${orderId}: ${msg}`);
      return false;
    }
  }

  // ─── Strategy: Copy Trade ──────────────────────────────────────────────

  private async runCopyTradeStrategy(): Promise<void> {
    this.log("info", "copy_trade", "Running copy trade strategy cycle");

    // Refresh leaderboard if stale
    if (Date.now() - this.leaderboardRefreshTime > COPY_TRADE.leaderboardRefreshMs) {
      await this.refreshLeaderboard();
    }

    // Risk gate: check total exposure
    const status = this.getStatus();
    if (status.exposurePct >= RISK.totalExposureCap) {
      this.log("warn", "copy_trade", `Exposure at ${(status.exposurePct * 100).toFixed(1)}%, skipping cycle`);
      return;
    }

    // Use CLOB sampling-markets for real liquidity/prices
    try {
      const samplingMarkets = await polyClient.getSamplingMarkets();
      this.log("info", "copy_trade", `Got ${samplingMarkets.length} CLOB sampling markets`);

      // Filter for tradeable markets
      const tradeableMarkets: Array<{
        market: ClobSamplingMarket;
        tokenId: string;
        outcome: string;
        price: number;
      }> = [];

      for (const market of samplingMarkets) {
        if (!market.tokens || market.tokens.length < 2) continue;

        // Check resolution time (at least 1 hour out)
        if (market.end_date_iso) {
          const resolutionTime = new Date(market.end_date_iso).getTime() - Date.now();
          if (resolutionTime < COPY_TRADE.minResolutionMs) {
            continue; // Too close to resolution
          }
        }

        // Check each outcome for value
        // Focus on HIGHER probability markets (15-65%) which have actual liquidity
        for (const token of market.tokens) {
          if (token.winner) continue; // Skip already resolved

          const price = token.price;
          // Look for markets with decent probability - these have more liquidity
          // Sweet spot: 15% to 65% (not too unlikely, not too expensive)
          if (price >= 0.15 && price <= 0.65) {
            tradeableMarkets.push({
              market,
              tokenId: token.token_id,
              outcome: token.outcome,
              price,
            });
          }
        }
      }

      this.log("info", "copy_trade", `Found ${tradeableMarkets.length} tradeable opportunities (15-65% range)`);

      // Sort by how close to 50% (most liquid markets) - prioritize markets near 50%
      tradeableMarkets.sort((a, b) => Math.abs(a.price - 0.5) - Math.abs(b.price - 0.5));

      // Try to execute a trade - look for markets with actual liquidity
      for (const opp of tradeableMarkets.slice(0, 20)) {
        // Check if we already have a position in this market
        const existingPosition = this.positions.find(p => p.tokenId === opp.tokenId);
        if (existingPosition) {
          continue; // Skip - already in this market
        }

        // Verify orderbook has liquidity
        const ob = await polyClient.getOrderBook(opp.tokenId);
        const bestBid = ob.bids?.[0] ? Number(ob.bids[0].price) : 0;
        const bestAsk = ob.asks?.[0] ? Number(ob.asks[0].price) : 1;
        const spread = bestAsk - bestBid;
        const bidDepth = ob.bids?.reduce((sum, b) => sum + Number(b.size), 0) ?? 0;
        const askDepth = ob.asks?.reduce((sum, a) => sum + Number(a.size), 0) ?? 0;

        // AGGRESSIVE: Trade if we can buy at a reasonable price
        // For higher probability markets, we expect the ask to match embedded price more closely
        // Accept asks up to 70% (good for markets priced 15-65%)
        if (bestAsk >= 0.70 || bestAsk < 0.10) {
          this.addSignal({
            strategy: "copy_trade",
            market: opp.market.question,
            marketId: opp.market.condition_id,
            action: "passed",
            reason: `Ask price ${(bestAsk * 100).toFixed(0)}¢ not in target range (10-70¢)`,
            data: { tokenId: opp.tokenId, bestBid, bestAsk, spread, embeddedPrice: opp.price },
          });
          continue;
        }

        // Need at least SOME exit liquidity (bids) - relax this a bit
        if (bidDepth < 5) {
          this.addSignal({
            strategy: "copy_trade",
            market: opp.market.question,
            marketId: opp.market.condition_id,
            action: "passed",
            reason: `Low bid depth ($${bidDepth.toFixed(0)}), hard to exit`,
            data: { tokenId: opp.tokenId, bestBid, bestAsk, bidDepth, askDepth },
          });
          continue;
        }

        // Accept wider spreads for liquid markets (up to 30%)
        if (spread > 0.30) {
          this.addSignal({
            strategy: "copy_trade",
            market: opp.market.question,
            marketId: opp.market.condition_id,
            action: "passed",
            reason: `Spread too wide (${(spread * 100).toFixed(0)}%)`,
            data: { tokenId: opp.tokenId, bestBid, bestAsk, spread, bidDepth },
          });
          continue;
        }

        // Calculate trade size: 5% of balance per copy-trade bet (hard cap)
        const tradeSize = Math.min(this.balance * COPY_TRADE.maxPositionPct, this.balance * 0.05);
        if (tradeSize < 1) {
          this.log("warn", "copy_trade", `Balance too low for trade: $${this.balance.toFixed(2)}`);
          break; // Not enough balance
        }

        const buyPrice = bestAsk; // Use actual ask price from orderbook
        const size = tradeSize / buyPrice;

        this.addSignal({
          strategy: "copy_trade",
          market: opp.market.question,
          marketId: opp.market.condition_id,
          action: "executed",
          side: "BUY",
          price: buyPrice,
          reason: `${opp.outcome} at ${(buyPrice * 100).toFixed(0)}¢ (embedded: ${(opp.price * 100).toFixed(0)}¢), buying $${tradeSize.toFixed(2)}`,
          confidence: 1 - buyPrice,
          data: { tokenId: opp.tokenId, outcome: opp.outcome, buyPrice, size, spread, bidDepth, askDepth },
        });

        this.log("trade", "copy_trade", `EXECUTING TRADE: ${opp.outcome} on "${opp.market.question.slice(0, 50)}" @ ${(buyPrice * 100).toFixed(0)}¢`);

        await this.executeStrategyTrade(
          opp.tokenId,
          `${opp.market.question} (${opp.outcome})`,
          "BUY",
          buyPrice,
          size,
          "copy_trade"
        );
        return; // One trade per cycle to manage risk
      }

      this.log("info", "copy_trade", "No suitable trades found this cycle");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("error", "copy_trade", `Strategy cycle failed: ${msg}`);
    }
  }

  private async refreshLeaderboard(): Promise<void> {
    this.log("info", "copy_trade", "Refreshing leaderboard from Polymarket data API...");
    try {
      // Fetch top traders by monthly PnL from the real Polymarket leaderboard API
      const url = "https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=MONTH&orderBy=PNL&limit=20";
      const res = await fetch(url, {
        signal: AbortSignal.timeout(EXECUTION.timeoutMs),
      });

      if (res.ok) {
        const traders = (await res.json()) as Array<{
          rank: string;
          userName: string;
          proxyWallet: string;
          pnl: number;
          vol: number;
        }>;

        if (Array.isArray(traders) && traders.length > 0) {
          // Extract wallet addresses from top traders
          this.trackedWallets = traders
            .filter((t) => t.proxyWallet && t.pnl > 10000) // Only track profitable wallets
            .map((t) => t.proxyWallet);

          this.leaderboardRefreshTime = Date.now();

          // Log the top 5 for visibility
          const top5 = traders.slice(0, 5).map((t) => `${t.userName}: $${(t.pnl / 1000).toFixed(0)}k`).join(", ");
          this.log("info", "copy_trade", `Leaderboard refreshed: tracking ${this.trackedWallets.length} wallets. Top 5: ${top5}`);

          // Add a signal for each top trader we're now tracking
          traders.slice(0, 5).forEach((t) => {
            this.addSignal({
              strategy: "copy_trade",
              market: `Tracking: ${t.userName}`,
              marketId: t.proxyWallet,
              action: "watching",
              reason: `Monthly PnL: $${(t.pnl / 1000).toFixed(0)}k, Volume: $${(t.vol / 1e6).toFixed(1)}M`,
              confidence: Math.min(1, t.pnl / 1_000_000), // Higher PnL = higher confidence
              data: { userName: t.userName, pnl: t.pnl, vol: t.vol, wallet: t.proxyWallet },
            });
          });
        } else {
          this.log("warn", "copy_trade", "Leaderboard returned empty or invalid data");
        }
      } else {
        this.log("warn", "copy_trade", `Leaderboard API returned ${res.status}: ${res.statusText}`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("warn", "copy_trade", `Leaderboard refresh failed: ${msg}`);
    }
  }

  // ─── Strategy: Volume Spike ────────────────────────────────────────────

  private async runVolumeSpikeStrategy(): Promise<void> {
    this.log("info", "volume_spike", "Running volume spike strategy cycle");

    // Risk gate
    const status = this.getStatus();
    if (status.exposurePct >= RISK.totalExposureCap) {
      this.log("warn", "volume_spike", `Exposure at ${(status.exposurePct * 100).toFixed(1)}%, skipping cycle`);
      return;
    }

    try {
      const markets = await polyClient.getMarkets(50, 0);
      this.log("info", "volume_spike", `Scanning ${markets.length} markets for volume spikes`);

      for (const market of markets) {
        const currentVolume = Number(market.volumeNum ?? market.volume ?? 0);
        const marketId = market.id;

        // Get historical buckets for this market
        const buckets = this.volumeHistory.get(marketId) ?? [];

        // Record current volume bucket
        buckets.push({
          marketId,
          question: market.question,
          volume: currentVolume,
          timestamp: Date.now(),
        });

        // Keep only last 24h of buckets (2 buckets at 12h each)
        const cutoff = Date.now() - 2 * VOLUME_SPIKE.bucketDurationMs;
        const recentBuckets = buckets.filter((b) => b.timestamp > cutoff);
        this.volumeHistory.set(marketId, recentBuckets);

        if (recentBuckets.length < 2) continue;

        // Calculate rolling average (exclude current)
        const historical = recentBuckets.slice(0, -1);
        const avgVolume = historical.reduce((s, b) => s + b.volume, 0) / historical.length;

        if (avgVolume > 0 && currentVolume > avgVolume * VOLUME_SPIKE.spikeThreshold) {
          const ratio = (currentVolume / avgVolume).toFixed(2);
          this.addSignal({
            strategy: "volume_spike",
            market: market.question,
            marketId: market.id,
            action: "considering",
            reason: `Volume spike ${ratio}x above average ($${(currentVolume / 1e6).toFixed(1)}M vs $${(avgVolume / 1e6).toFixed(1)}M avg)`,
            confidence: Math.min(1, (currentVolume / avgVolume - 1) / 3),
            data: { currentVolume, avgVolume, ratio },
          });
          this.log("info", "volume_spike", `SPIKE detected: ${market.question}`, {
            marketId,
            currentVolume,
            avgVolume,
            ratio,
          });

          // In a real implementation, we would analyze the orderbook for
          // price wall indicators and execute trades.
          if (market.clobTokenIds && market.clobTokenIds.length > 0) {
            try {
              const tokenId = market.clobTokenIds[0]!;
              const ob = await polyClient.getOrderBook(tokenId);
              const bestBid = ob.bids?.[0] ? Number(ob.bids[0].price) : 0;
              const bestAsk = ob.asks?.[0] ? Number(ob.asks[0].price) : 1;
              const spread = bestAsk - bestBid;
              const bidDepth = ob.bids?.reduce((s, b) => s + Number(b.size), 0) ?? 0;

              this.log("info", "volume_spike", `Orderbook: bestBid=${bestBid} bestAsk=${bestAsk} spread=${(spread*100).toFixed(1)}% bidDepth=$${bidDepth.toFixed(0)}`);

              // Execute trade if orderbook is tradeable
              if (bestAsk >= 0.10 && bestAsk <= 0.65 && spread <= 0.15 && bidDepth >= 10) {
                // Check for existing position
                const existing = this.positions.find(p => p.tokenId === tokenId);
                if (existing) continue;

                // 15% of balance per volume-spike bet (hard cap)
                const tradeSize = Math.min(this.balance * VOLUME_SPIKE.maxPositionPct, this.balance * 0.15);
                if (tradeSize < 1) continue;

                const size = tradeSize / bestAsk;

                this.addSignal({
                  strategy: "volume_spike",
                  market: market.question,
                  marketId: market.id,
                  action: "executed",
                  side: "BUY",
                  price: bestAsk,
                  reason: `Volume spike ${ratio}x, buying YES at ${(bestAsk*100).toFixed(0)}¢ for $${tradeSize.toFixed(2)}`,
                  confidence: Math.min(1, (currentVolume / avgVolume - 1) / 3),
                  data: { tokenId, bestAsk, spread, bidDepth, tradeSize },
                });

                await this.executeStrategyTrade(tokenId, market.question, "BUY", bestAsk, size, "volume_spike");
                return; // One trade per cycle
              }
            } catch {
              // Skip if orderbook unavailable
            }
          }
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("error", "volume_spike", `Strategy cycle failed: ${msg}`);
    }
  }

  // ─── Balance Refresh ───────────────────────────────────────────────────

  private async refreshBalance(): Promise<void> {
    if (PAPER_TRADING.enabled) return;

    try {
      const newBalance = await polyClient.getBalance();
      // -1 means the fetch failed — keep existing balance
      if (newBalance < 0) {
        this.log("warn", "balance", "Balance fetch failed, keeping existing balance");
        return;
      }
      if (newBalance !== this.balance) {
        this.log("info", "balance", `Balance updated: $${this.balance.toFixed(2)} -> $${newBalance.toFixed(2)}`);
        this.balance = newBalance;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.log("warn", "balance", `Balance refresh failed: ${msg}`);
    }
  }

  // ─── Error Handling ────────────────────────────────────────────────────

  private handleStrategyError(strategy: string, err: unknown): void {
    const msg = err instanceof Error ? err.message : String(err);
    this.lastError = `[${strategy}] ${msg}`;
    this.log("error", strategy, `Unhandled error: ${msg}`);
  }
}

// ─── Export singleton ───────────────────────────────────────────────────────

export const engine = new TradingEngine();
