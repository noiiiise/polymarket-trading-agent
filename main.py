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
import threading
from datetime import datetime

import config
import database
from execution import OrderExecutor
from logger import StrategyDocLogger
from redemption import RedemptionManager
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


async def run_agent(thread_stop: threading.Event) -> None:
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

    redemption_mgr = RedemptionManager(db)
    await redemption_mgr.start()

    # Shutdown event — polled from thread_stop so signals stay in main thread
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def _poll_thread_stop() -> None:
        while not thread_stop.is_set():
            await asyncio.sleep(0.5)
        loop.call_soon_threadsafe(shutdown_event.set)

    # Create tasks
    tasks = [
        asyncio.create_task(wallet_mgr.refresh_loop(), name="wallet_refresh"),
        asyncio.create_task(copy_strategy.run(), name="copy_trade"),
        asyncio.create_task(spike_strategy.run(), name="volume_spike"),
        asyncio.create_task(redemption_mgr.run(), name="redemption"),
        asyncio.create_task(
            _daily_summary_loop(doc_logger, shutdown_event),
            name="daily_summary",
        ),
        asyncio.create_task(_poll_thread_stop(), name="stop_poller"),
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

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("Some tasks did not finish within 30s shutdown window — forcing exit")

    # Final strategy doc update
    try:
        await doc_logger.log_daily_summary()
    except Exception as e:
        logger.error("Failed final doc update: %s", e)

    # Cleanup
    await copy_strategy.stop()
    await spike_strategy.stop()
    await redemption_mgr.stop()
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


def _run_agent_thread(thread_stop: threading.Event) -> None:
    """Run the async trading agent in a background thread."""
    try:
        asyncio.run(run_agent(thread_stop))
    except Exception as e:
        logger.critical("Trading agent crashed: %s", e, exc_info=True)


def main() -> None:
    """
    Entry point.
    Flask runs in the main thread (required for Railway web services).
    The async trading agent runs in a background thread.
    Signal handlers registered here (main thread only) set thread_stop,
    which the agent polls via asyncio to trigger graceful shutdown.
    """
    setup_logging()
    logger.info("Python %s on %s", sys.version, sys.platform)

    thread_stop = threading.Event()

    def handle_shutdown(sig: int, frame: object) -> None:
        logger.info("Received signal %s, initiating shutdown...", sig)
        thread_stop.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start trading agent in background thread
    agent_thread = threading.Thread(
        target=_run_agent_thread, args=(thread_stop,), name="agent", daemon=True
    )
    agent_thread.start()
    logger.info("Trading agent started in background thread")

    # Run Flask dashboard in the main thread — Railway routes HTTP here
    from dashboard import app
    port = int(os.getenv("PORT", "8080"))
    logger.info("Dashboard starting on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
