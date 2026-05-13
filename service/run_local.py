#!/usr/bin/env python3
"""Run the meme bot locally with .env.local config."""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env.local
env_file = Path(__file__).parent / ".env.local"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip()
    print(f"Loaded {env_file}")

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

async def main():
    from src.core.database import engine
    from src.models.base import Base
    from src.workers.paper_trader import PaperPosition, PaperTrade  # noqa
    
    # Create tables if needed (including new exit engine columns)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add exit engine columns
        from sqlalchemy import text
        for sql in [
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS entry_liquidity_usd NUMERIC(18,2)",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS position_size_tokens_original NUMERIC(18,0)",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS position_size_tokens_remaining NUMERIC(18,0)",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS stop_price NUMERIC(18,10)",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS trailing_active BOOLEAN DEFAULT FALSE",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tier_1_done BOOLEAN DEFAULT FALSE",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tier_2_done BOOLEAN DEFAULT FALSE",
            "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tier_3_done BOOLEAN DEFAULT FALSE",
        ]:
            try:
                await conn.execute(text(sql))
            except:
                pass
    print("DB ready")
    
    from src.workers.paper_trader import run as paper_trader_run
    print("Starting meme bot locally...")
    await paper_trader_run()

if __name__ == "__main__":
    asyncio.run(main())
