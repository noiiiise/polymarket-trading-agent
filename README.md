# Polymarket Trading Agent

Autonomous prediction market trading agent with a real-time dashboard. Ported from the Python agent at [github.com/noiiiise/polymarket-trading-agent](https://github.com/noiiiise/polymarket-trading-agent).

## Architecture

- **Frontend** (`webapp/`) — React + Vite trading dashboard on port 8000
- **Backend** (`backend/`) — Hono + Bun API server on port 3000 with Polymarket CLOB integration

## Trading Strategies

1. **Copy Trade** — Monitors top-performing Polymarket wallets and mirrors their trades (5% max per position)
2. **Volume Spike** — Detects abnormal order book volume (2x rolling average) and trades on conviction signals (15% max per position)

## Risk Management

- 40% total wallet exposure cap
- Limit orders only (no market orders)
- Paper trading mode enabled by default
- Per-strategy position size caps

## Backend API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/polymarket/status` | GET | Engine state, balance, P&L |
| `/api/polymarket/start` | POST | Start trading engine |
| `/api/polymarket/stop` | POST | Stop trading engine |
| `/api/polymarket/markets` | GET | Browse active markets |
| `/api/polymarket/market/:id` | GET | Single market details |
| `/api/polymarket/orderbook/:tokenId` | GET | Order book for a token |
| `/api/polymarket/positions` | GET | Current positions |
| `/api/polymarket/orders` | GET | Open orders |
| `/api/polymarket/trades` | GET | Trade history |
| `/api/polymarket/trade` | POST | Place a manual trade |
| `/api/polymarket/cancel/:orderId` | POST | Cancel an order |
| `/api/polymarket/cancel-all` | POST | Cancel all orders |
| `/api/polymarket/logs` | GET | Engine activity logs |
| `/api/polymarket/config` | GET | Current config/thresholds |

## Environment Variables (Backend)

| Variable | Description |
|----------|-------------|
| `POLYMARKET_API_KEY` | Polymarket CLOB API key |
| `POLYMARKET_PRIVATE_KEY` | Wallet private key for signing orders |
| `POLYMARKET_WALLET_ADDRESS` | Polymarket wallet/funder address |
