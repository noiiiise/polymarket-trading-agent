export interface EngineStatus {
  state: "stopped" | "starting" | "running" | "error";
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

export interface Market {
  id: string;
  question: string;
  slug: string;
  image?: string;
  volume: number;
  volume24hr: number;
  liquidity: number;
  endDate: string;
  active: boolean;
  closed: boolean;
  outcomes: string[];
  outcomePrices: number[];
  bestBid: number;
  bestAsk: number;
  spread: number;
  tags?: string[];
}

export interface Position {
  id: string;
  marketId: string;
  market: string;
  outcome: string;
  side: "long" | "short";
  size: number;
  avgEntryPrice: number;
  currentPrice: number;
  pnl: number;
  pnlPct: number;
  value: number;
}

export interface TradeRecord {
  id: string;
  timestamp: string;
  marketId: string;
  market: string;
  side: "BUY" | "SELL";
  outcome: string;
  price: number;
  size: number;
  cost: number;
  strategy: "copy_trade" | "volume_spike" | "manual" | "mean_reversion" | "momentum";
  status: "filled" | "pending" | "cancelled" | "failed";
}

export interface LogEntry {
  id: string;
  timestamp: string;
  level: "info" | "warn" | "error" | "trade" | "debug";
  message: string;
  source?: string;
}

export interface TradeRequest {
  marketId: string;
  outcome: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
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
