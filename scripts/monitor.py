#!/usr/bin/env python3
"""
AGIO Protocol — Mainnet Monitor

Real-time monitoring dashboard. Updates every 10 seconds.
Shows vault balances, transaction stats, batch worker status,
fee revenue, and reconciliation health.

Usage: python3 scripts/monitor.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ADDRESSES_FILE = SCRIPT_DIR / "deployed_addresses.json"

# Token decimals
TOKEN_DECIMALS = {
    "USDC": 6, "USDT": 6, "DAI": 18, "WETH": 18, "cbETH": 18,
}


def load_config():
    env_file = SCRIPT_DIR / ".env.mainnet"
    env = {}
    if env_file.exists():
        for line in env_file.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()

    addrs = {}
    if ADDRESSES_FILE.exists():
        addrs = json.loads(ADDRESSES_FILE.read_text())

    return env, addrs


def cast_call(rpc_url: str, contract: str, sig: str, *args) -> str:
    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    cmd = [cast_bin, "call", contract, sig] + list(args) + ["--rpc-url", rpc_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_eth_balance(rpc_url: str, address: str) -> float:
    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    try:
        result = subprocess.run(
            [cast_bin, "balance", address, "--rpc-url", rpc_url, "--ether"],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip()) if result.returncode == 0 else 0.0
    except Exception:
        return 0.0


def get_token_balance(rpc_url: str, token: str, holder: str, decimals: int) -> float:
    raw = cast_call(rpc_url, token, "balanceOf(address)(uint256)", holder)
    try:
        return int(raw) / (10 ** decimals) if raw else 0.0
    except (ValueError, TypeError):
        return 0.0


def get_block_number(rpc_url: str) -> int:
    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    try:
        result = subprocess.run(
            [cast_bin, "block-number", "--rpc-url", rpc_url],
            capture_output=True, text=True, timeout=10,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


def check_db_stats() -> dict:
    """Query PostgreSQL for agent/payment stats via psql."""
    try:
        result = subprocess.run(
            ["python3", "-c", """
import asyncio
async def main():
    import json
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text
        engine = create_async_engine("postgresql+asyncpg://agio:password@localhost:5432/agio_mainnet")
        async with engine.begin() as conn:
            agents = (await conn.execute(text("SELECT COUNT(*) FROM agents"))).scalar() or 0
            payments = (await conn.execute(text("SELECT COUNT(*) FROM payments"))).scalar() or 0
            settled = (await conn.execute(text("SELECT COUNT(*) FROM payments WHERE status='SETTLED'"))).scalar() or 0
            volume = (await conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='SETTLED'"))).scalar() or 0
            queue = (await conn.execute(text("SELECT COUNT(*) FROM payments WHERE status='QUEUED'"))).scalar() or 0
            batches = (await conn.execute(text("SELECT COUNT(*) FROM batches WHERE status='SETTLED'"))).scalar() or 0
        await engine.dispose()
        print(json.dumps({"agents": agents, "payments": payments, "settled": settled,
                          "volume": float(volume), "queued": queue, "batches": batches}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
asyncio.run(main())
"""],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return {"error": "db unavailable"}


def check_redis_queue() -> int:
    try:
        result = subprocess.run(
            ["python3", "-c", "import redis; r=redis.from_url('redis://localhost:6379/1'); print(r.llen('agio:payment_queue'))"],
            capture_output=True, text=True, timeout=5,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else -1
    except Exception:
        return -1


async def monitor_loop():
    env, addrs = load_config()
    rpc_url = env.get("BASE_RPC_URL", "https://mainnet.base.org")
    deployer = env.get("DEPLOYER_ADDRESS", "")
    contracts = addrs.get("contracts", {})
    tokens = addrs.get("tokens", {})
    vault_addr = contracts.get("vault", "")

    cycle = 0
    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Clear screen
        print("\033c", end="")

        print("=" * 62)
        print(f"  AGIO MAINNET MONITOR            {now}")
        print("=" * 62)

        # Block number
        block = get_block_number(rpc_url)
        print(f"\n  NETWORK")
        print(f"    Base mainnet block:  {block:,}" if block else "    Base mainnet:  disconnected")

        # Deployer balance
        if deployer and not deployer.startswith("<"):
            eth_bal = get_eth_balance(rpc_url, deployer)
            print(f"    Deployer ETH:        {eth_bal:.6f}")

        # Vault token balances
        if vault_addr:
            print(f"\n  VAULT ({vault_addr[:18]}...)")
            for symbol, addr in tokens.items():
                decimals = TOKEN_DECIMALS.get(symbol, 6)
                bal = get_token_balance(rpc_url, addr, vault_addr, decimals)
                if decimals <= 6:
                    print(f"    {symbol:6s}  ${bal:>14,.2f}")
                else:
                    print(f"    {symbol:6s}  {bal:>14,.6f}")

            # Per-token invariant check
            print(f"\n  INVARIANT CHECK")
            for symbol, addr in tokens.items():
                raw = cast_call(rpc_url, vault_addr, "checkInvariant(address)(bool,uint256,uint256)", addr)
                if "true" in raw.lower():
                    print(f"    {symbol:6s}  OK")
                elif raw:
                    print(f"    {symbol:6s}  MISMATCH!")
                else:
                    print(f"    {symbol:6s}  (no data)")

            # Paused status
            paused_raw = cast_call(rpc_url, vault_addr, "paused()(bool)")
            if "true" in paused_raw.lower():
                print(f"\n  *** VAULT IS PAUSED ***")

        # Database stats
        db_stats = check_db_stats()
        if "error" not in db_stats:
            print(f"\n  DATABASE")
            print(f"    Agents:      {db_stats['agents']:>8,}")
            print(f"    Payments:    {db_stats['payments']:>8,}")
            print(f"    Settled:     {db_stats['settled']:>8,}")
            print(f"    Volume:      ${db_stats['volume']:>12,.4f}")
            print(f"    Batches:     {db_stats['batches']:>8,}")
        else:
            print(f"\n  DATABASE: {db_stats['error']}")

        # Redis queue
        queue = check_redis_queue()
        print(f"\n  BATCH WORKER")
        if queue >= 0:
            print(f"    Payment queue:  {queue}")
        else:
            print(f"    Redis:          disconnected")

        # Uptime
        print(f"\n  MONITOR")
        print(f"    Cycle:       {cycle}")
        print(f"    Refresh:     10s")

        print(f"\n  {'=' * 60}")
        print(f"  Ctrl+C to exit")

        await asyncio.sleep(10)


def main():
    env, addrs = load_config()

    if not addrs:
        print("No deployed_addresses.json found.")
        print("This monitor requires deployed contracts.")
        print("Run deploy_mainnet.sh --broadcast first, or create the file manually.")
        sys.exit(1)

    print("Starting AGIO mainnet monitor...")
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


if __name__ == "__main__":
    main()
