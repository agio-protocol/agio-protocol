# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Reserve Rebalancer — Maintains USDC reserves across chains via Circle CCTP V2.

Two jobs running every 5 minutes:
  1. Cross-chain reserve rebalancing (CCTP: free bridge, only gas)
  2. Gas wallet top-up (swap small USDC → ETH via Uniswap when gas runs low)

CCTP flow: burn on source → Circle attestation (~20 min) → mint on dest.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import async_session
from ..models.chain import SupportedChain

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("rebalancer")

REBALANCE_INTERVAL = 300  # 5 minutes
LOW_THRESHOLD = 0.5       # rebalance when below 50% of target
HIGH_THRESHOLD = 2.0      # chain has excess when above 200% of target

# ----- CCTP V2 Mainnet Addresses (same across all EVM via CREATE2) -----
CCTP_TOKEN_MESSENGER = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"
CCTP_MESSAGE_TRANSMITTER = "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64"

CCTP_DOMAINS = {
    "base-mainnet": 6,
    "ethereum-mainnet": 0,
    "solana-mainnet": 5,
}

# ----- Base Mainnet Addresses -----
BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
UNISWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"  # SwapRouter02
DEPLOYER = "0xB18A31796ea51c52c203c96AaB0B1bC551C4e051"

# Gas thresholds
GAS_LOW_ETH = 0.002       # trigger USDC→ETH swap below this
GAS_TARGET_ETH = 0.005    # swap enough to reach this level
GAS_SWAP_MAX_USDC = 15.0  # never swap more than $15 at once

# Minimal ABIs
ERC20_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]

TOKEN_MESSENGER_ABI = [
    {"inputs":[{"name":"amount","type":"uint256"},{"name":"destinationDomain","type":"uint32"},{"name":"mintRecipient","type":"bytes32"},{"name":"burnToken","type":"address"}],"name":"depositForBurn","outputs":[{"type":"uint64"}],"stateMutability":"nonpayable","type":"function"},
]

SWAP_ROUTER_ABI = [
    {"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"deadline","type":"uint256"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"type":"uint256"}],"stateMutability":"payable","type":"function"},
]

ALERT_EMAIL = os.getenv("ALERT_EMAIL", "jeffrey_wylie@yahoo.com")


def _get_w3():
    from web3 import Web3
    return Web3(Web3.HTTPProvider(BASE_RPC))


def _get_account():
    key = settings.get_batch_submitter_key()
    if not key:
        return None
    from web3 import Web3
    w3 = _get_w3()
    return w3.eth.account.from_key(key)


# ──────────────────────────────────────────────
# 1. Cross-chain reserve rebalancing via CCTP
# ──────────────────────────────────────────────

async def check_and_rebalance_reserves():
    """Check reserve ratios and initiate CCTP transfer if needed."""
    async with async_session() as db:
        chains = (await db.execute(
            select(SupportedChain).where(SupportedChain.is_active == True)
        )).scalars().all()

        chain_data = [{
            "obj": c,
            "name": c.chain_name,
            "reserve": float(c.reserve_balance),
            "target": float(c.min_reserve),
            "ratio": float(c.reserve_balance) / max(float(c.min_reserve), 0.01),
        } for c in chains]

        for chain in chain_data:
            if chain["ratio"] >= LOW_THRESHOLD:
                continue

            deficit = chain["target"] - chain["reserve"]
            surplus_chain = None
            surplus_amount = 0

            for other in chain_data:
                if other["name"] == chain["name"]:
                    continue
                excess = other["reserve"] - other["target"]
                if excess > surplus_amount:
                    surplus_chain = other
                    surplus_amount = excess

            if not surplus_chain or surplus_amount <= 0:
                logger.warning(f"No surplus to rebalance {chain['name']} (at {chain['ratio']:.0%})")
                continue

            transfer_amount = min(deficit, surplus_amount * 0.5)
            if transfer_amount < 1.0:
                continue

            logger.info(
                f"Rebalancing: {surplus_chain['name']} → {chain['name']}, "
                f"${transfer_amount:.2f} USDC (reserve at {chain['ratio']:.0%})"
            )

            result = await initiate_cctp_transfer(
                surplus_chain["name"], chain["name"], transfer_amount
            )

            if result.get("status") == "sent":
                await db.execute(
                    update(SupportedChain)
                    .where(SupportedChain.chain_name == chain["name"])
                    .values(reserve_balance=SupportedChain.reserve_balance + Decimal(str(transfer_amount)))
                )
                await db.execute(
                    update(SupportedChain)
                    .where(SupportedChain.chain_name == surplus_chain["name"])
                    .values(reserve_balance=SupportedChain.reserve_balance - Decimal(str(transfer_amount)))
                )
                await db.commit()
                logger.info(f"Reserve update committed: +${transfer_amount:.2f} to {chain['name']}")
            else:
                logger.warning(f"CCTP transfer failed: {result}")


async def initiate_cctp_transfer(from_chain: str, to_chain: str, amount: float) -> dict:
    """Burn USDC on source chain via CCTP V2. Circle mints on destination after attestation."""
    from_domain = CCTP_DOMAINS.get(from_chain)
    to_domain = CCTP_DOMAINS.get(to_chain)

    if from_domain is None or to_domain is None:
        return {"status": "skipped", "reason": f"CCTP domain not configured for {from_chain} or {to_chain}"}

    # Only Base→other EVM transfers are implemented (we control the Base deployer key)
    if from_chain != "base-mainnet":
        logger.info(f"CCTP from {from_chain} not yet automated — logged for manual action")
        return {"status": "manual", "reason": "Only Base outbound automated", "amount": amount}

    account = _get_account()
    if not account:
        return {"status": "skipped", "reason": "No batch submitter key configured"}

    try:
        from web3 import Web3
        w3 = _get_w3()

        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE), abi=ERC20_ABI)
        messenger = w3.eth.contract(address=Web3.to_checksum_address(CCTP_TOKEN_MESSENGER), abi=TOKEN_MESSENGER_ABI)

        amount_raw = int(amount * 1e6)  # USDC has 6 decimals

        # Check deployer USDC balance
        balance = usdc.functions.balanceOf(account.address).call()
        if balance < amount_raw:
            return {"status": "skipped", "reason": f"Insufficient USDC: {balance / 1e6:.2f} < {amount:.2f}"}

        # Check allowance, approve if needed
        allowance = usdc.functions.allowance(account.address, Web3.to_checksum_address(CCTP_TOKEN_MESSENGER)).call()
        if allowance < amount_raw:
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(CCTP_TOKEN_MESSENGER), 2**256 - 1
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 60000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.eth.gas_price,
            })
            signed = account.sign_transaction(approve_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            logger.info(f"CCTP approval tx: {tx_hash.hex()}")

        # Encode destination address as bytes32 (for EVM, left-pad 20-byte address)
        # For Solana, this would be the 32-byte vault PDA
        if to_chain == "solana-mainnet":
            import base58
            sol_vault = "3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ"
            mint_recipient = base58.b58decode(sol_vault)
            mint_recipient = mint_recipient.rjust(32, b'\x00')
        else:
            mint_recipient = bytes(12) + bytes.fromhex(DEPLOYER[2:])

        # depositForBurn
        burn_tx = messenger.functions.depositForBurn(
            amount_raw,
            to_domain,
            mint_recipient,
            Web3.to_checksum_address(USDC_BASE),
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 200000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.eth.gas_price,
        })
        signed = account.sign_transaction(burn_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            logger.info(f"CCTP burn success: {tx_hash.hex()}, ${amount:.2f} → domain {to_domain}")
            return {"status": "sent", "tx_hash": tx_hash.hex(), "amount": amount, "dest_domain": to_domain}
        else:
            logger.error(f"CCTP burn reverted: {tx_hash.hex()}")
            return {"status": "reverted", "tx_hash": tx_hash.hex()}

    except Exception as e:
        logger.error(f"CCTP transfer error: {e}", exc_info=True)
        return {"status": "error", "error": str(e)[:200]}


# ──────────────────────────────────────────────
# 2. Gas wallet ETH top-up via Uniswap
# ──────────────────────────────────────────────

async def check_and_topup_gas():
    """If deployer ETH is low, swap a small amount of USDC → ETH via Uniswap."""
    account = _get_account()
    if not account:
        return

    try:
        from web3 import Web3
        w3 = _get_w3()

        eth_balance = w3.eth.get_balance(account.address) / 1e18

        if eth_balance >= GAS_LOW_ETH:
            logger.debug(f"Gas wallet OK: {eth_balance:.6f} ETH (${eth_balance * 2300:.2f})")
            return

        eth_needed = GAS_TARGET_ETH - eth_balance
        eth_price = 2300  # conservative estimate
        usdc_needed = min(eth_needed * eth_price * 1.05, GAS_SWAP_MAX_USDC)  # 5% buffer for slippage

        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE), abi=ERC20_ABI)
        usdc_balance = usdc.functions.balanceOf(account.address).call() / 1e6

        if usdc_balance < usdc_needed:
            logger.warning(f"Gas top-up needed but insufficient USDC: ${usdc_balance:.2f} < ${usdc_needed:.2f}")
            _send_gas_alert(eth_balance, usdc_balance)
            return

        logger.info(f"Gas low ({eth_balance:.6f} ETH). Swapping ${usdc_needed:.2f} USDC → ETH")

        router = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_ROUTER), abi=SWAP_ROUTER_ABI)
        amount_in = int(usdc_needed * 1e6)

        # Check/set allowance for Uniswap router
        allowance = usdc.functions.allowance(account.address, Web3.to_checksum_address(UNISWAP_ROUTER)).call()
        if allowance < amount_in:
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(UNISWAP_ROUTER), 2**256 - 1
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 60000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.eth.gas_price,
            })
            signed = account.sign_transaction(approve_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            logger.info(f"Uniswap USDC approval: {tx_hash.hex()}")

        # Swap USDC → WETH → unwrap (Uniswap sends ETH if recipient is EOA via WETH)
        min_out = int(eth_needed * 0.95 * 1e18)  # 5% slippage tolerance
        import time
        swap_params = (
            Web3.to_checksum_address(USDC_BASE),      # tokenIn
            Web3.to_checksum_address(WETH_BASE),       # tokenOut
            500,                                        # fee tier (0.05% — best for USDC/ETH)
            account.address,                            # recipient
            int(time.time()) + 300,                     # deadline
            amount_in,                                  # amountIn
            min_out,                                    # amountOutMinimum
            0,                                          # sqrtPriceLimitX96
        )

        swap_tx = router.functions.exactInputSingle(swap_params).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 200000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.eth.gas_price,
            "value": 0,
        })
        signed = account.sign_transaction(swap_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            new_balance = w3.eth.get_balance(account.address) / 1e18
            logger.info(f"Gas top-up success: {tx_hash.hex()}, ETH now {new_balance:.6f}")
        else:
            logger.error(f"Gas swap reverted: {tx_hash.hex()}")

    except Exception as e:
        logger.error(f"Gas top-up error: {e}", exc_info=True)


def _send_gas_alert(eth_balance: float, usdc_balance: float):
    """Email alert when gas is low and auto-topup can't cover it."""
    import smtplib
    from email.mime.text import MIMEText
    smtp_host = os.getenv("SMTP_HOST", "")
    if not smtp_host:
        logger.warning(f"GAS ALERT (no SMTP): {eth_balance:.6f} ETH, ${usdc_balance:.2f} USDC available")
        return
    try:
        body = (
            f"Agiotage deployer gas wallet is low and auto-topup cannot cover it.\n\n"
            f"ETH balance: {eth_balance:.6f} ETH (${eth_balance * 2300:.2f})\n"
            f"USDC available: ${usdc_balance:.2f}\n\n"
            f"Action needed: send ETH to {DEPLOYER} on Base mainnet,\n"
            f"or send USDC to the deployer so auto-topup can swap it.\n"
        )
        msg = MIMEText(body)
        msg["Subject"] = "[AGIOTAGE] Gas wallet critically low"
        msg["From"] = os.getenv("SMTP_USER", "")
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        logger.error(f"Gas alert email failed: {e}")


# ──────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────

async def run_rebalancer():
    """Main loop — runs both reserve rebalancing and gas top-up every 5 minutes."""
    logger.info(f"Rebalancer started. Interval: {REBALANCE_INTERVAL}s. Gas low: {GAS_LOW_ETH} ETH")

    while True:
        try:
            await check_and_rebalance_reserves()
        except Exception as e:
            logger.error(f"Reserve rebalance error: {e}", exc_info=True)

        try:
            await check_and_topup_gas()
        except Exception as e:
            logger.error(f"Gas top-up error: {e}", exc_info=True)

        await asyncio.sleep(REBALANCE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_rebalancer())
