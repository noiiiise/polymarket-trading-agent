// ─── Polymarket Client Wrapper ──────────────────────────────────────────────
// Wraps @polymarket/clob-client with Gamma API calls and error handling.

import { ClobClient, Side, OrderType } from "@polymarket/clob-client";
import { Wallet } from "@ethersproject/wallet";
import { CLOB_HOST, CHAIN_ID, GAMMA_API, EXECUTION, CLOB_PROXY_URL } from "./config";
import type { PolymarketMarket, PolymarketOrderBook } from "../types";

export type ClientStatus = "disconnected" | "connecting" | "connected" | "error";

export interface ClientState {
  status: ClientStatus;
  error?: string;
  walletAddress?: string;
}

let clobClient: ClobClient | null = null;
let clientState: ClientState = { status: "disconnected" };
let initPromise: Promise<ClobClient> | null = null;

// ─── Initialization ─────────────────────────────────────────────────────────

export function getClientState(): ClientState {
  return { ...clientState };
}

export async function initClient(): Promise<ClobClient> {
  if (clobClient) return clobClient;
  if (initPromise) return initPromise;

  initPromise = (async () => {
    clientState = { status: "connecting" };
    console.log("[PolyClient] Initializing ClobClient...");

    try {
      const privateKey = process.env.POLYMARKET_PRIVATE_KEY!;
      const walletAddress = process.env.POLYMARKET_WALLET_ADDRESS!;

      const signer = new Wallet(privateKey);
      console.log("[PolyClient] Signer address:", signer.address);

      // Derive API credentials
      const tempClient = new ClobClient(CLOB_HOST, CHAIN_ID, signer);
      const creds = await tempClient.createOrDeriveApiKey();
      console.log("[PolyClient] API creds derived successfully");

      // Full client with GNOSIS_SAFE signature type (2) and funder address
      const client = new ClobClient(
        CLOB_HOST,
        CHAIN_ID,
        signer,
        creds,
        2, // GNOSIS_SAFE
        walletAddress
      );

      clobClient = client;
      clientState = { status: "connected", walletAddress };
      console.log("[PolyClient] ClobClient ready");
      return client;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      clientState = { status: "error", error: message };
      console.error("[PolyClient] Init failed:", message);
      initPromise = null;
      throw err;
    }
  })();

  return initPromise;
}

/** Get the client, initializing if needed. Throws on failure. */
export async function getClient(): Promise<ClobClient> {
  return initClient();
}

/** Reset the client so it will re-initialize on next use. */
export function resetClient(): void {
  clobClient = null;
  initPromise = null;
  clientState = { status: "disconnected" };
}

// ─── Gamma API helpers (not covered by CLOB client) ─────────────────────────

export async function getMarkets(
  limit = 20,
  offset = 0,
  query?: string,
  active = true
): Promise<PolymarketMarket[]> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    active: String(active),
    closed: "false",
  });
  if (query) params.set("query", query);

  const url = `${GAMMA_API}/markets?${params}`;
  console.log("[PolyClient] Fetching markets:", url);

  const res = await fetch(url, { signal: AbortSignal.timeout(EXECUTION.timeoutMs) });
  if (!res.ok) {
    throw new Error(`Gamma API error: ${res.status} ${res.statusText}`);
  }
  const markets = await res.json() as PolymarketMarket[];

  // Fix: Gamma API returns clobTokenIds as a JSON string, not an array
  // Parse it into an actual array
  for (const market of markets) {
    if (typeof market.clobTokenIds === "string") {
      try {
        market.clobTokenIds = JSON.parse(market.clobTokenIds);
      } catch {
        market.clobTokenIds = [];
      }
    }
    // Same for outcomes and outcomePrices
    if (typeof market.outcomes === "string") {
      try {
        market.outcomes = JSON.parse(market.outcomes);
      } catch {
        market.outcomes = [];
      }
    }
    if (typeof market.outcomePrices === "string") {
      try {
        market.outcomePrices = JSON.parse(market.outcomePrices);
      } catch {
        market.outcomePrices = [];
      }
    }
  }

  return markets;
}

export async function getMarket(conditionId: string): Promise<PolymarketMarket> {
  const url = `${GAMMA_API}/markets/${conditionId}`;
  console.log("[PolyClient] Fetching market:", conditionId);

  const res = await fetch(url, { signal: AbortSignal.timeout(EXECUTION.timeoutMs) });
  if (!res.ok) {
    throw new Error(`Gamma API error: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<PolymarketMarket>;
}

// ─── CLOB client wrappers ───────────────────────────────────────────────────

export async function getOrderBook(tokenId: string): Promise<PolymarketOrderBook> {
  // Use direct REST call - the CLOB client's getOrderBook has issues
  const url = `${CLOB_HOST}/book?token_id=${tokenId}`;
  console.log(`[PolyClient] Fetching orderbook: ${url.slice(0, 80)}...`);
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
    console.log(`[PolyClient] Orderbook response: ${res.status} ${res.statusText}`);
    if (res.ok) {
      const data = await res.json() as PolymarketOrderBook;

      // IMPORTANT: CLOB returns bids sorted ASCENDING (lowest first)
      // and asks sorted DESCENDING (highest first).
      // We need to reverse them so best bid (highest) is first and best ask (lowest) is first.
      if (data.bids && data.bids.length > 1) {
        data.bids = data.bids.reverse();
      }
      if (data.asks && data.asks.length > 1) {
        data.asks = data.asks.reverse();
      }

      console.log(`[PolyClient] Orderbook: ${data.bids?.length ?? 0} bids, ${data.asks?.length ?? 0} asks (best bid: ${data.bids?.[0]?.price ?? 'none'}, best ask: ${data.asks?.[0]?.price ?? 'none'})`);
      return data;
    }
    console.warn(`[PolyClient] Orderbook fetch failed: ${res.status}`);
  } catch (err) {
    console.warn(`[PolyClient] Orderbook error:`, err instanceof Error ? err.message : err);
  }
  // Return empty orderbook on failure
  return { market: "", asset_id: tokenId, hash: "", timestamp: "", bids: [], asks: [] };
}

export async function getOpenOrders() {
  // Use relay client if proxy is configured (same routing as placeOrder)
  const proxyUrl = CLOB_PROXY_URL;
  if (proxyUrl) {
    const relayClient = await getRelayClient();
    return relayClient.getOpenOrders();
  }
  const client = await getClient();
  return client.getOpenOrders();
}

export async function getOrder(orderId: string) {
  // Use relay client if proxy is configured
  const proxyUrl = CLOB_PROXY_URL;
  if (proxyUrl) {
    const relayClient = await getRelayClient();
    return relayClient.getOrder(orderId);
  }
  const client = await getClient();
  return client.getOrder(orderId);
}

export async function getPositions() {
  const client = await getClient();
  // The CLOB client doesn't have a direct positions endpoint,
  // but getBalanceAllowance can give us position-related info.
  // For now we use open orders as a proxy; real positions come from
  // the data API or trades history.
  const trades = await client.getTrades();
  return trades;
}

export async function placeOrder(
  tokenId: string,
  side: "BUY" | "SELL",
  price: number,
  size: number,
  orderType: "GTC" | "GTD" = "GTC"
) {
  const proxyUrl = CLOB_PROXY_URL;
  console.log(`[PolyClient] placeOrder called. CLOB_PROXY_URL=${proxyUrl || "(not set)"}`);

  if (proxyUrl) {
    // SIGN locally, then POST through proxy with clean headers to bypass geoblock.
    // The @polymarket/clob-client's internal HTTP client leaks geo headers,
    // so we manually construct the request with only the required POLY_* auth headers.
    console.log(`[PolyClient] Signing order locally, submitting via proxy: ${proxyUrl}`);

    const relayClient = await getRelayClient();

    // Step 1: Create (sign) the order locally — no network call
    const signedOrder = await relayClient.createOrder(
      {
        tokenID: tokenId,
        side: side === "BUY" ? Side.BUY : Side.SELL,
        price,
        size,
      },
      { tickSize: "0.01", negRisk: false }
    );
    console.log("[PolyClient] Order signed locally:", JSON.stringify(signedOrder).slice(0, 150));

    // Step 2: Build the order payload (same format as CLOB client's postOrder)
    const creds = relayClient.creds;
    if (!creds) throw new Error("CLOB client has no API credentials");

    const orderPayload = {
      deferExec: false,
      order: {
        salt: parseInt(signedOrder.salt, 10),
        maker: signedOrder.maker,
        signer: signedOrder.signer,
        taker: signedOrder.taker,
        tokenId: signedOrder.tokenId,
        makerAmount: signedOrder.makerAmount,
        takerAmount: signedOrder.takerAmount,
        side: side === "BUY" ? "BUY" : "SELL",
        expiration: signedOrder.expiration,
        nonce: signedOrder.nonce,
        feeRateBps: signedOrder.feeRateBps,
        signatureType: signedOrder.signatureType,
        signature: signedOrder.signature,
      },
      owner: creds.key,
      orderType: orderType === "GTC" ? "GTC" : "GTD",
    };

    // Step 3: Build HMAC auth headers manually
    const ts = Math.floor(Date.now() / 1000);
    const bodyStr = JSON.stringify(orderPayload);

    // HMAC signature: key=secret, message = ts + method + path + body
    const encoder = new TextEncoder();
    const cryptoKey = await crypto.subtle.importKey(
      "raw",
      encoder.encode(creds.secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    const message = `${ts}\nPOST\n/order\n${bodyStr}`;
    const sigBuf = await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(message));
    const sigB64 = btoa(String.fromCharCode(...new Uint8Array(sigBuf)));

    const signerAddress = await (async () => {
      const w = new Wallet(process.env.POLYMARKET_PRIVATE_KEY!);
      return w.address;
    })();

    // Step 4: POST to proxy with ONLY the required headers (no geo headers)
    const submitUrl = `${proxyUrl.replace(/\/$/, "")}/order`;
    console.log(`[PolyClient] POSTing to ${submitUrl}`);

    const res = await fetch(submitUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "POLY_ADDRESS": signerAddress,
        "POLY_SIGNATURE": sigB64,
        "POLY_TIMESTAMP": `${ts}`,
        "POLY_API_KEY": creds.key,
        "POLY_PASSPHRASE": creds.passphrase,
      },
      body: bodyStr,
      signal: AbortSignal.timeout(EXECUTION.timeoutMs),
    });

    const responseText = await res.text();
    console.log(`[PolyClient] Proxy response: ${res.status} ${responseText.slice(0, 300)}`);

    if (!res.ok) {
      throw new Error(`CLOB order failed: ${res.status} ${responseText.slice(0, 200)}`);
    }

    try {
      return JSON.parse(responseText);
    } catch {
      return { raw: responseText };
    }
  }

  // Direct CLOB client (may be geoblocked)
  const client = await getClient();
  const result = await client.createAndPostOrder(
    {
      tokenID: tokenId,
      side: side === "BUY" ? Side.BUY : Side.SELL,
      price,
      size,
    },
    { tickSize: "0.01", negRisk: false },
    orderType === "GTC" ? OrderType.GTC : OrderType.GTD
  );
  return result;
}

// ─── Relay Client (routes through CLOB_PROXY_URL to bypass geoblock) ────────

let relayClobClient: ClobClient | null = null;
let relayInitPromise: Promise<ClobClient> | null = null;

async function getRelayClient(): Promise<ClobClient> {
  if (relayClobClient) return relayClobClient;
  if (relayInitPromise) return relayInitPromise;

  relayInitPromise = (async () => {
    const proxyUrl = CLOB_PROXY_URL.replace(/\/$/, "");
    const privateKey = process.env.POLYMARKET_PRIVATE_KEY!;
    const walletAddress = process.env.POLYMARKET_WALLET_ADDRESS!;

    console.log("[PolyClient] Initializing relay ClobClient via", proxyUrl);

    const signer = new Wallet(privateKey);

    // Derive API creds via the relay (auth endpoint also needs to bypass geoblock)
    const tempClient = new ClobClient(proxyUrl, CHAIN_ID, signer);
    const creds = await tempClient.createOrDeriveApiKey();
    console.log("[PolyClient] Relay API creds derived");

    const client = new ClobClient(
      proxyUrl,
      CHAIN_ID,
      signer,
      creds,
      2, // GNOSIS_SAFE
      walletAddress
    );

    relayClobClient = client;
    console.log("[PolyClient] Relay ClobClient ready");
    return client;
  })();

  return relayInitPromise;
}

export async function cancelOrder(orderId: string) {
  const client = await getClient();
  return client.cancelOrder({ orderID: orderId });
}

export async function cancelAllOrders() {
  const client = await getClient();
  return client.cancelAll();
}

// USDC.e contract on Polygon (what Polymarket uses)
const USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const POLYGON_RPC = "https://1rpc.io/matic";
// balanceOf(address) function selector
const BALANCE_OF_SELECTOR = "0x70a08231";

export async function getBalance(): Promise<number> {
  const walletAddress = process.env.POLYMARKET_WALLET_ADDRESS;
  if (!walletAddress) {
    console.warn("[PolyClient] No POLYMARKET_WALLET_ADDRESS set, cannot fetch balance");
    return 0;
  }

  // Query USDC.e balance directly from Polygon chain via public RPC
  const addrPadded = walletAddress.replace("0x", "").toLowerCase().padStart(64, "0");
  const callData = `${BALANCE_OF_SELECTOR}${addrPadded}`;

  try {
    console.log("[PolyClient] Fetching on-chain USDC.e balance for", walletAddress);
    const res = await fetch(POLYGON_RPC, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "eth_call",
        params: [{ to: USDC_E_ADDRESS, data: callData }, "latest"],
        id: 1,
      }),
      signal: AbortSignal.timeout(10_000),
    });

    if (res.ok) {
      const json = (await res.json()) as { result?: string; error?: unknown };
      if (json.result) {
        const rawBalance = parseInt(json.result, 16);
        // USDC.e has 6 decimals
        const bal = rawBalance / 1e6;
        console.log(`[PolyClient] On-chain USDC.e balance: $${bal.toFixed(2)}`);
        return bal;
      }
      console.warn("[PolyClient] RPC returned no result:", json.error);
    }
  } catch (err) {
    console.warn("[PolyClient] Polygon RPC error:", err instanceof Error ? err.message : String(err));
  }

  // Do NOT fall back to CLOB - it returns 0 due to "Invalid asset type" error
  // Return -1 to signal that the fetch failed (caller should keep existing balance)
  console.warn("[PolyClient] On-chain balance fetch failed, returning -1 to preserve existing balance");
  return -1;
}

export async function getMidpoint(tokenId: string): Promise<number> {
  const client = await getClient();
  const mid = await client.getMidpoint(tokenId);
  return Number(mid);
}

// ─── CLOB Sampling Markets (high liquidity markets with embedded prices) ────

export interface ClobSamplingMarket {
  condition_id: string;
  question: string;
  tokens: Array<{
    token_id: string;
    outcome: string;
    price: number;
    winner: boolean;
  }>;
  end_date_iso?: string;
  volume_num?: number;
  liquidity?: number;
}

export async function getSamplingMarkets(): Promise<ClobSamplingMarket[]> {
  const url = `${CLOB_HOST}/sampling-markets`;
  console.log("[PolyClient] Fetching CLOB sampling markets...");

  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(15_000) });
    if (!res.ok) {
      console.warn(`[PolyClient] Sampling markets fetch failed: ${res.status}`);
      return [];
    }

    const data = await res.json() as Record<string, unknown>;
    // The response has a "data" array
    const markets = ((data?.data ?? data) as ClobSamplingMarket[]);
    console.log(`[PolyClient] Got ${markets.length} sampling markets with embedded prices`);
    return markets;
  } catch (err) {
    console.warn("[PolyClient] Sampling markets error:", err instanceof Error ? err.message : err);
    return [];
  }
}

export async function getPrice(tokenId: string, side: "BUY" | "SELL"): Promise<number> {
  const client = await getClient();
  const price = await client.getPrice(tokenId, side);
  return Number(price);
}

