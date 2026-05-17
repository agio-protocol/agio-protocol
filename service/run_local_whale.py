#!/usr/bin/env python3
"""Run whale follow bot locally."""
import asyncio, logging, os, sys
from pathlib import Path

env_file = Path(__file__).parent / ".env.local"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

async def main():
    from src.core.database import engine
    from src.models.base import Base
    from src.workers.whale_follow_bot import WhalePosition, WhaleTrade, TrackedWhale
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from src.workers.whale_follow_bot import run
    await run()

if __name__ == "__main__":
    asyncio.run(main())
