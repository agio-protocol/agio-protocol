# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Solana Batch Worker — settles payments on the Solana AGIO vault.

Same pattern as the Base batch worker but uses solders/solana-py
for transaction construction and Ed25519 signing.

Max 5 payments per transaction (Solana 1232 byte tx limit).
Larger queues are split into multiple batches.
"""
import asyncio
import json
import hashlib
import struct
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from web3 import Web3  # just for keccak, not EVM interaction

from ..core.config import settings
from ..core.database import async_session
from ..core.redis import redis_client
from ..models.payment import Payment
from ..models.batch import Batch
from ..models.agent import Agent, AgentBalance

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("solana_batch_worker")

SOLANA_PAYMENT_QUEUE = "agio:solana_payment_queue"
MAX_BATCH_SIZE = 5  # Solana tx size limit
BATCH_INTERVAL = 60

# Solana program constants
PROGRAM_ID = None  # Set from config
SOLANA_RPC = None


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def _encode_payment(from_pk: bytes, to_pk: bytes, amount: int, mint: bytes, payment_id: bytes, fee: int) -> bytes:
    return from_pk + to_pk + struct.pack("<Q", amount) + mint + payment_id + struct.pack("<Q", fee)


def _encode_settle_batch(batch_id: bytes, payments_encoded: list[bytes]) -> bytes:
    data = _disc("settle_batch")
    data += batch_id
    data += struct.pack("<I", len(payments_encoded))
    for p in payments_encoded:
        data += p
    return data


async def submit_solana_batch(payments: list[dict], batch_id_bytes: bytes) -> str:
    """Submit a batch to the Solana AGIO vault program."""
    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.system_program import ID as SYS_PROGRAM
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.types import TxOpts
    except ImportError:
        logger.error("solders/solana not installed — pip install solders solana")
        return ""

    program_id = Pubkey.from_string(PROGRAM_ID)
    signer_key = settings.get_batch_signer_key()
    if not signer_key:
        logger.warning("No Solana batch signer key — skipping")
        return ""

    # Load signer keypair
    if signer_key.startswith("0x"):
        signer_key = signer_key[2:]
    signer = Keypair.from_bytes(bytes.fromhex(signer_key.ljust(128, '0'))[:64])

    vault_pda = Pubkey.find_program_address([b"vault"], program_id)[0]
    batch_pda = Pubkey.find_program_address([b"batch", batch_id_bytes], program_id)[0]

    # Encode payments
    encoded = []
    metas = [
        AccountMeta(vault_pda, is_signer=False, is_writable=True),
        AccountMeta(batch_pda, is_signer=False, is_writable=True),
        AccountMeta(signer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(SYS_PROGRAM, is_signer=False, is_writable=False),
    ]

    for p in payments:
        from_pk = Pubkey.from_string(p["from_wallet"])
        to_pk = Pubkey.from_string(p["to_wallet"])
        mint = Pubkey.from_string(p.get("token_mint", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        amount = int(float(p["amount"]) * 1_000_000)  # 6 decimals
        fee = int(float(p.get("fee", "0")) * 1_000_000)
        pid = hashlib.sha256(p["payment_id"].encode()).digest()

        encoded.append(_encode_payment(bytes(from_pk), bytes(to_pk), amount, bytes(mint), pid, fee))

        sender_pda = Pubkey.find_program_address([b"agent", bytes(from_pk)], program_id)[0]
        receiver_pda = Pubkey.find_program_address([b"agent", bytes(to_pk)], program_id)[0]
        metas.append(AccountMeta(sender_pda, is_signer=False, is_writable=True))
        metas.append(AccountMeta(receiver_pda, is_signer=False, is_writable=True))

    ix_data = _encode_settle_batch(batch_id_bytes, encoded)
    ix = Instruction(program_id, ix_data, metas)

    async with AsyncClient(SOLANA_RPC) as client:
        blockhash = (await client.get_latest_blockhash()).value.blockhash
        tx = Transaction.new_signed_with_payer([ix], signer.pubkey(), [signer], blockhash)
        result = await client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
        sig = str(result.value)
        logger.info(f"Solana batch submitted: {sig[:20]}...")
        return sig


def _estimate_solana_gas(num_payments: int) -> float:
    """Estimate Solana transaction cost in USD."""
    base_fee = 0.000005  # 5000 lamports
    priority_fee = 0.00005
    sol_price = 150  # approximate
    return (base_fee + priority_fee) * sol_price


async def run_worker():
    """Main Solana batch worker loop."""
    logger.info(f"Solana batch worker started. Max batch: {MAX_BATCH_SIZE}, interval: {BATCH_INTERVAL}s")

    while True:
        try:
            queue_depth = await redis_client.llen(SOLANA_PAYMENT_QUEUE)

            if queue_depth == 0:
                await asyncio.sleep(BATCH_INTERVAL)
                continue

            # Pull payments (max 5 per batch)
            raw_payments = []
            for _ in range(MAX_BATCH_SIZE):
                item = await redis_client.lpop(SOLANA_PAYMENT_QUEUE)
                if item is None:
                    break
                raw_payments.append(json.loads(item))

            if not raw_payments:
                continue

            logger.info(f"Processing Solana batch: {len(raw_payments)} payments (queue={queue_depth})")

            # Generate batch ID
            content = json.dumps(sorted([p["payment_id"] for p in raw_payments]) + [str(datetime.utcnow().timestamp())])
            batch_id = hashlib.sha256(content.encode()).digest()
            batch_id_hex = "0x" + batch_id.hex()

            # Profitability check
            total_fees = sum(Decimal(str(p.get("fee", "0"))) for p in raw_payments)
            est_gas = _estimate_solana_gas(len(raw_payments))
            is_profitable = float(total_fees) >= est_gas
            if not is_profitable:
                logger.info(f"Subsidized Solana batch: fees=${float(total_fees):.6f}, gas=${est_gas:.6f}")

            # Record batch in DB
            async with async_session() as db:
                batch_record = Batch(
                    batch_id=batch_id_hex,
                    total_payments=len(raw_payments),
                    total_volume=sum(Decimal(str(p["amount"])) for p in raw_payments),
                    status="SETTLING",
                    submitted_at=datetime.utcnow(),
                )
                db.add(batch_record)
                for p in raw_payments:
                    await db.execute(
                        update(Payment)
                        .where(Payment.payment_id == p["payment_id"])
                        .values(status="BATCHED", batch_id=batch_id_hex)
                    )
                await db.commit()

            # Submit to Solana
            try:
                tx_hash = await submit_solana_batch(raw_payments, batch_id)
                if not tx_hash:
                    logger.error("Solana submission returned empty — retrying")
                    await _return_to_queue(raw_payments, batch_id_hex)
                    continue
            except Exception as e:
                logger.error(f"Solana submission failed: {e}")
                await _return_to_queue(raw_payments, batch_id_hex)
                await asyncio.sleep(BATCH_INTERVAL)
                continue

            # Record success
            async with async_session() as db:
                await db.execute(
                    update(Batch)
                    .where(Batch.batch_id == batch_id_hex)
                    .values(status="SETTLED", tx_hash=tx_hash, settled_at=datetime.utcnow())
                )
                for p in raw_payments:
                    await db.execute(
                        update(Payment)
                        .where(Payment.payment_id == p["payment_id"])
                        .values(status="SETTLED", settled_at=datetime.utcnow())
                    )
                await _update_agent_stats(db, raw_payments)
                await db.commit()

            logger.info(
                f"Solana batch SETTLED ({len(raw_payments)} payments, "
                f"fees=${float(total_fees):.6f}, gas~${est_gas:.6f}, "
                f"{'PROFITABLE' if is_profitable else 'SUBSIDIZED'})"
            )

        except Exception as e:
            logger.error(f"Solana worker error: {e}", exc_info=True)

        await asyncio.sleep(BATCH_INTERVAL)


async def _return_to_queue(payments, batch_id_hex):
    """Return failed payments to Solana queue."""
    async with async_session() as db:
        for p in payments:
            await redis_client.rpush(SOLANA_PAYMENT_QUEUE, json.dumps(p))
            await db.execute(
                update(Payment)
                .where(Payment.payment_id == p["payment_id"])
                .values(status="QUEUED", batch_id=None)
            )
        await db.execute(
            update(Batch)
            .where(Batch.batch_id == batch_id_hex)
            .values(status="FAILED")
        )
        await db.commit()
    logger.info(f"Returned {len(payments)} Solana payments to queue")


async def _update_agent_stats(db, payments):
    """Update agent stats after Solana settlement."""
    for p in payments:
        amount = Decimal(str(p["amount"]))
        fee = Decimal(str(p.get("fee", "0")))
        total_debit = amount + fee

        await db.execute(
            update(Agent).where(Agent.id == p["from_db_id"]).values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + amount,
                locked_balance=Agent.locked_balance - total_debit,
            )
        )
        await db.execute(
            update(Agent).where(Agent.id == p["to_db_id"]).values(
                total_payments=Agent.total_payments + 1,
                total_volume=Agent.total_volume + amount,
                balance=Agent.balance + amount,
            )
        )

        if float(fee) > 0:
            logger.info(f"Solana fee: ${float(fee):.6f} from {p['payment_id'][:16]}...")

    # Award points
    try:
        from ..services.points_service import award_points
        for p in payments:
            if p.get("from_agio_id"):
                await award_points(db, p["from_agio_id"], "payment_sent", 1)
            if p.get("to_agio_id"):
                await award_points(db, p["to_agio_id"], "payment_received", 1)
    except Exception as e:
        logger.warning(f"Points award failed: {e}")


import os
PROGRAM_ID = os.getenv("SOLANA_PROGRAM_ID", "68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y")
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

if __name__ == "__main__":
    asyncio.run(run_worker())
