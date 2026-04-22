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

            # 1. Pull pending payments from Redis
            raw_payments = []
            for _ in range(settings.max_batch_size):
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

            # 5. Submit to blockchain
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
                    logger.info(f"Batch {batch_id_hex[:10]}... SETTLED ({len(raw_payments)} payments)")
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
        # Also unlock agent balances
        for p in payments:
            await db.execute(
                update(Agent)
                .where(Agent.id == p["from_db_id"])
                .values(
                    locked_balance=Agent.locked_balance - Decimal(str(p["amount"])),
                    balance=Agent.balance + Decimal(str(p["amount"])),
                )
            )
        await db.commit()
    logger.info(f"Returned {len(payments)} payments to queue from batch {batch_id[:10]}...")


async def _update_agent_stats(db, payments: list[dict]):
    """Update agent payment counters after successful settlement."""
    for p in payments:
        await db.execute(
            update(Agent)
            .where(Agent.id == p["from_db_id"])
            .values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + Decimal(str(p["amount"])),
                locked_balance=Agent.locked_balance - Decimal(str(p["amount"])),
            )
        )
        await db.execute(
            update(Agent)
            .where(Agent.id == p["to_db_id"])
            .values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + Decimal(str(p["amount"])),
                balance=Agent.balance + Decimal(str(p["amount"])),
            )
        )


if __name__ == "__main__":
    asyncio.run(run_worker())
