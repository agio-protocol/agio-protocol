#!/usr/bin/env python3
"""Standalone runner for Copy Trade Bot — runs outside gunicorn."""
import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

async def main():
    # Create DB tables if needed
    from src.core.database import engine
    from src.models.base import Base
    from src.workers.copy_trader import CopyPosition, CopyTrade, TrackedWallet  # noqa
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from src.workers.copy_trader import run
    await run()

if __name__ == "__main__":
    asyncio.run(main())
