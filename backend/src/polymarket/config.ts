// ─── Polymarket Trading Engine Configuration ────────────────────────────────
// Ported from https://github.com/noiiiise/polymarket-trading-agent config.py

// ─── API Endpoints ──────────────────────────────────────────────────────────

export const CLOB_HOST = "https://clob.polymarket.com";
export const CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
export const GAMMA_API = "https://gamma-api.polymarket.com";
export const POLYGON_RPC = "https://polygon-rpc.com";
export const CHAIN_ID = 137; // Polygon mainnet

// Cloudflare Worker relay to bypass CLOB geoblock (reads from .env at import time)
export const CLOB_PROXY_URL = process.env.CLOB_PROXY_URL || readEnvFile("CLOB_PROXY_URL") || "";

/** Read a value from the .env file directly (fallback when hot-reload skips .env) */
function readEnvFile(key: string): string {
  try {
    const envPath = new URL("../../.env", import.meta.url).pathname;
    const content = require("fs").readFileSync(envPath, "utf-8") as string;
    const match = content.match(new RegExp(`^${key}=(.+)$`, "m"));
    return match?.[1]?.trim() ?? "";
  } catch {
    return "";
  }
}

// ─── Copy Trade Strategy ────────────────────────────────────────────────────

export const COPY_TRADE = {
  /** Maximum position size as fraction of wallet balance */
  maxPositionPct: 0.05,
  /** Polling interval in milliseconds (30 seconds for aggressive trading) */
  pollIntervalMs: 30_000,
  /** Number of top wallets to track */
  topWallets: 20,
  /** Minimum trades in 30 days for a wallet to qualify */
  minWalletTrades30d: 10,
  /** Minimum time to market resolution in ms (1 hour) */
  minResolutionMs: 60 * 60 * 1000,
  /** Maximum spread allowed to enter a position (15% for more opportunities) */
  maxSpreadPct: 0.15,
  /** Leaderboard refresh interval in ms (6 hours) */
  leaderboardRefreshMs: 6 * 60 * 60 * 1000,
} as const;

// ─── Volume Spike Strategy ──────────────────────────────────────────────────

export const VOLUME_SPIKE = {
  /** Maximum position size as fraction of wallet balance */
  maxPositionPct: 0.15,
  /** Spike threshold: volume must exceed Nx rolling average */
  spikeThreshold: 2.0,
  /** Tracking bucket duration in ms (12 hours) */
  bucketDurationMs: 12 * 60 * 60 * 1000,
  /** Check interval in ms (12 hours) */
  checkIntervalMs: 12 * 60 * 60 * 1000,
  /** Price wall indicator: 50%+ volume concentrated at 1-2 levels */
  priceWallPct: 0.50,
} as const;

// ─── Risk Management ────────────────────────────────────────────────────────

export const RISK = {
  /** Maximum total exposure as fraction of wallet balance */
  totalExposureCap: 0.40,
  /** Balance refresh interval in ms (30 seconds) */
  balanceRefreshMs: 30_000,
} as const;

// ─── Order Execution ────────────────────────────────────────────────────────

export const EXECUTION = {
  /** Best price threshold: maximum acceptable spread */
  bestPriceSpreadPct: 0.02,
  /** Retry delay in ms */
  retryDelayMs: 5_000,
  /** Maximum retries per order */
  maxRetries: 1,
  /** Order timeout in ms */
  timeoutMs: 30_000,
} as const;

// ─── Paper Trading ──────────────────────────────────────────────────────────

export const PAPER_TRADING = {
  /** Whether paper trading mode is enabled (default true for safety) */
  enabled: false,
  /** Starting simulated balance in USDC */
  startingBalance: 10_000,
} as const;

// ─── Engine Limits ──────────────────────────────────────────────────────────

export const ENGINE = {
  /** Maximum log entries kept in memory */
  maxLogEntries: 1_000,
} as const;
