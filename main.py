"""
Main entry point: Starts all strategy loops, wallet manager, and logger as concurrent asyncio tasks.
Handles graceful shutdown and restart recovery via SQLite persistence.
"""

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime

import config
import database
from dashboard import start_dashboard
from execution import OrderExecutor
from logger import StrategyDocLogger
from strategies.copy_trade import CopyTradeStrategy
from strategies.volume_spike import VolumeSpikeStrategy
from wallet import WalletManager


def setup_logging() -> None:
    """Configure logging to stdout and daily-rotated file."""
    os.makedirs(config.LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(config.LOG_FORMAT)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler with daily rotation
    file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(config.LOG_DIR, "agent.log"),
        when=config.LOG_ROTATION,
        backupCount=config.LOG_RETENTION_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


logger = logging.getLogger("main")


async def run_agent() -> None:
    """Initialize all components and run the agent."""
    logger.info("=" * 60)
    logger.info("Polymarket Trading Agent starting...")
    logger.info("Paper trading: %s", config.PAPER_TRADING)
    logger.info("=" * 60)

    # Validate required config
    if not config.PAPER_TRADING:
        missing = []
        if not config.POLYMARKET_API_KEY:
            missing.append("POLYMARKET_API_KEY")
        if not config.POLYMARKET_PRIVATE_KEY:
            missing.append("POLYMARKET_PRIVATE_KEY")
        if not config.POLYMARKET_WALLET_ADDRESS:
            missing.append("POLYMARKET_WALLET_ADDRESS")
        if missing:
            logger.error("Missing required credentials: %s", ", ".join(missing))
            logger.error("Set PAPER_TRADING=true to run in simulation mode")
            sys.exit(1)

    if not config.GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — STRATEGY_DOC.md will only update locally")
    if not config.GITHUB_OWNER:
        logger.warning("GITHUB_OWNER not set — GitHub push disabled")

    # Start web dashboard
    start_dashboard(port=int(os.getenv("PORT", "5000")))
    logger.info("Dashboard running on http://0.0.0.0:%s", os.getenv("PORT", "5000"))

    # Initialize database
    logger.info("Initializing database at %s", config.SQLITE_DB_PATH)
    await database.init_db()
    db = await database.get_db()

    # Initialize components
    wallet_mgr = WalletManager()
    await wallet_mgr.start(db)

    executor = OrderExecutor(db)
    await executor.start()

    copy_strategy = CopyTradeStrategy(db, wallet_mgr, executor)
    await copy_strategy.start()

    spike_strategy = VolumeSpikeStrategy(db, wallet_mgr, executor)
    await spike_strategy.start()

    doc_logger = StrategyDocLogger(db)
    await doc_logger.start()

    # Shutdown event
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig: int, frame: object) -> None:
        logger.info("Received signal %s, initiating shutdown...", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Create tasks
    tasks = [
        asyncio.create_task(wallet_mgr.refresh_loop(), name="wallet_refresh"),
        asyncio.create_task(copy_strategy.run(), name="copy_trade"),
        asyncio.create_task(spike_strategy.run(), name="volume_spike"),
        asyncio.create_task(
            _daily_summary_loop(doc_logger, shutdown_event),
            name="daily_summary",
        ),
    ]

    logger.info("All systems running. Monitoring %d strategy modules.", 2)

    # Wait for shutdown signal
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    # Graceful shutdown
    logger.info("Shutting down...")

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Final strategy doc update
    try:
        await doc_logger.log_daily_summary()
    except Exception as e:
        logger.error("Failed final doc update: %s", e)

    # Cleanup
    await copy_strategy.stop()
    await spike_strategy.stop()
    await executor.stop()
    await wallet_mgr.stop()
    await doc_logger.stop()
    await db.close()

    logger.info("Agent shut down cleanly.")


async def _daily_summary_loop(
    doc_logger: StrategyDocLogger,
    shutdown_event: asyncio.Event,
) -> None:
    """Periodic loop that triggers daily STRATEGY_DOC.md summary updates."""
    while not shutdown_event.is_set():
        try:
            if await doc_logger.should_update_daily():
                logger.info("Running daily STRATEGY_DOC.md update...")
                await doc_logger.log_daily_summary()
        except Exception as e:
            logger.error("Daily summary error: %s", e)

        # Check every hour
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
            break  # Shutdown signaled
        except asyncio.TimeoutError:
            continue


def main() -> None:
    """Entry point."""
    setup_logging()

    logger.info("Python %s on %s", sys.version, sys.platform)

    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
