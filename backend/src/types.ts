import { z } from "zod";

// ─── Polymarket Trade Request ───────────────────────────────────────────────

export const TradeRequestSchema = z.object({
  tokenId: z.string().min(1, "tokenId is required"),
  side: z.enum(["BUY", "SELL"]),
  price: z.number().min(0.01).max(0.99),
  size: z.number().positive("size must be positive"),
});

export type TradeRequest = z.infer<typeof TradeRequestSchema>;

// ─── Polymarket Market (from Gamma API) ─────────────────────────────────────

export const MarketSchema = z.object({
  id: z.string(),
  question: z.string(),
  conditionId: z.string().optional(),
  slug: z.string().optional(),
  description: z.string().optional(),
  outcomes: z.array(z.string()).optional(),
  outcomePrices: z.array(z.string()).optional(),
  volume: z.string().optional(),
  active: z.boolean().optional(),
  closed: z.boolean().optional(),
  image: z.string().optional(),
  icon: z.string().optional(),
  startDate: z.string().optional(),
  endDate: z.string().optional(),
  clobTokenIds: z.array(z.string()).optional(),
  liquidityNum: z.number().optional(),
  volumeNum: z.number().optional(),
});

export type PolymarketMarket = z.infer<typeof MarketSchema> & { [key: string]: unknown };

// ─── Polymarket OrderBook ───────────────────────────────────────────────────

export const OrderBookEntrySchema = z.object({
  price: z.string(),
  size: z.string(),
});

export const OrderBookSchema = z.object({
  market: z.string().optional(),
  asset_id: z.string().optional(),
  hash: z.string().optional(),
  timestamp: z.string().optional(),
  bids: z.array(OrderBookEntrySchema),
  asks: z.array(OrderBookEntrySchema),
});

export interface OrderBookEntry {
  price: string;
  size: string;
}

export interface PolymarketOrderBook {
  market: string;
  asset_id: string;
  hash: string;
  timestamp: string;
  bids: OrderBookEntry[];
  asks: OrderBookEntry[];
}

// ─── Engine Status ──────────────────────────────────────────────────────────

export const EngineStatusSchema = z.object({
  state: z.enum(["stopped", "starting", "running", "error"]),
  paperTrading: z.boolean(),
  clientStatus: z.string(),
  balance: z.number(),
  totalExposure: z.number(),
  exposurePct: z.number(),
  pnl: z.number(),
  positionCount: z.number(),
  openOrderCount: z.number(),
  tradeCount: z.number(),
  uptime: z.number(),
  lastError: z.string().optional(),
});

export type EngineStatus = z.infer<typeof EngineStatusSchema>;

// ─── Log Entry ──────────────────────────────────────────────────────────────

export const LogEntrySchema = z.object({
  timestamp: z.string(),
  level: z.enum(["info", "warn", "error", "trade"]),
  source: z.string(),
  message: z.string(),
  data: z.unknown().optional(),
});

export type LogEntry = z.infer<typeof LogEntrySchema>;

// ─── Position ───────────────────────────────────────────────────────────────

export const PositionSchema = z.object({
  tokenId: z.string(),
  marketQuestion: z.string(),
  side: z.enum(["BUY", "SELL"]),
  price: z.number(),
  size: z.number(),
  entryTime: z.string(),
  currentPrice: z.number().optional(),
  pnl: z.number().optional(),
});

export type Position = z.infer<typeof PositionSchema>;

// ─── Order ──────────────────────────────────────────────────────────────────

export const OrderSchema = z.object({
  id: z.string(),
  market: z.string().optional(),
  asset_id: z.string().optional(),
  side: z.string(),
  price: z.string(),
  original_size: z.string().optional(),
  size_matched: z.string().optional(),
  status: z.string().optional(),
  created_at: z.string().optional(),
});

export type Order = z.infer<typeof OrderSchema>;

// ─── Trade Record ───────────────────────────────────────────────────────────

export const TradeRecordSchema = z.object({
  id: z.string(),
  timestamp: z.string(),
  tokenId: z.string(),
  side: z.enum(["BUY", "SELL"]),
  price: z.number(),
  size: z.number(),
  strategy: z.enum(["copy_trade", "volume_spike", "manual"]),
  paper: z.boolean(),
  result: z.unknown().optional(),
});

export type TradeRecord = z.infer<typeof TradeRecordSchema>;

// ─── Query Schemas ──────────────────────────────────────────────────────────

export const MarketsQuerySchema = z.object({
  query: z.string().optional(),
  limit: z.coerce.number().int().positive().default(20),
  offset: z.coerce.number().int().min(0).default(0),
  active: z.coerce.boolean().optional().default(true),
});

export type MarketsQuery = z.infer<typeof MarketsQuerySchema>;

export const PriceSideSchema = z.object({
  side: z.enum(["BUY", "SELL"]).default("BUY"),
});

export const LogsQuerySchema = z.object({
  limit: z.coerce.number().int().positive().default(100),
});

// ─── Trade Signal ──────────────────────────────────────────────────────────

export const TradeSignalSchema = z.object({
  id: z.string(),
  timestamp: z.string(),
  strategy: z.enum(["copy_trade", "volume_spike", "manual_signal"]),
  market: z.string(),
  marketId: z.string(),
  action: z.enum(["watching", "considering", "passed", "executed"]),
  side: z.enum(["BUY", "SELL"]).optional(),
  price: z.number().optional(),
  reason: z.string(),
  confidence: z.number().optional(),
  data: z.unknown().optional(),
});

export type TradeSignal = z.infer<typeof TradeSignalSchema>;

export const ManualSignalSchema = z.object({
  market: z.string().min(1),
  note: z.string().min(1),
});

// ─── Pending Order (Browser Relay) ──────────────────────────────────────────

export const PendingOrderSchema = z.object({
  id: z.string(),
  timestamp: z.string(),
  tokenId: z.string(),
  marketQuestion: z.string(),
  side: z.enum(["BUY", "SELL"]),
  price: z.number(),
  size: z.number(),
  strategy: z.enum(["copy_trade", "volume_spike", "manual"]),
  signedOrder: z.unknown(),
  authHeaders: z.record(z.string(), z.string()),
  status: z.enum(["pending", "submitting", "confirmed", "rejected"]),
  error: z.string().optional(),
  result: z.unknown().optional(),
});

export type PendingOrder = z.infer<typeof PendingOrderSchema>;

export const ConfirmOrderSchema = z.object({
  id: z.string().min(1),
  result: z.unknown(),
});

export const RejectOrderSchema = z.object({
  id: z.string().min(1),
  error: z.string().min(1),
});
