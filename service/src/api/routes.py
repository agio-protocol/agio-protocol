# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""API routes — all AGIO endpoints including cross-chain and reputation."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..core.exceptions import AgentNotFound
from ..services import payment_service, registry_service, router_service, reputation_service, savings_service

router = APIRouter(prefix="/v1")


# --- Request/Response Models ---

class RegisterRequest(BaseModel):
    wallet_address: str
    chain: str = "base"
    name: Optional[str] = None
    metadata: Optional[dict] = None

class PayRequest(BaseModel):
    from_agio_id: str
    to_agio_id: str
    amount: float
    token: str = "USDC"
    memo: Optional[str] = None

class SetPreferredTokenRequest(BaseModel):
    agio_id: str
    token: str

class RequestPaymentRequest(BaseModel):
    from_agio_id: str
    amount: float
    service: Optional[str] = None
    expires_in: int = 300

class ReputationQueryRequest(BaseModel):
    min_score: int = 0
    tier: Optional[str] = None
    limit: int = 50


# --- Core Endpoints ---

@router.post("/register")
async def register_agent(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new agent with AGIO."""
    if not req.wallet_address or len(req.wallet_address) < 10:
        raise HTTPException(400, "Invalid wallet address (minimum 10 characters)")
    from ..core.ratelimit import check_registration_limit
    if not await check_registration_limit():
        raise HTTPException(429, "Registration rate limit exceeded. Try again later.")
    return await registry_service.register_agent(
        db, req.wallet_address, req.name, req.metadata
    )


@router.post("/pay")
async def create_payment(req: PayRequest, request: Request, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    from .auth_guard import verify_agent
    await verify_agent(req.from_agio_id, authorization, request)
    """
    Submit a payment. Auto-detects cross-chain from AGIO ID prefix.

    Same-chain: queued for batch settlement (~60s).
    Cross-chain: settled instantly via liquidity fronting (~500ms).

    AGIO ID format: "agio:chain:address" (e.g., "agio:sol:0x1234...")
    Omit prefix for Base (default chain).
    """
    routing = await router_service.route_payment(
        db, req.from_agio_id, req.to_agio_id, req.amount
    )

    if routing.routing_type == "SAME_CHAIN":
        return await payment_service.create_payment(
            db, req.from_agio_id, req.to_agio_id, req.amount, req.memo, req.token
        )
    else:
        import hashlib, uuid
        payment_id = "0x" + hashlib.sha256(
            f"{req.from_agio_id}:{req.to_agio_id}:{req.amount}:{uuid.uuid4()}".encode()
        ).hexdigest()
        return await router_service.execute_cross_chain(
            db, req.from_agio_id, req.to_agio_id, req.amount, payment_id, routing
        )


@router.post("/request")
async def request_payment(req: RequestPaymentRequest, db: AsyncSession = Depends(get_db)):
    """Create a payment request (invoice)."""
    return {
        "invoice_id": "inv_placeholder",
        "amount": req.amount,
        "from": req.from_agio_id,
        "service": req.service,
        "expires_in": req.expires_in,
        "status": "PENDING",
    }


@router.get("/balance/{agio_id}")
async def get_balance(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent balance breakdown."""
    result = await registry_service.get_balance(db, agio_id)
    if not result:
        raise AgentNotFound(agio_id)
    return result


@router.get("/payment/{payment_id}")
async def get_payment(payment_id: str, db: AsyncSession = Depends(get_db)):
    """Get payment status and details."""
    result = await payment_service.get_payment(db, payment_id)
    if not result:
        raise AgentNotFound(payment_id)
    return result


@router.get("/agent/{agio_id}")
async def get_agent(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent profile and stats."""
    result = await registry_service.get_agent(db, agio_id)
    if not result:
        raise AgentNotFound(agio_id)
    return result


# --- Token Preferences ---

@router.post("/token/preferred")
async def set_preferred_token(req: SetPreferredTokenRequest, request: Request, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Set agent's preferred receive token (USDC, USDT, DAI, WETH, cbETH)."""
    from .auth_guard import verify_agent
    await verify_agent(req.agio_id, authorization, request)
    from ..services.payment_service import SUPPORTED_TOKENS
    if req.token not in SUPPORTED_TOKENS:
        raise HTTPException(status_code=400, detail=f"Unsupported token: {req.token}")

    from ..models.agent import Agent
    from sqlalchemy import update
    result = await db.execute(
        update(Agent).where(Agent.agio_id == req.agio_id).values(preferred_token=req.token)
    )
    await db.commit()
    if result.rowcount == 0:
        raise AgentNotFound(req.agio_id)
    return {"agio_id": req.agio_id, "preferred_token": req.token}


@router.get("/balances/{agio_id}")
async def get_token_balances(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get all per-token balances for an agent."""
    from ..models.agent import Agent, AgentBalance
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agio_id)
    )).scalar_one_or_none()
    if not agent:
        raise AgentNotFound(agio_id)

    balances = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == agent.id)
    )).scalars().all()

    return {
        "agio_id": agio_id,
        "preferred_token": agent.preferred_token,
        "balances": {
            b.token: {
                "available": float(b.balance),
                "locked": float(b.locked_balance),
            }
            for b in balances
        },
    }


# --- Reputation Endpoints ---

@router.get("/reputation/{agio_id}")
async def get_reputation(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent reputation score and component breakdown."""
    result = await reputation_service.get_score(db, agio_id)
    if not result:
        raise AgentNotFound(agio_id)
    return result


@router.post("/reputation/{agio_id}/refresh")
async def refresh_reputation(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Force recalculate reputation score."""
    result = await reputation_service.calculate_score(db, agio_id)
    return {
        "agio_id": agio_id,
        "score": result.total,
        "tier": result.tier,
        "components": result.components,
    }


@router.post("/reputation/query")
async def query_reputation(req: ReputationQueryRequest, db: AsyncSession = Depends(get_db)):
    """Find agents by reputation criteria (agent discovery)."""
    return await reputation_service.query_agents(db, req.min_score, req.tier, req.limit)


# --- Routing Info ---

@router.get("/routing/estimate")
async def estimate_routing(
    from_id: str = Query(...),
    to_id: str = Query(...),
    amount: float = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Estimate routing for a payment without executing it."""
    routing = await router_service.route_payment(db, from_id, to_id, amount)
    return {
        "routing_type": routing.routing_type,
        "source_chain": routing.source_chain,
        "dest_chain": routing.dest_chain,
        "estimated_cost_usd": routing.estimated_cost,
        "estimated_time_ms": routing.estimated_time_ms,
        "reserve_sufficient": routing.reserve_sufficient,
    }


# --- Savings Calculator ---

class SavingsRequest(BaseModel):
    current_protocol: str = "x402"
    daily_transactions: int = 500
    average_amount: float = 0.005
    chains_used: list[str] = ["base"]

@router.post("/estimate/savings")
async def estimate_savings(req: SavingsRequest):
    """
    Calculate how much an agent would save by switching to AGIO.
    Compares current protocol costs vs AGIO per-transaction and Personal Plan pricing.
    """
    result = savings_service.estimate_savings(
        req.current_protocol, req.daily_transactions, req.average_amount, req.chains_used
    )
    return {
        "current_protocol": result.current_protocol,
        "current_daily_cost": f"${result.current_daily_cost:.4f}",
        "current_monthly_cost": f"${result.current_monthly_cost:.2f}",
        "agio_daily_cost": f"${result.agio_daily_cost:.4f}",
        "agio_monthly_cost": f"${result.agio_monthly_cost:.2f}",
        "monthly_savings": f"${result.monthly_savings:.2f}",
        "savings_percentage": f"{result.savings_percentage:.0f}%",
        "best_plan": result.best_option,
        "personal_plan_monthly": f"${result.agio_plan_monthly_cost:.2f}",
        "breakdown": result.breakdown,
    }


# --- Loyalty: Tiers ---

@router.get("/tier/{agio_id}")
async def get_tier_info(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent's tier, fees, limits, and progress to next tier."""
    from ..services import tier_service
    return await tier_service.get_tier_info(db, agio_id)


# --- Loyalty: Points ---

@router.get("/points/{agio_id}")
async def get_points(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent's points, streak, and multiplier."""
    from ..services import points_service
    return await points_service.get_points(db, agio_id)

@router.get("/points/leaderboard")
async def get_leaderboard(limit: int = 25, db: AsyncSession = Depends(get_db)):
    """Top agents by lifetime points."""
    from ..services import points_service
    return await points_service.get_leaderboard(db, limit)


# --- Loyalty: Referrals ---

@router.post("/referral/generate")
async def generate_referral(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Generate a referral code. Requires PULSE+ tier."""
    from ..services import referral_service
    return await referral_service.generate_referral_code(db, agio_id)

@router.get("/referral/earnings/{agio_id}")
async def get_referral_earnings(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get referral earnings summary."""
    from ..services import referral_service
    return await referral_service.get_referral_summary(db, agio_id)


# --- Network Dashboard ---

@router.get("/network/stats")
async def get_network_stats(db: AsyncSession = Depends(get_db)):
    """Public network dashboard stats. Cached 60 seconds."""
    from ..core.cache import get_cached, set_cached

    cached = await get_cached("network_stats")
    if cached:
        return cached

    from sqlalchemy import func as sa_func
    from ..models.agent import Agent as AgentModel
    from ..models.payment import Payment as PaymentModel

    total_agents = (await db.execute(
        select(sa_func.count()).select_from(AgentModel)
    )).scalar() or 0

    total_txns = (await db.execute(
        select(sa_func.count()).select_from(PaymentModel).where(PaymentModel.status == "SETTLED")
    )).scalar() or 0

    total_volume = (await db.execute(
        select(sa_func.coalesce(sa_func.sum(PaymentModel.amount), 0)).where(PaymentModel.status == "SETTLED")
    )).scalar() or 0

    result = {
        "total_agents": total_agents,
        "total_transactions": total_txns,
        "total_volume_usd": float(total_volume),
        "average_transaction": float(total_volume) / max(total_txns, 1),
    }
    await set_cached("network_stats", result, ttl_key="network_stats")
    return result


# --- Discovery ---

@router.get("/discover")
async def discover():
    """Machine-readable protocol discovery. One endpoint to know everything about AGIO."""
    return {
        "protocol": "agio",
        "version": "1.0",
        "status": "live",
        "chains": {
            "base": {
                "chain_id": 8453,
                "tokens": ["USDC", "USDT", "DAI", "WETH", "cbETH"],
                "vault": "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc",
            },
            "solana": {
                "program": "68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y",
                "tokens": ["USDC-SPL", "SOL", "USDT-SPL"],
                "vault_pda": "3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ",
            },
        },
        "pricing": {
            "same_chain": "$0.001 per payment",
            "cross_chain": "$0.002 per hop (250x cheaper than bridging)",
            "swap_fee": "0.3% for cross-token conversion",
            "tiers": "SPARK → ARC → PULSE → CORE → NEXUS (up to 80% discount)",
        },
        "register": {
            "endpoint": "POST /v1/register",
            "body": {"wallet_address": "0x... or base58", "name": "my-agent", "chain": "base|solana"},
            "returns": "agio_id (your permanent identity)",
        },
        "pay": {
            "endpoint": "POST /v1/pay",
            "body": {"from_agio_id": "...", "to_agio_id": "agio:sol:...", "amount": 0.001, "token": "USDC"},
            "cross_chain": "Prefix to_agio_id with agio:chain: for cross-chain routing",
        },
        "sdk": {
            "python": "pip install agio-sdk",
            "quickstart": [
                "from agio import AgioClient",
                "client = AgioClient(chain='solana')",
                "await client.register()",
                "await client.deposit(token='USDC', amount=1.00)",
                "await client.pay(to='agio:base:0x...', amount=0.001)",
            ],
        },
        "dashboard": "https://agiotage.finance/dashboard",
        "docs": "https://github.com/agio-protocol/agio-protocol",
    }


# --- Payment Mode ---

class PaymentModeRequest(BaseModel):
    agio_id: str
    mode: str


@router.post("/settings/payment-mode")
async def set_payment_mode(req: PaymentModeRequest, request: Request, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Switch between vault and direct payment modes."""
    from .auth_guard import verify_agent
    await verify_agent(req.agio_id, authorization, request)
    if req.mode not in ("vault", "direct"):
        raise HTTPException(400, "Mode must be 'vault' or 'direct'")
    from ..models.agent import Agent
    from sqlalchemy import update, text
    try:
        result = await db.execute(
            update(Agent).where(Agent.agio_id == req.agio_id).values(payment_mode=req.mode)
        )
        await db.commit()
        if result.rowcount == 0:
            raise AgentNotFound(req.agio_id)
    except Exception:
        await db.rollback()
        try:
            await db.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS payment_mode VARCHAR(10) DEFAULT 'vault'"))
            await db.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS approval_amount NUMERIC(20,6) DEFAULT 0"))
            await db.commit()
            result = await db.execute(update(Agent).where(Agent.agio_id == req.agio_id).values(payment_mode=req.mode))
            await db.commit()
            if result.rowcount == 0:
                raise AgentNotFound(req.agio_id)
        except Exception as e:
            await db.rollback()
            raise HTTPException(500, f"Migration error: {e}")
    return {"agio_id": req.agio_id, "payment_mode": req.mode}


@router.get("/direct/approval-status/{agio_id}")
async def get_approval_status(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get current token approval status for Direct Mode."""
    from ..models.agent import Agent
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        raise AgentNotFound(agio_id)
    mode = "vault"
    approval = 0
    try:
        mode = agent.payment_mode or "vault"
        approval = float(agent.approval_amount or 0)
    except Exception:
        pass
    return {
        "agio_id": agio_id,
        "payment_mode": mode,
        "approval_amount": approval,
        "wallet": agent.wallet_address,
        "recommended_approval": 100.0,
    }


@router.get("/direct/approve-instructions/{agio_id}")
async def get_approve_instructions(agio_id: str, amount: float = Query(100.0), db: AsyncSession = Depends(get_db)):
    """Get the exact transaction the agent needs to sign to set their token approval."""
    from ..models.agent import Agent
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        raise AgentNotFound(agio_id)

    is_solana = not agent.wallet_address.startswith("0x")

    if is_solana:
        return {
            "chain": "solana",
            "method": "spl-token approve",
            "command": f"spl-token approve <your-usdc-token-account> {amount} 3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ",
            "revoke": "spl-token revoke <your-usdc-token-account>",
            "amount": amount,
            "delegate": "3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ",
        }
    else:
        usdc_base = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        vault = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
        amount_wei = int(amount * 1e6)
        return {
            "chain": "base",
            "contract": usdc_base,
            "method": "approve(address,uint256)",
            "args": [vault, amount_wei],
            "spender": vault,
            "amount": amount,
            "amount_wei": amount_wei,
            "revoke": {"method": "approve(address,uint256)", "args": [vault, 0]},
        }


@router.get("/vault/status")
async def vault_status(db: AsyncSession = Depends(get_db)):
    """Public vault transparency data."""
    from sqlalchemy import func as sa_func
    from ..models.agent import AgentBalance

    balances = (await db.execute(
        select(AgentBalance.token, sa_func.sum(AgentBalance.balance), sa_func.sum(AgentBalance.locked_balance))
        .group_by(AgentBalance.token)
    )).all()

    token_totals = {}
    grand_total = 0
    for token, bal, locked in balances:
        total = float(bal or 0)
        token_totals[token] = {"total": total, "locked": float(locked or 0)}
        grand_total += total

    return {
        "base_vault": "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc",
        "solana_vault": "3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ",
        "total_deposits_usd": grand_total,
        "token_breakdown": token_totals,
        "base_explorer": "https://basescan.org/address/0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc",
        "solana_explorer": "https://solscan.io/account/3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ",
        "contracts_source": "https://github.com/agio-protocol/agio-contracts",
        "payment_modes": {
            "vault": "Deposit first. Lowest fees ($0.001/tx). Batched settlement. Cross-chain supported.",
            "direct": "No deposit. Higher fees ($0.005/tx). Individual on-chain settlement. Same-chain only.",
        },
    }


# --- System ---

@router.get("/health")
async def health():
    """Service health check."""
    return {"status": "ok", "service": "agiotage-api", "version": "1.0.0"}
