# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Wallet Monitor — Prevents accidental ETH drain and monitors all platform wallets.

Runs every 10 minutes. Checks:
1. Deployer ETH balance — alerts and pauses workers if critically low
2. Deployer USDC balance — tracks for gas auto-topup fuel
3. Vault USDC balance — ensures deposits are safe
4. Transaction rate — detects abnormal spend patterns
5. Worker health — checks if any worker is burning gas abnormally

Safety features:
- Sets Redis flag to pause batch settlement if ETH too low
- Sends email alerts on critical thresholds
- Logs all balance changes for audit trail
- Kill switch: can pause ALL outgoing transactions via Redis flag
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from decimal import Decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("wallet_monitor")

CHECK_INTERVAL = 600  # 10 minutes
BASE_RPC = os.getenv("RPC_URL", "https://mainnet.base.org")
DEPLOYER = "0xB18A31796ea51c52c203c96AaB0B1bC551C4e051"
VAULT = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "jeffrey_wylie@yahoo.com")
ETH_PRICE = 2300

# Thresholds
ETH_WARN = 0.003        # ~$7 — send warning
ETH_CRITICAL = 0.001    # ~$2.30 — pause batch settlement
ETH_EMERGENCY = 0.0003  # ~$0.70 — pause everything

# Track balance history for drain detection
_balance_history = []
_alert_cooldown = {}

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]


def _get_w3():
    from web3 import Web3
    return Web3(Web3.HTTPProvider(BASE_RPC))


async def _set_redis_flag(key, value):
    """Set a Redis flag to control worker behavior."""
    try:
        from ..core.redis import redis_client
        await redis_client.set(key, value)
        logger.info(f"Redis flag set: {key} = {value}")
    except Exception as e:
        logger.error(f"Failed to set Redis flag {key}: {e}")


async def _get_redis_flag(key):
    try:
        from ..core.redis import redis_client
        return await redis_client.get(key)
    except Exception:
        return None


def _send_alert(subject, body, level="warning"):
    """Send email alert with cooldown (max 1 per type per hour)."""
    now = time.time()
    cooldown_key = f"{level}:{subject[:30]}"
    if cooldown_key in _alert_cooldown and now - _alert_cooldown[cooldown_key] < 3600:
        return

    _alert_cooldown[cooldown_key] = now

    smtp_host = os.getenv("SMTP_HOST", "")
    if not smtp_host:
        logger.warning(f"ALERT [{level.upper()}] (no SMTP): {subject}\n{body}")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = f"[AGIOTAGE {level.upper()}] {subject}"
        msg["From"] = os.getenv("SMTP_USER", "")
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        logger.error(f"Alert email failed: {e}")


async def check_wallets():
    """Main monitoring check. Returns dict of current state."""
    from web3 import Web3
    w3 = _get_w3()
    now = datetime.utcnow()

    state = {
        "timestamp": now.isoformat(),
        "deployer_eth": 0,
        "deployer_usdc": 0,
        "vault_usdc": 0,
        "alerts": [],
        "actions_taken": [],
    }

    # 1. Deployer ETH balance
    try:
        deployer_addr = Web3.to_checksum_address(DEPLOYER)
        eth_wei = w3.eth.get_balance(deployer_addr)
        eth_bal = eth_wei / 1e18
        state["deployer_eth"] = eth_bal
        eth_usd = eth_bal * ETH_PRICE

        if eth_bal < ETH_EMERGENCY:
            state["alerts"].append(f"EMERGENCY: Deployer ETH at {eth_bal:.8f} (${eth_usd:.4f})")
            await _set_redis_flag("AGIO:payments_paused", "1")
            await _set_redis_flag("AGIO:pause_reason", f"Emergency: deployer ETH critically low ({eth_bal:.8f})")
            state["actions_taken"].append("PAUSED all payments")
            _send_alert("EMERGENCY — ETH depleted",
                f"Deployer ETH: {eth_bal:.8f} (${eth_usd:.4f})\n"
                f"All payments PAUSED.\n"
                f"Send ETH to {DEPLOYER} on Base immediately.", "critical")

        elif eth_bal < ETH_CRITICAL:
            state["alerts"].append(f"CRITICAL: Deployer ETH at {eth_bal:.6f} (${eth_usd:.2f})")
            await _set_redis_flag("AGIO:payments_paused", "1")
            await _set_redis_flag("AGIO:pause_reason", f"Critical: deployer ETH low ({eth_bal:.6f})")
            state["actions_taken"].append("PAUSED batch settlement")
            _send_alert("CRITICAL — ETH very low",
                f"Deployer ETH: {eth_bal:.6f} (${eth_usd:.2f})\n"
                f"Batch settlement PAUSED.\n"
                f"Send ETH to {DEPLOYER} on Base.", "critical")

        elif eth_bal < ETH_WARN:
            state["alerts"].append(f"WARNING: Deployer ETH at {eth_bal:.6f} (${eth_usd:.2f})")
            _send_alert("ETH balance low",
                f"Deployer ETH: {eth_bal:.6f} (${eth_usd:.2f})\n"
                f"Consider topping up {DEPLOYER} on Base.", "warning")

        else:
            # ETH is healthy — unpause if previously paused
            paused = await _get_redis_flag("AGIO:payments_paused")
            pause_reason = await _get_redis_flag("AGIO:pause_reason") or ""
            if paused == "1" and "ETH" in pause_reason:
                await _set_redis_flag("AGIO:payments_paused", "0")
                await _set_redis_flag("AGIO:pause_reason", "")
                state["actions_taken"].append("UNPAUSED payments (ETH recovered)")
                logger.info(f"ETH recovered to {eth_bal:.6f} — payments unpaused")

    except Exception as e:
        state["alerts"].append(f"Failed to check deployer ETH: {e}")
        logger.error(f"Deployer ETH check failed: {e}")

    # 2. Deployer USDC balance
    try:
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE), abi=ERC20_ABI)
        deployer_usdc = usdc.functions.balanceOf(Web3.to_checksum_address(DEPLOYER)).call() / 1e6
        state["deployer_usdc"] = deployer_usdc
    except Exception as e:
        logger.error(f"Deployer USDC check failed: {e}")

    # 3. Vault USDC balance
    try:
        vault_usdc = usdc.functions.balanceOf(Web3.to_checksum_address(VAULT)).call() / 1e6
        state["vault_usdc"] = vault_usdc
        if vault_usdc < 1.0:
            state["alerts"].append(f"Vault USDC low: ${vault_usdc:.2f}")
    except Exception as e:
        logger.error(f"Vault USDC check failed: {e}")

    # 4. Drain detection — compare to previous balance
    _balance_history.append({
        "time": time.time(),
        "eth": state["deployer_eth"],
    })
    # Keep last 24 hours of readings (144 at 10-min intervals)
    while len(_balance_history) > 144:
        _balance_history.pop(0)

    if len(_balance_history) >= 6:  # At least 1 hour of data
        hour_ago_eth = _balance_history[-6]["eth"]
        current_eth = state["deployer_eth"]
        hourly_burn = hour_ago_eth - current_eth
        if hourly_burn > 0.001:  # Burning more than $2.30/hour
            state["alerts"].append(f"DRAIN DETECTED: {hourly_burn:.6f} ETH/hour (${hourly_burn * ETH_PRICE:.2f}/hr)")
            _send_alert("Abnormal ETH drain detected",
                f"Burning {hourly_burn:.6f} ETH/hour (${hourly_burn * ETH_PRICE:.2f}/hr)\n"
                f"Current: {current_eth:.6f} ETH\n"
                f"1 hour ago: {hour_ago_eth:.6f} ETH\n"
                f"Check rebalancer and batch worker for issues.", "critical")

    # Log summary
    logger.info(
        f"Wallet check: deployer={state['deployer_eth']:.6f} ETH "
        f"(${state['deployer_eth'] * ETH_PRICE:.2f}), "
        f"USDC=${state['deployer_usdc']:.2f}, "
        f"vault=${state['vault_usdc']:.2f}"
        f"{' | ALERTS: ' + ', '.join(state['alerts']) if state['alerts'] else ' | OK'}"
    )

    return state


async def run_monitor():
    """Main loop."""
    logger.info(f"Wallet monitor started. Interval: {CHECK_INTERVAL}s")
    logger.info(f"Thresholds: warn={ETH_WARN} ETH, critical={ETH_CRITICAL}, emergency={ETH_EMERGENCY}")
    logger.info(f"Deployer: {DEPLOYER}")
    logger.info(f"Vault: {VAULT}")

    # Initialize Redis connection
    try:
        from ..core.config import settings
        import redis.asyncio as aioredis
        from ..core import redis as redis_mod
        redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception as e:
        logger.warning(f"Redis init: {e}")

    while True:
        try:
            await check_wallets()
        except Exception as e:
            logger.error(f"Wallet monitor error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_monitor())
