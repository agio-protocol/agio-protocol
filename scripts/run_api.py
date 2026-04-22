#!/usr/bin/env python3
"""Run the AGIO API server against mainnet database."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))

os.environ["DATABASE_URL"] = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio_mainnet"
os.environ["REDIS_URL"] = "redis://localhost:6379/1"
os.environ["RPC_URL"] = "https://mainnet.base.org"
os.environ["VAULT_ADDRESS"] = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
os.environ["BATCH_SETTLEMENT_ADDRESS"] = "0x3937a057AE18971657AD12830964511B73D9e7C5"

import uvicorn
uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
