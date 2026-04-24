# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Payment service — validates and queues payments with multi-token support."""
import json
import uuid
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent, AgentBalance
from ..models.payment import Payment
from ..models.chain import SWAP_FEE_BPS
from ..core.redis import redis_client, PAYMENT_QUEUE
from ..core.exceptions import InsufficientBalance, AgentNotFound

MAX_MEMO_LENGTH = 500

SUPPORTED_TOKENS = {"USDC", "USDT", "DAI", "WETH", "cbETH"}
DEFAULT_TOKEN = "USDC"


def _sanitize_memo(memo: str | None) -> str | None:
    """Enforce memo limits: max 500 chars, UTF-8, no null bytes."""
    if memo is None:
        return None
    memo = memo.replace("\x00", "")
    memo = memo.encode("utf-8", errors="replace").decode("utf-8")
    return memo[:MAX_MEMO_LENGTH]


def _amount_to_str(amount: float | Decimal) -> str:
    """Convert amount to string for Redis — avoids float precision issues."""
    return str(Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))


def _calculate_swap_fee(amount: Decimal) -> Decimal:
    """Calculate 0.3% swap fee for cross-token payments."""
    return (amount * SWAP_FEE_BPS / 10000).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


async def _get_or_create_balance(
    db: AsyncSession, agent_id, token: str
):
    """Get or create an AgentBalance row for a given agent+token."""
    bal = (await db.execute(
        select(AgentBalance)
        .where(AgentBalance.agent_id == agent_id, AgentBalance.token == token)
        .with_for_update()
    )).scalar_one_or_none()

    if bal is None:
        bal = AgentBalance(agent_id=agent_id, token=token)
        db.add(bal)
        await db.flush()

    return bal


async def create_payment(
    db: AsyncSession,
    from_agio_id: str,
    to_agio_id: str,
    amount: float,
    memo: str | None = None,
    token: str = "USDC",
) -> dict:
    """Validate and queue a payment. Handles cross-token swaps automatically."""
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    if amount > 100_000:
        raise ValueError("Payment amount exceeds maximum ($100,000)")
    if token not in SUPPORTED_TOKENS:
        raise ValueError(f"Unsupported token: {token}")

    memo = _sanitize_memo(memo)

    from_agent = (await db.execute(
        select(Agent)
        .where(Agent.agio_id == from_agio_id)
        .with_for_update()
    )).scalar_one_or_none()
    if not from_agent:
        raise AgentNotFound(from_agio_id)

    to_agent = (await db.execute(
        select(Agent).where(Agent.agio_id == to_agio_id)
    )).scalar_one_or_none()
    if not to_agent:
        raise AgentNotFound(to_agio_id)

    # Determine if swap is needed
    receiver_token = to_agent.preferred_token or DEFAULT_TOKEN
    needs_swap = (token != receiver_token)
    swap_fee = Decimal("0")

    from .tier_service import get_agent_tier, calculate_fee
    tier = await get_agent_tier(db, from_agent)
    amt = Decimal(str(amount))
    if tier:
        fee = calculate_fee(tier, amt, is_cross_chain=False)
    else:
        fee = Decimal("0.00015")  # SPARK default

    if needs_swap:
        swap_fee = _calculate_swap_fee(amt)

    total_debit = amt + fee + swap_fee

    # Check per-token balance
    sender_bal = await _get_or_create_balance(db, from_agent.id, token)
    balance = Decimal(str(sender_bal.balance))
    locked = Decimal(str(sender_bal.locked_balance))
    available = balance - locked

    if available < total_debit:
        raise InsufficientBalance(float(available), float(total_debit))

    payment_id = "0x" + hashlib.sha256(
        f"{from_agio_id}:{to_agio_id}:{amount}:{uuid.uuid4()}".encode()
    ).hexdigest()

    payment = Payment(
        payment_id=payment_id,
        from_agent_id=from_agent.id,
        to_agent_id=to_agent.id,
        amount=amt,
        fee=fee,
        from_token=token,
        to_token=receiver_token,
        swap_fee=swap_fee,
        memo=memo,
        status="QUEUED",
    )
    db.add(payment)

    sender_bal.balance = balance - total_debit
    sender_bal.locked_balance = locked + total_debit

    # Credit receiver immediately (off-chain instant settlement)
    receiver_bal = await _get_or_create_balance(db, to_agent.id, receiver_token)
    receiver_bal.balance = Decimal(str(receiver_bal.balance)) + amt

    await db.commit()

    # Route to correct chain's batch worker queue
    from .router_service import parse_agio_id, get_payment_queue
    chain_name, _ = parse_agio_id(from_agio_id)
    queue = get_payment_queue(chain_name)

    await redis_client.rpush(queue, json.dumps({
        "payment_id": payment_id,
        "from_wallet": from_agent.wallet_address,
        "to_wallet": to_agent.wallet_address,
        "amount": _amount_to_str(amount),
        "fee": _amount_to_str(fee),
        "swap_fee": _amount_to_str(swap_fee),
        "from_token": token,
        "to_token": receiver_token,
        "from_db_id": str(from_agent.id),
        "to_db_id": str(to_agent.id),
        "from_agio_id": from_agio_id,
        "to_agio_id": to_agio_id,
        "chain": chain_name,
    }))

    return {
        "payment_id": payment_id,
        "status": "QUEUED",
        "amount": amount,
        "from_token": token,
        "to_token": receiver_token,
        "swap_needed": needs_swap,
        "fee": float(fee),
        "swap_fee": float(swap_fee),
        "total_debited": float(total_debit),
        "tier": from_agent.tier,
        "estimated_settlement": f"{tier.batch_priority}s",
    }


async def get_payment(db: AsyncSession, payment_id: str) -> dict | None:
    """Get payment status and details."""
    payment = (await db.execute(
        select(Payment).where(Payment.payment_id == payment_id)
    )).scalar_one_or_none()

    if not payment:
        return None

    return {
        "payment_id": payment.payment_id,
        "amount": float(payment.amount),
        "from_token": payment.from_token,
        "to_token": payment.to_token,
        "swap_fee": float(payment.swap_fee),
        "status": payment.status,
        "batch_id": payment.batch_id,
        "memo": payment.memo,
        "created_at": payment.created_at.isoformat(),
        "settled_at": payment.settled_at.isoformat() if payment.settled_at else None,
    }
