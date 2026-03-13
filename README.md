# Polymarket Trading Agent

A fully autonomous Python trading agent for [Polymarket](https://polymarket.com) prediction markets. Runs two concurrent strategies — **copy trading** and **volume spike detection** — with a shared wallet manager, risk enforcement, and automatic strategy logging.

## Architecture

```
polymarket-trading-agent/
├── main.py                  # Entry point, starts all loops
├── config.py                # All thresholds, constants, env vars
├── wallet.py                # Wallet balance tracker + position manager
├── execution.py             # Order execution via CLOB API
├── database.py              # SQLite persistence layer
├── strategies/
│   ├── copy_trade.py        # Strategy 1: Copy top wallets
│   └── volume_spike.py      # Strategy 2: Volume spike detection
├── logger.py                # Writes STRATEGY_DOC.md to GitHub
├── STRATEGY_DOC.md          # Auto-updated strategy learning log
├── requirements.txt
├── .env.example
└── logs/                    # Daily-rotated log files
```

## Strategies

### 1. Copy Trading
Monitors the top 20 Polymarket wallets by 30-day profit and replicates their new positions in real time. Position size mirrors the tracked wallet's allocation percentage, hard-capped at 5% of our wallet per trade.

### 2. Volume Spike Detection
Tracks order book volume in 12-hour buckets across all active markets. When volume exceeds 2x the rolling 24-hour average, the agent analyzes:
- **Price concentration** — clustered volume at 1-2 levels indicates limit walls (strong conviction)
- **Trend alignment** — spike direction matches 24-hour price trend

Trade decisions:
- **Enter** when wall + trend align
- **Fade** when volume is spread (retail noise)
- **Skip** when signals conflict

Position size scales with spike magnitude, hard-capped at 15% of wallet.

## Risk Management

| Rule | Limit |
|------|-------|
| Copy trade position cap | 5% of wallet |
| Volume spike position cap | 15% of wallet |
| Total exposure cap | 40% of wallet |
| Min resolution time (copy) | 1 hour |
| Max spread (copy) | 10% |
| Min wallet trades (copy) | 10 in 30 days |
| Order type | Limit only (never market) |
| Order retry | 1 retry after 5s, then skip |

## Setup

### Prerequisites
- Python 3.11+
- A Polymarket account with API access
- A Polygon wallet with USDC
- A GitHub personal access token (for strategy log commits)

### Installation

```bash
git clone https://github.com/YOUR_USER/polymarket-trading-agent.git
cd polymarket-trading-agent
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_API_KEY` | For live | CLOB API key |
| `POLYMARKET_PRIVATE_KEY` | For live | Wallet private key for signing |
| `POLYMARKET_WALLET_ADDRESS` | For live | Your Polygon wallet address |
| `GITHUB_TOKEN` | No | GitHub PAT for STRATEGY_DOC.md |
| `GITHUB_OWNER` | No | GitHub username/org |
| `GITHUB_REPO` | No | Repo name (default: polymarket-trading-agent) |
| `PAPER_TRADING` | No | `true` for simulation (default: true) |

### Running

**Paper trading (simulation mode):**
```bash
python main.py
```

**Live trading:**
```bash
PAPER_TRADING=false python main.py
```

The agent is fully restartable — all state persists in SQLite (`data/agent.db`).

### Running with GitHub Actions

Set the environment variables as GitHub Secrets, then create a workflow that runs:
```yaml
- name: Run agent
  env:
    POLYMARKET_API_KEY: ${{ secrets.POLYMARKET_API_KEY }}
    POLYMARKET_PRIVATE_KEY: ${{ secrets.POLYMARKET_PRIVATE_KEY }}
    POLYMARKET_WALLET_ADDRESS: ${{ secrets.POLYMARKET_WALLET_ADDRESS }}
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    PAPER_TRADING: "false"
  run: python main.py
```

## STRATEGY_DOC.md

The agent auto-updates `STRATEGY_DOC.md` via GitHub API. It writes:
- After every resolved trade (win/loss, P&L)
- After every volume spike event (traded or not)
- At least once every 24 hours as a summary

Read this file to track the agent's performance and what it's learning over time.

## Development

The codebase is fully async (`asyncio` + `aiohttp` + `websockets`). Key design decisions:

- **SQLite** for persistence — survives restarts, no external DB needed
- **Limit orders only** — no market orders, ever
- **Paper trading first** — set `PAPER_TRADING=true` to test without risk
- **Modular strategies** — each strategy is a standalone class with `start()`, `stop()`, `run()`
- **All thresholds in config.py** — no magic numbers in strategy code

## License

MIT
