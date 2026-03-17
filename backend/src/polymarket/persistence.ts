// ─── Simple JSON File Persistence for Trades & Positions ────────────────────
// Reads/writes to backend/data/trades.json so trade history survives restarts.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join, dirname } from "path";
import type { TradeRecord, PaperPosition } from "./engine";

// ─── Types ──────────────────────────────────────────────────────────────────

interface OpenOrder {
  orderId: string;
  tokenId: string;
  marketQuestion: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  strategy: "copy_trade" | "volume_spike" | "manual";
  submittedAt: string;
  status: "submitted" | "partially_filled" | "expired";
}

interface PersistedData {
  trades: TradeRecord[];
  positions: PaperPosition[];
  openOrders: OpenOrder[];
  lastSaved: string;
}

export type { OpenOrder };

// ─── File path ──────────────────────────────────────────────────────────────

const DATA_DIR = join(import.meta.dir, "..", "..", "data");
const DATA_FILE = join(DATA_DIR, "trades.json");

function ensureDataDir(): void {
  if (!existsSync(DATA_DIR)) {
    mkdirSync(DATA_DIR, { recursive: true });
  }
}

// ─── Read ───────────────────────────────────────────────────────────────────

function loadAll(): PersistedData {
  try {
    if (!existsSync(DATA_FILE)) {
      return { trades: [], positions: [], openOrders: [], lastSaved: "" };
    }
    const raw = readFileSync(DATA_FILE, "utf-8");
    const parsed = JSON.parse(raw) as Partial<PersistedData>;
    return {
      trades: Array.isArray(parsed.trades) ? parsed.trades : [],
      positions: Array.isArray(parsed.positions) ? parsed.positions : [],
      openOrders: Array.isArray(parsed.openOrders) ? parsed.openOrders : [],
      lastSaved: typeof parsed.lastSaved === "string" ? parsed.lastSaved : "",
    };
  } catch (err) {
    console.error("[Persistence] Failed to load data file:", err instanceof Error ? err.message : err);
    return { trades: [], positions: [], openOrders: [], lastSaved: "" };
  }
}

function saveAll(data: PersistedData): void {
  try {
    ensureDataDir();
    data.lastSaved = new Date().toISOString();
    writeFileSync(DATA_FILE, JSON.stringify(data, null, 2), "utf-8");
  } catch (err) {
    console.error("[Persistence] Failed to save data file:", err instanceof Error ? err.message : err);
  }
}

// ─── Public API ─────────────────────────────────────────────────────────────

export function loadTrades(): TradeRecord[] {
  return loadAll().trades;
}

export function loadPositions(): PaperPosition[] {
  return loadAll().positions;
}

export function loadOpenOrders(): OpenOrder[] {
  return loadAll().openOrders;
}

export function saveTrades(trades: TradeRecord[]): void {
  const data = loadAll();
  data.trades = trades;
  saveAll(data);
}

export function savePositions(positions: PaperPosition[]): void {
  const data = loadAll();
  data.positions = positions;
  saveAll(data);
}

export function saveOpenOrders(openOrders: OpenOrder[]): void {
  const data = loadAll();
  data.openOrders = openOrders;
  saveAll(data);
}

/** Save trades, positions, and open orders atomically in one write. */
export function saveState(trades: TradeRecord[], positions: PaperPosition[], openOrders: OpenOrder[]): void {
  const data: PersistedData = {
    trades,
    positions,
    openOrders,
    lastSaved: new Date().toISOString(),
  };
  try {
    ensureDataDir();
    writeFileSync(DATA_FILE, JSON.stringify(data, null, 2), "utf-8");
  } catch (err) {
    console.error("[Persistence] Failed to save state:", err instanceof Error ? err.message : err);
  }
}
