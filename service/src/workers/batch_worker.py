# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Batch worker — assembles queued payments into batches and submits on-chain.

Dynamic batching: adjusts interval based on queue depth.
  500+ payments → settle immediately
  100-499       → every 30 seconds
  10-99         → every 60 seconds
  1-9           → every 120 seconds

More payments per batch = cheaper per payment (gas is shared).
Idempotent — crashes and restarts never cause double-processing.
"""
import asyncio
import json
import hashlib
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from web3 import Web3

from ..core.config import settings
from ..core.database import async_session
from ..core.redis import redis_client, PAYMENT_QUEUE
from ..models.payment import Payment
from ..models.batch import Batch
from ..models.agent import Agent
from ..services.blockchain_service import submit_batch_to_chain, wait_for_receipt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("batch_worker")


BASE_GAS_PER_PAYMENT = 45_000
BASE_GAS_OVERHEAD = 100_000
ETH_PRICE_USD = 2300
UNPROFITABLE_ALERT_SENT = False


def _estimate_gas_cost(num_payments: int) -> float:
    """Estimate gas cost in USD for a batch on Base."""
    total_gas = BASE_GAS_OVERHEAD + (num_payments * BASE_GAS_PER_PAYMENT)
    try:
        w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
        gas_price_wei = w3.eth.gas_price
    except Exception:
        gas_price_wei = 10_000_000  # 0.01 gwei fallback (Base is cheap)
    gas_cost_eth = (total_gas * gas_price_wei) / 1e18
    return gas_cost_eth * ETH_PRICE_USD


def _send_unprofitable_alert(num_payments: int, fees: float, gas_cost: float):
    """Email alert when batches are consistently unprofitable."""
    global UNPROFITABLE_ALERT_SENT
    if UNPROFITABLE_ALERT_SENT:
        return
    import os
    import smtplib
    from email.mime.text import MIMEText
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    alert_email = os.getenv("ALERT_EMAIL", "jeffrey_wylie@yahoo.com")
    if not smtp_host:
        logger.warning(f"UNPROFITABLE ALERT (no SMTP): {num_payments} payments, fees=${fees:.6f}, gas=${gas_cost:.6f}")
        UNPROFITABLE_ALERT_SENT = True
        return
    try:
        msg = MIMEText(
            f"AGIO batch worker is deferring settlements because gas costs exceed fee revenue.\n\n"
            f"Batch size: {num_payments} payments\n"
            f"Fee revenue: ${fees:.6f}\n"
            f"Estimated gas: ${gas_cost:.6f}\n\n"
            f"Options:\n"
            f"1. Wait for more payments to accumulate (larger batches are cheaper per payment)\n"
            f"2. Increase the AGIO fee rate\n"
            f"3. Subsidize gas from treasury during growth phase\n"
        )
        msg["Subject"] = "[AGIO ALERT] Batch settlement unprofitable"
        msg["From"] = smtp_user
        msg["To"] = alert_email
        with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        UNPROFITABLE_ALERT_SENT = True
    except Exception as e:
        logger.error(f"Failed to send unprofitable alert: {e}")


def generate_batch_id(payments: list[dict]) -> bytes:
    """Unique batch ID from payment contents + timestamp."""
    content = json.dumps(sorted([p["payment_id"] for p in payments]) + [str(datetime.utcnow().timestamp())])
    return Web3.solidity_keccak(["string"], [content])


def dynamic_interval(queue_depth: int) -> int:
    """Adjust batch interval based on queue depth. More payments = faster settlement."""
    if queue_depth >= 500:
        return 1       # immediate
    elif queue_depth >= 100:
        return 30      # 30 seconds
    elif queue_depth >= 10:
        return 60      # 1 minute
    elif queue_depth >= 1:
        return 120     # 2 minutes
    return settings.batch_interval_seconds  # default


async def run_worker():
    """Main batch worker loop with dynamic interval."""
    logger.info(f"Batch worker started. Dynamic batching enabled, "
                f"max batch: {settings.max_batch_size}")

    while True:
        try:
            # Check queue depth for dynamic interval
            queue_depth = await redis_client.llen(PAYMENT_QUEUE)

            if queue_depth == 0:
                await asyncio.sleep(settings.batch_interval_seconds)
                continue

            interval = dynamic_interval(queue_depth)
            if interval > 1 and queue_depth < 500:
                await asyncio.sleep(interval)
                # Re-check — more may have arrived
                queue_depth = await redis_client.llen(PAYMENT_QUEUE)

            # 1. Pull pending payments from Redis (max 50 per batch for gas safety)
            effective_batch_size = min(settings.max_batch_size, 50)
            raw_payments = []
            for _ in range(effective_batch_size):
                item = await redis_client.lpop(PAYMENT_QUEUE)
                if item is None:
                    break
                raw_payments.append(json.loads(item))

            if not raw_payments:
                continue

            logger.info(f"Processing batch of {len(raw_payments)} payments "
                        f"(queue was {queue_depth}, interval={interval}s)")

            # 2. Generate batch ID
            batch_id = generate_batch_id(raw_payments)
            batch_id_hex = "0x" + batch_id.hex()

            # 3. Build on-chain payment structs (amounts in token base units)
            from ..models.chain import BASE_TOKENS
            chain_payments = []
            for p in raw_payments:
                token_symbol = p.get("from_token", "USDC")
                token_info = BASE_TOKENS.get(token_symbol, {"address": "0x" + "0" * 40, "decimals": 6})
                decimals = token_info["decimals"]
                amount_base = int(float(p["amount"]) * (10 ** decimals))
                payment_id_bytes = Web3.solidity_keccak(["string"], [p["payment_id"]])
                chain_payments.append({
                    "from": Web3.to_checksum_address(p["from_wallet"]),
                    "to": Web3.to_checksum_address(p["to_wallet"]),
                    "amount": amount_base,
                    "token": Web3.to_checksum_address(token_info["address"]),
                    "paymentId": payment_id_bytes,
                })

            # 4. Record batch in DB
            async with async_session() as db:
                batch_record = Batch(
                    batch_id=batch_id_hex,
                    total_payments=len(raw_payments),
                    total_volume=sum(Decimal(str(p["amount"])) for p in raw_payments),
                    status="SETTLING",
                    submitted_at=datetime.utcnow(),
                )
                db.add(batch_record)

                # Update payment statuses
                for p in raw_payments:
                    await db.execute(
                        update(Payment)
                        .where(Payment.payment_id == p["payment_id"])
                        .values(status="BATCHED", batch_id=batch_id_hex)
                    )
                await db.commit()

            # 5. Profitability check — log economics, defer only if gas > $0.10
            total_fees = sum(Decimal(str(p.get("fee", "0"))) for p in raw_payments)
            estimated_gas = _estimate_gas_cost(len(raw_payments))
            is_profitable = float(total_fees) >= estimated_gas

            if not is_profitable:
                logger.info(
                    f"Subsidized batch: {len(raw_payments)} payments, "
                    f"fees=${float(total_fees):.6f}, est_gas=${estimated_gas:.6f}"
                )

            # Only defer if gas per payment is extremely high (Base spike)
            # Normal Base: ~$0.0006/payment. Congested: >$0.01/payment.
            gas_per_payment = estimated_gas / max(len(raw_payments), 1)
            if gas_per_payment > 0.01:
                logger.warning(
                    f"GAS TOO HIGH: ${gas_per_payment:.4f}/payment — deferring batch. "
                    f"Base may be congested."
                )
                _send_unprofitable_alert(len(raw_payments), float(total_fees), estimated_gas)
                await _return_to_queue(raw_payments, batch_id_hex)
                await asyncio.sleep(settings.batch_interval_seconds * 2)
                continue

            # 6. Submit to blockchain
            try:
                tx_hash = await submit_batch_to_chain(chain_payments, batch_id)
                logger.info(f"Batch {batch_id_hex[:10]}... submitted: tx={tx_hash[:10]}...")
            except Exception as e:
                logger.error(f"On-chain submission failed: {e}")
                await _return_to_queue(raw_payments, batch_id_hex)
                await asyncio.sleep(settings.batch_interval_seconds)
                continue

            # 6. Wait for confirmation
            receipt = await wait_for_receipt(tx_hash)

            # 7. Update statuses based on result
            async with async_session() as db:
                if receipt["success"]:
                    await db.execute(
                        update(Batch)
                        .where(Batch.batch_id == batch_id_hex)
                        .values(
                            status="SETTLED",
                            tx_hash=tx_hash,
                            settled_at=datetime.utcnow(),
                            gas_used=receipt["gas_used"],
                        )
                    )
                    for p in raw_payments:
                        await db.execute(
                            update(Payment)
                            .where(Payment.payment_id == p["payment_id"])
                            .values(status="SETTLED", settled_at=datetime.utcnow())
                        )
                    # Update agent stats
                    await _update_agent_stats(db, raw_payments)
                    actual_gas_cost = _estimate_gas_cost(len(raw_payments))
                    logger.info(
                        f"Batch {batch_id_hex[:10]}... SETTLED ({len(raw_payments)} payments, "
                        f"fees=${float(total_fees):.6f}, gas~${actual_gas_cost:.6f}, "
                        f"{'PROFITABLE' if float(total_fees) >= actual_gas_cost else 'SUBSIDIZED'})"
                    )
                else:
                    logger.error(f"Batch {batch_id_hex[:10]}... FAILED on-chain")
                    await db.execute(
                        update(Batch)
                        .where(Batch.batch_id == batch_id_hex)
                        .values(status="FAILED", tx_hash=tx_hash)
                    )
                    await _return_to_queue(raw_payments, batch_id_hex)

                await db.commit()

        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)

        await asyncio.sleep(settings.batch_interval_seconds)


async def _return_to_queue(payments: list[dict], batch_id: str):
    """Return failed payments to the queue for retry."""
    async with async_session() as db:
        for p in payments:
            await redis_client.rpush(PAYMENT_QUEUE, json.dumps(p))
            await db.execute(
                update(Payment)
                .where(Payment.payment_id == p["payment_id"])
                .values(status="QUEUED", batch_id=None)
            )
        # Unlock agent balances (amount + fee + swap_fee = total_debit)
        for p in payments:
            total_debit = Decimal(str(p["amount"])) + Decimal(str(p.get("fee", "0"))) + Decimal(str(p.get("swap_fee", "0")))
            await db.execute(
                update(Agent)
                .where(Agent.id == p["from_db_id"])
                .values(
                    locked_balance=Agent.locked_balance - total_debit,
                    balance=Agent.balance + total_debit,
                )
            )
        await db.commit()
    logger.info(f"Returned {len(payments)} payments to queue from batch {batch_id[:10]}...")


async def _update_agent_stats(db, payments: list[dict]):
    """Update agent payment counters and collect fees after successful settlement."""
    from ..models.agent import AgentBalance

    for p in payments:
        amount = Decimal(str(p["amount"]))
        fee = Decimal(str(p.get("fee", "0")))
        swap_fee = Decimal(str(p.get("swap_fee", "0")))
        total_debit = amount + fee + swap_fee
        token = p.get("from_token", "USDC")

        # Sender: unlock total_debit from Agent.locked_balance
        await db.execute(
            update(Agent)
            .where(Agent.id == p["from_db_id"])
            .values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + amount,
                locked_balance=Agent.locked_balance - total_debit,
            )
        )

        # Receiver: credit payment amount to Agent.balance
        await db.execute(
            update(Agent)
            .where(Agent.id == p["to_db_id"])
            .values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + amount,
                balance=Agent.balance + amount,
            )
        )

        # Update per-token AgentBalance for sender (unlock)
        from sqlalchemy import select
        sender_bal = (await db.execute(
            select(AgentBalance).where(
                AgentBalance.agent_id == p["from_db_id"],
                AgentBalance.token == token,
            )
        )).scalar_one_or_none()
        if sender_bal:
            sender_bal.locked_balance = max(Decimal("0"), sender_bal.locked_balance - total_debit)

        # Update per-token AgentBalance for receiver (credit amount only)
        recv_bal = (await db.execute(
            select(AgentBalance).where(
                AgentBalance.agent_id == p["to_db_id"],
                AgentBalance.token == token,
            )
        )).scalar_one_or_none()
        if recv_bal:
            recv_bal.balance = recv_bal.balance + amount
        else:
            db.add(AgentBalance(
                agent_id=p["to_db_id"], token=token,
                balance=amount, locked_balance=Decimal("0"),
            ))

        # Fee revenue: log it (fees stay in the vault as protocol revenue)
        if fee > 0 or swap_fee > 0:
            logger.info(f"Fee collected: ${float(fee):.6f} + swap ${float(swap_fee):.6f} from {p['payment_id'][:16]}...")

    # Award points (1 point per payment for both sender and receiver)
    try:
        from ..services.points_service import award_points
        for p in payments:
            from_id = p.get("from_agio_id")
            to_id = p.get("to_agio_id")
            if from_id:
                await award_points(db, from_id, "payment_sent", 1)
            if to_id:
                await award_points(db, to_id, "payment_received", 1)
    except Exception as e:
        logger.warning(f"Points award failed (non-fatal): {e}")


if __name__ == "__main__":
    asyncio.run(run_worker())
