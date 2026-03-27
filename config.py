"""
Configuration constants and thresholds for the Polymarket trading agent.
All tunable parameters live here — no magic numbers scattered in code.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Credentials (from environment) ──────────────────────────────────────────
POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_WALLET_ADDRESS: str = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

# GitHub repo for STRATEGY_DOC.md commits
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "polymarket-trading-agent")
GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "")
GITHUB_BRANCH: str = os.getenv("GITHUB_BRANCH", "main")

# ── Polymarket API Endpoints ────────────────────────────────────────────────
POLYMARKET_REST_BASE: str = "https://clob.polymarket.com"
POLYMARKET_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYMARKET_GAMMA_API: str = "https://gamma-api.polymarket.com"

# Chain / Execution
# Primary Polygon RPC — overrideable via env var
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")

# Free public RPC fallback pool (tried in order when primary is rate-limited)
POLYGON_RPC_FALLBACKS: list[str] = [
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://polygon-mainnet.public.blastapi.io",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]
CHAIN_ID: int = 137  # Polygon mainnet

# USDC.e on Polygon (what Polymarket uses for collateral)
USDC_E_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
BALANCE_OF_SELECTOR: str = "0x70a08231"  # ERC-20 balanceOf(address)

# NOTE: Gamma API /profiles/{address} returns 404 for most wallets.
# data-api.polymarket.com/value?user= requires lowercase addresses.
# Most reliable balance method: direct on-chain USDC.e query via RPC.

# ── Copy Trade Strategy ─────────────────────────────────────────────────────
# When non-empty, bypass the leaderboard entirely and track only these addresses.
COPY_TRADE_PINNED_WALLETS: list[str] = [
    "0x7f3c8979d0afa00007bae4747d5347122af05613",
]
COPY_TRADE_MAX_POSITION_PCT: float = 0.08          # 8% of wallet per copy trade (HARD CAP)
COPY_TRADE_POLL_INTERVAL_SEC: int = 60              # Poll top wallets every 60s
COPY_TRADE_TOP_WALLETS_COUNT: int = 10              # Track top 10 most profitable wallets
COPY_TRADE_MIN_WALLET_TRADES: int = 10              # Skip wallets with <10 trades in 30d
COPY_TRADE_MIN_RESOLUTION_HOURS: float = 1.0        # Skip markets resolving within 1hr
COPY_TRADE_MAX_SPREAD_PCT: float = 0.10             # Skip if spread > 10%
COPY_TRADE_LEADERBOARD_REFRESH_HOURS: int = 6       # Refresh leaderboard every 6 hours

# ── Volume Spike Strategy ───────────────────────────────────────────────────
VOLUME_SPIKE_MAX_POSITION_PCT: float = 0.15         # 15% of wallet per spike trade (HARD CAP)
VOLUME_SPIKE_BUCKET_HOURS: int = 12                 # Volume tracking bucket size
VOLUME_SPIKE_THRESHOLD_MULTIPLIER: float = 2.0      # Flag if volume > 2x rolling avg
VOLUME_SPIKE_CHECK_INTERVAL_SEC: int = 43200        # Check every 12 hours (43200s)
VOLUME_SPIKE_PRICE_WALL_PCT: float = 0.50           # 50%+ volume at 1-2 levels = wall
VOLUME_SPIKE_FADE_ENABLED: bool = True              # Allow fading noisy spikes

# ── Wallet / Risk Management ────────────────────────────────────────────────
MAX_TOTAL_EXPOSURE_PCT: float = 0.90                # Total open positions <= 90% of wallet
BALANCE_REFRESH_INTERVAL_SEC: int = 120             # Refresh wallet balance every 2 min

# ── Execution ───────────────────────────────────────────────────────────────
ORDER_TYPE: str = "limit"                           # Always limit orders, never market
MAX_SPREAD_FOR_BEST_PRICE_PCT: float = 0.02         # Use best price if spread <= 2%
ORDER_RETRY_DELAY_SEC: int = 5                      # Retry failed order after 5s
ORDER_MAX_RETRIES: int = 1                          # Retry once then skip
ORDER_TIMEOUT_SEC: int = 30                         # Cancel unfilled order after 30s

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR: str = "logs"
LOG_ROTATION: str = "midnight"                      # Rotate logs daily at midnight
LOG_RETENTION_DAYS: int = 30
LOG_FORMAT: str = "%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s"

# ── Database ────────────────────────────────────────────────────────────────
SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/agent.db")

# ── Strategy Doc Update ─────────────────────────────────────────────────────
STRATEGY_DOC_PATH: str = "STRATEGY_DOC.md"
STRATEGY_DOC_UPDATE_INTERVAL_SEC: int = 86400       # Summary update at least every 24h

# ── Simulation Mode ─────────────────────────────────────────────────────────
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_TRADING_INITIAL_BALANCE: float = 10000.0      # Simulated starting balance
