"""
Trading agent entry point — runs without the Flask dashboard.
Used by start.sh so gunicorn and the agent are separate OS processes.
"""
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime

import aiosqlite
import config
import database
import reflection
from execution import OrderExecutor
from logger import StrategyDocLogger
from strategies.copy_trade import CopyTradeStrategy
from strategies.volume_spike import VolumeSpikeStrategy
from wallet import WalletManager
import reflection


def setup_logging() -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(config.LOG_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(config.LOG_DIR, "agent.log"),
        when=config.LOG_ROTATION,
        backupCount=config.LOG_RETENTION_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Silence noisy third-party HTTP loggers — they dump full headers
    # (including API keys) which triggers Cloudflare WAF on the logs endpoint.
    for name in ("httpcore", "httpx", "hpack", "h2", "h11", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


logger = logging.getLogger("agent")


async def run_agent() -> None:
    logger.info("=" * 60)
    logger.info("Polymarket Trading Agent starting...")
    logger.info("Paper trading: %s", config.PAPER_TRADING)
    logger.info("=" * 60)

    if not config.PAPER_TRADING:
        missing = [k for k, v in {
            "POLYMARKET_API_KEY": config.POLYMARKET_API_KEY,
            "POLYMARKET_PRIVATE_KEY": config.POLYMARKET_PRIVATE_KEY,
            "POLYMARKET_WALLET_ADDRESS": config.POLYMARKET_WALLET_ADDRESS,
        }.items() if not v]
        if missing:
            logger.error("Missing required credentials: %s", ", ".join(missing))
            sys.exit(1)

    if not config.GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — STRATEGY_DOC.md will only update locally")

    logger.info("Initializing database at %s", config.SQLITE_DB_PATH)
    await database.init_db()
    db = await database.get_db()

    wallet_mgr = WalletManager()
    executor = OrderExecutor(db)
    # Retry executor startup so a transient CLOB rate-limit doesn't crash-loop Railway.
    for _attempt in range(1, 6):
        try:
            await executor.start()
            break
        except Exception as _exc:
            _wait = min(60 * _attempt, 300)
            logger.warning(
                "Executor startup failed (attempt %d/5): %s — retrying in %ds",
                _attempt, _exc, _wait,
            )
            await asyncio.sleep(_wait)
    else:
        logger.critical("Could not start executor after 5 attempts — exiting")
        sys.exit(1)
    wallet_mgr.set_executor(executor)
    await wallet_mgr.start(db)

    copy_strategy = CopyTradeStrategy(db, wallet_mgr, executor)
    await copy_strategy.start()

    spike_strategy = VolumeSpikeStrategy(db, wallet_mgr, executor)
    await spike_strategy.start()

    doc_logger = StrategyDocLogger(db)
    await doc_logger.start()

    shutdown_event = asyncio.Event()

    def handle_shutdown(sig: int, frame: object) -> None:
        logger.info("Received signal %s, shutting down...", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    task_factories = {
        "wallet_refresh": lambda: wallet_mgr.refresh_loop(),
        "copy_trade": lambda: copy_strategy.run(),
        "volume_spike": lambda: spike_strategy.run(),
        "daily_summary": lambda: _daily_summary_loop(doc_logger, shutdown_event),
        "pending_sells": lambda: _pending_sells_loop(executor, wallet_mgr, shutdown_event),
        "nightly_reflection": lambda: reflection.reflection_loop(db, shutdown_event),
    }
    tasks = {
        name: asyncio.create_task(factory(), name=name)
        for name, factory in task_factories.items()
    }

    logger.info("All systems running. Monitoring 2 strategy modules.")

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break

        for name, task in list(tasks.items()):
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                logger.error(
                    "Task '%s' died unexpectedly%s — restarting",
                    name,
                    f": {exc}" if exc else "",
                )
                tasks[name] = asyncio.create_task(
                    task_factories[name](), name=name
                )

    logger.info("Shutting down...")
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)

    try:
        await doc_logger.log_daily_summary()
    except Exception as e:
        logger.error("Failed final doc update: %s", e)

    await copy_strategy.stop()
    await spike_strategy.stop()
    await executor.stop()
    await wallet_mgr.stop()
    await doc_logger.stop()
    await db.close()
    logger.info("Agent shut down cleanly.")


async def _pending_sells_loop(
    executor: OrderExecutor,
    wallet: WalletManager,
    shutdown_event: asyncio.Event,
) -> None:
    """
    Poll for queued SELL orders written by the dashboard /api/sell endpoint.
    Opens a fresh DB connection each cycle to avoid WAL read-snapshot isolation
    preventing the agent from seeing rows committed by the dashboard process.
    """
    while not shutdown_event.is_set():
        try:
            # Fresh connection each iteration so we always see the latest WAL data.
            async with aiosqlite.connect(config.SQLITE_DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS pending_sells (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id   TEXT NOT NULL,
                        market_id  TEXT NOT NULL DEFAULT '',
                        size       REAL NOT NULL,
                        price      REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        executed   INTEGER NOT NULL DEFAULT 0
                    )
                """)
                await conn.commit()

                rows = await conn.execute_fetchall(
                    "SELECT id, token_id, market_id, size, price FROM pending_sells WHERE executed=0"
                )

            for row in rows:
                token_id = row["token_id"]
                market_id = row["market_id"] or token_id
                size = float(row["size"])
                price = float(row["price"])
                row_id = row["id"]
                logger.info(
                    "Processing pending SELL: token=%s size=%.2f price=%.4f",
                    token_id[:20], size, price,
                )
                try:
                    result = await executor.place_order(
                        token_id=token_id,
                        market_id=market_id,
                        outcome="SELL",
                        side="SELL",
                        price=price,
                        size=size,
                    )
                    logger.info("Pending SELL result: %s", result)
                except Exception as e:
                    logger.error("Pending SELL failed for %s: %s", token_id[:20], e)

                async with aiosqlite.connect(config.SQLITE_DB_PATH) as conn:
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute(
                        "UPDATE pending_sells SET executed=1 WHERE id=?", (row_id,)
                    )
                    await conn.commit()
                await wallet.refresh()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Pending sells loop error: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            pass


async def _daily_summary_loop(doc_logger: StrategyDocLogger, shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        try:
            if await doc_logger.should_update_daily():
                logger.info("Running daily STRATEGY_DOC.md update...")
                await doc_logger.log_daily_summary()
        except Exception as e:
            logger.error("Daily summary error: %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
            break
        except asyncio.TimeoutError:
            continue


if __name__ == "__main__":
    setup_logging()
    logger.info("Python %s on %s", sys.version, sys.platform)
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
