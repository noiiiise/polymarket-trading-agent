# Polymarket Trading Agent

Autonomous prediction market trading agent for Polymarket. Runs dual strategies — copy trading and volume spike detection — with built-in risk management.

Also ported to TypeScript for the Vibecode web dashboard (see STRATEGY_DOC.md for integration notes).

## Strategies

### Copy Trade
- Monitors top 20 wallets by 30-day profit
- Mirrors new positions with 5% max allocation per trade
- Filters: min 48h to resolution, max 5% spread, min 10 source trades
- Polls every 60 seconds, refreshes leaderboard every 6 hours

### Volume Spike
- Tracks volume in 12-hour buckets across active markets
- Detects spikes >2x rolling average
- Analyzes price wall concentration and trend alignment
- Position sizing: 5-15% based on spike magnitude

## Risk Management
- 40% total wallet exposure cap (hard limit)
- Limit orders only (no market orders)
- Paper trading mode (default: enabled)
- Per-strategy position size caps

## Integration Learnings

### What Works
- **Polygon RPC** (`https://1rpc.io/matic`): Free, reliable, no API key needed
- **USDC.e balance**: Direct `eth_call` to `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` with `balanceOf` selector
- **CLOB client**: `createOrDeriveApiKey()` from private key signer works for auth
- **Gamma API markets**: `GET /markets?active=true&closed=false` works for market discovery

### What Doesn't Work
- `polygon-rpc.com` — now requires API key, returns 403
- `ankr.com/polygon` — requires API key
- Gamma API `/profiles/{address}` — returns 404
- `data-api.polymarket.com/value?user=` — returns "invalid" for checksummed addresses

## Setup

```bash
cp .env.example .env
# Edit .env with your Polymarket credentials
pip install -r requirements.txt
python main.py
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `POLYMARKET_API_KEY` | CLOB API key | required |
| `POLYMARKET_PRIVATE_KEY` | Wallet private key | required |
| `POLYMARKET_WALLET_ADDRESS` | Wallet/funder address | required |
| `POLYGON_RPC_URL` | Polygon RPC endpoint | `https://1rpc.io/matic` |
| `PAPER_TRADING` | Enable paper mode | `true` |
| `GITHUB_TOKEN` | For auto-updating STRATEGY_DOC | optional |
| `GITHUB_OWNER` | GitHub username | optional |

## Architecture

- **Async Python** with `asyncio` + `aiohttp`
- **SQLite** via `aiosqlite` for persistent state
- **Signal handling** for graceful shutdown
- **Daily log rotation** with 30-day retention
- **Auto-documentation** via GitHub API updates to STRATEGY_DOC.md
