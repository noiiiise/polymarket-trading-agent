# Polymarket Trading Agent — Strategy & Learnings

## Architecture

Ported from Python (github.com/noiiiise/polymarket-trading-agent) to TypeScript running on Bun/Hono.
Backend at `/backend`, frontend dashboard at `/webapp`.

### Key Integration Details

- **Wallet**: `0xa33527A07Ad5e257BC6251ed8CC8B1D89b9DE247`
- **Balance source**: On-chain USDC.e query via Polygon RPC (`1rpc.io/matic`), NOT Gamma/Data API (those require auth tokens we don't have)
- **CLOB client**: `@polymarket/clob-client` v5.8.0, signature type 2 (GNOSIS_SAFE), chain 137
- **API auth**: Derived via `createOrDeriveApiKey()` from private key signer

### What Didn't Work
- Gamma API `/profiles/{address}` — returns 404
- Data API `/value?user={address}` — returns "invalid" for checksummed addresses
- Polygon public RPCs: `ankr.com` requires API key, `llamarpc.com` times out
- **Working RPC**: `https://1rpc.io/matic` — reliable, free, no key needed
- USDC.e contract: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (6 decimals)

## Active Strategies

### 1. Copy Trade
- Monitors top-performing Polymarket wallets and mirrors their positions
- **Params**: 5% max position per copy, 60s polling, top 20 wallets tracked
- **Filters**: Min 48h until resolution, max 5% spread, min 10 trades from source wallet
- **Status**: Leaderboard discovery needs real Polymarket profile/leaderboard API access

### 2. Volume Spike
- Detects abnormal order book activity (2x rolling 12h average)
- **Params**: 15% max position, 2x spike threshold, 12h volume buckets
- **Filters**: Analyzes bid/ask depth imbalance after spike detection
- **Status**: Active, scanning top 50 markets per cycle

## Risk Management
- **40% total wallet exposure cap** — hard limit, no trade exceeds this
- **Limit orders only** — no market orders to avoid slippage
- **Per-strategy caps**: Copy trade 5%, Volume spike 15%
- **Current balance**: ~$97.93 USDC.e

## Signals & Notes from X

_Add observations, alpha, or market signals below. The agent will read these on each cycle._

<!--
Format:
- [DATE] SIGNAL: description
- [DATE] MARKET: specific market insight
- [DATE] ALPHA: trading edge or pattern noticed
-->

---

_Last updated: 2026-03-16_
