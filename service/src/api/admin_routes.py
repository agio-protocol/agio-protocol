"""Admin API routes — protected by ADMIN_API_KEY header."""
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func, text, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.config import settings
from ..models.agent import Agent, AgentBalance
from ..models.payment import Payment
from ..models.batch import Batch
from ..models.chain import SupportedChain
from ..models.loyalty import FeeTier

router = APIRouter(prefix="/v1/admin")

ADMIN_KEY = "agio-admin-2026"


async def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


@router.get("/overview")
async def admin_overview(db: AsyncSession = Depends(get_db), _=Depends(verify_admin)):
    """Full admin overview — the one screen to check every morning."""
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    # Agents
    total_agents = (await db.execute(select(func.count()).select_from(Agent))).scalar() or 0
    active_7d = (await db.execute(
        select(func.count()).select_from(Agent).where(Agent.updated_at >= week_ago)
    )).scalar() or 0

    # Transactions
    total_txns = (await db.execute(
        select(func.count()).select_from(Payment).where(Payment.status == "SETTLED")
    )).scalar() or 0
    txns_24h = (await db.execute(
        select(func.count()).select_from(Payment).where(
            Payment.status == "SETTLED", Payment.settled_at >= day_ago
        )
    )).scalar() or 0

    # Volume
    total_volume = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "SETTLED")
    )).scalar() or 0)
    volume_24h = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == "SETTLED", Payment.settled_at >= day_ago
        )
    )).scalar() or 0)

    # Fees (from actual recorded fee data)
    total_fees = float((await db.execute(
        select(func.coalesce(func.sum(Payment.fee), 0)).where(Payment.status == "SETTLED")
    )).scalar() or 0)
    fees_24h = float((await db.execute(
        select(func.coalesce(func.sum(Payment.fee), 0)).where(
            Payment.status == "SETTLED", Payment.settled_at >= day_ago
        )
    )).scalar() or 0)
    total_swap_fees = float((await db.execute(
        select(func.coalesce(func.sum(Payment.swap_fee), 0)).where(Payment.status == "SETTLED")
    )).scalar() or 0)
    swap_fees_24h = float((await db.execute(
        select(func.coalesce(func.sum(Payment.swap_fee), 0)).where(
            Payment.status == "SETTLED", Payment.settled_at >= day_ago
        )
    )).scalar() or 0)

    # Batches
    total_batches = (await db.execute(
        select(func.count()).select_from(Batch).where(Batch.status == "SETTLED")
    )).scalar() or 0
    last_batch = (await db.execute(
        select(Batch).where(Batch.status == "SETTLED").order_by(Batch.settled_at.desc()).limit(1)
    )).scalar_one_or_none()
    total_gas = float((await db.execute(
        select(func.coalesce(func.sum(Batch.gas_used), 0)).where(Batch.status == "SETTLED")
    )).scalar() or 0)

    # Queue
    try:
        from ..core.redis import redis_client
        queue_depth = await redis_client.llen("agio:payment_queue")
    except Exception:
        queue_depth = -1

    # Tier distribution
    tier_dist = {}
    rows = (await db.execute(
        select(Agent.tier, func.count()).group_by(Agent.tier)
    )).all()
    for tier, count in rows:
        tier_dist[tier or "NEW"] = count

    # Per-token vault balances (from AgentBalance table)
    token_balances = {}
    token_rows = (await db.execute(
        select(
            AgentBalance.token,
            func.sum(AgentBalance.balance).label("total"),
            func.sum(AgentBalance.locked_balance).label("locked"),
        ).group_by(AgentBalance.token)
    )).all()
    for token, total, locked in token_rows:
        token_balances[token] = {
            "available": float(total or 0),
            "locked": float(locked or 0),
            "total": float((total or 0) + (locked or 0)),
        }

    return {
        "timestamp": now.isoformat(),
        "agents": {
            "total": total_agents,
            "active_7d": active_7d,
        },
        "transactions": {
            "total": total_txns,
            "last_24h": txns_24h,
        },
        "volume": {
            "total_usd": total_volume,
            "last_24h_usd": volume_24h,
        },
        "fees": {
            "settlement_fees_total": total_fees,
            "settlement_fees_24h": fees_24h,
            "swap_fees_total": total_swap_fees,
            "swap_fees_24h": swap_fees_24h,
            "total_revenue": total_fees + total_swap_fees,
        },
        "batches": {
            "total": total_batches,
            "total_gas_used": total_gas,
            "last_settled_at": last_batch.settled_at.isoformat() if last_batch and last_batch.settled_at else None,
            "last_batch_payments": last_batch.total_payments if last_batch else 0,
            "last_batch_tx": last_batch.tx_hash if last_batch else None,
        },
        "queue_depth": queue_depth,
        "vault_balances": token_balances,
        "tier_distribution": tier_dist,
    }


@router.get("/agents")
async def admin_agents(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    sort: str = Query("registered_at"),
    order: str = Query("desc"),
    tier: str = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_admin),
):
    """Paginated agent list with filters."""
    query = select(Agent)
    if tier:
        query = query.where(Agent.tier == tier)

    sort_col = getattr(Agent, sort, Agent.registered_at)
    query = query.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    query = query.offset((page - 1) * limit).limit(limit)

    agents = (await db.execute(query)).scalars().all()
    total = (await db.execute(
        select(func.count()).select_from(Agent).where(Agent.tier == tier if tier else True)
    )).scalar() or 0

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "agents": [
            {
                "agio_id": a.agio_id,
                "wallet": a.wallet_address,
                "tier": a.tier,
                "balance": float(a.balance),
                "locked": float(a.locked_balance),
                "preferred_token": a.preferred_token,
                "total_payments": a.total_payments,
                "total_volume": float(a.total_volume),
                "registered_at": a.registered_at.isoformat(),
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in agents
        ],
    }


@router.get("/transactions")
async def admin_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    status: str = Query(None),
    token: str = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_admin),
):
    """Paginated transaction list."""
    query = select(Payment)
    if status:
        query = query.where(Payment.status == status)
    if token:
        query = query.where(Payment.from_token == token)

    query = query.order_by(Payment.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)

    payments = (await db.execute(query)).scalars().all()

    return {
        "page": page,
        "limit": limit,
        "transactions": [
            {
                "payment_id": p.payment_id,
                "from_agent_id": str(p.from_agent_id),
                "to_agent_id": str(p.to_agent_id),
                "amount": float(p.amount),
                "from_token": p.from_token,
                "to_token": p.to_token,
                "swap_fee": float(p.swap_fee),
                "status": p.status,
                "batch_id": p.batch_id,
                "memo": p.memo,
                "created_at": p.created_at.isoformat(),
                "settled_at": p.settled_at.isoformat() if p.settled_at else None,
            }
            for p in payments
        ],
    }


@router.get("/batches")
async def admin_batches(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_admin),
):
    """Paginated batch list."""
    query = select(Batch).order_by(Batch.submitted_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)
    batches = (await db.execute(query)).scalars().all()

    return {
        "page": page,
        "limit": limit,
        "batches": [
            {
                "batch_id": b.batch_id,
                "total_payments": b.total_payments,
                "total_volume": float(b.total_volume),
                "gas_used": b.gas_used,
                "tx_hash": b.tx_hash,
                "status": b.status,
                "submitted_at": b.submitted_at.isoformat() if b.submitted_at else None,
                "settled_at": b.settled_at.isoformat() if b.settled_at else None,
                "basescan_url": f"https://basescan.org/tx/0x{b.tx_hash}" if b.tx_hash else None,
            }
            for b in batches
        ],
    }


@router.get("/revenue")
async def admin_revenue(db: AsyncSession = Depends(get_db), _=Depends(verify_admin)):
    """Revenue breakdown."""
    total_volume = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "SETTLED")
    )).scalar() or 0)
    total_swap_fees = float((await db.execute(
        select(func.coalesce(func.sum(Payment.swap_fee), 0)).where(Payment.status == "SETTLED")
    )).scalar() or 0)
    total_settlement_fees = total_volume * 0.00015

    # Monthly run rate
    first_payment = (await db.execute(
        select(Payment.created_at).where(Payment.status == "SETTLED").order_by(Payment.created_at.asc()).limit(1)
    )).scalar()
    days_active = max((datetime.utcnow() - first_payment).days, 1) if first_payment else 1
    daily_revenue = (total_settlement_fees + total_swap_fees) / days_active
    monthly_projected = daily_revenue * 30

    return {
        "settlement_fees": total_settlement_fees,
        "swap_fees": total_swap_fees,
        "total_revenue": total_settlement_fees + total_swap_fees,
        "total_volume": total_volume,
        "days_active": days_active,
        "daily_avg_revenue": daily_revenue,
        "monthly_projected": monthly_projected,
    }


@router.get("/wallets")
async def admin_wallets(_=Depends(verify_admin)):
    """All protocol wallet balances across Base and Solana."""
    import httpx

    result = {"base": {}, "solana": {}, "alerts": [], "total_usd": 0.0}

    # === BASE CHAIN ===
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
        vault_addr = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
        deployer = settings.get_deployer_address() or "0xB18A31796ea51c52c203c96AaB0B1bC551C4e051"
        fee_collector = settings.get_fee_collector_address() or deployer

        deployer_eth = w3.eth.get_balance(Web3.to_checksum_address(deployer)) / 1e18
        fee_eth = w3.eth.get_balance(Web3.to_checksum_address(fee_collector)) / 1e18

        TOKENS = {
            "USDC": {"addr": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "dec": 6},
            "USDT": {"addr": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "dec": 6},
            "DAI":  {"addr": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "dec": 18},
            "WETH": {"addr": "0x4200000000000000000000000000000000000006", "dec": 18},
            "cbETH": {"addr": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "dec": 18},
        }
        ERC20_ABI = [{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]

        vault_tokens = {}
        for symbol, info in TOKENS.items():
            try:
                c = w3.eth.contract(address=Web3.to_checksum_address(info["addr"]), abi=ERC20_ABI)
                raw = c.functions.balanceOf(Web3.to_checksum_address(vault_addr)).call()
                vault_tokens[symbol] = round(raw / (10 ** info["dec"]), 6)
            except Exception:
                vault_tokens[symbol] = 0

        result["base"] = {
            "deployer": {"address": deployer, "eth": round(deployer_eth, 6)},
            "fee_collector": {"address": fee_collector, "eth": round(fee_eth, 6)},
            "vault": {"address": vault_addr, "tokens": vault_tokens},
        }
        if deployer_eth < 0.002:
            result["alerts"].append("Base deployer ETH below $5 — cannot pay gas")
        result["total_usd"] += vault_tokens.get("USDC", 0) + vault_tokens.get("USDT", 0) + vault_tokens.get("DAI", 0)
        result["total_usd"] += vault_tokens.get("WETH", 0) * 2400
    except Exception as e:
        result["base"] = {"error": str(e)[:80]}

    # === SOLANA CHAIN ===
    try:
        sol_deployer = "Csix2rY2de4eGpsVVvfytGpxTUHLeq2V32HZJmW8Wa6S"
        sol_vault = "3wtiPBWPNAy5QeJkSUEdgNcazMukTmxZSVYS3Mk8EkxQ"
        sol_rpc = "https://api.mainnet-beta.solana.com"

        async with httpx.AsyncClient(timeout=10) as client:
            # Deployer SOL balance
            r = await client.post(sol_rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [sol_deployer]
            })
            sol_balance = r.json().get("result", {}).get("value", 0) / 1e9

            # Vault USDC-SPL balance
            usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            r2 = await client.post(sol_rpc, json={
                "jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
                "params": [sol_vault, {"mint": usdc_mint}, {"encoding": "jsonParsed"}]
            })
            vault_usdc = 0
            accounts = r2.json().get("result", {}).get("value", [])
            for acct in accounts:
                info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                vault_usdc = float(info.get("tokenAmount", {}).get("uiAmount", 0))

        # Deployer USDC-SPL balance
        deployer_usdc = 0
        r3 = await client.post(sol_rpc, json={
            "jsonrpc": "2.0", "id": 3, "method": "getTokenAccountsByOwner",
            "params": [sol_deployer, {"mint": usdc_mint}, {"encoding": "jsonParsed"}]
        })
        for acct in r3.json().get("result", {}).get("value", []):
            info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            deployer_usdc = float(info.get("tokenAmount", {}).get("uiAmount", 0))

        result["solana"] = {
            "deployer": {"address": sol_deployer, "sol": round(sol_balance, 6), "usdc_spl": deployer_usdc},
            "vault": {"address": sol_vault, "usdc_spl": vault_usdc},
        }
        if sol_balance < 0.1:
            result["alerts"].append("Solana deployer SOL below 0.1 — cannot pay gas")
        if vault_usdc < 10:
            result["alerts"].append(f"Solana vault USDC reserve low: ${vault_usdc}")
        result["total_usd"] += vault_usdc
    except Exception as e:
        result["solana"] = {"error": str(e)[:80]}

    return result


@router.get("/reconciliation")
async def admin_reconciliation(_=Depends(verify_admin)):
    """Current reconciliation status."""
    try:
        from ..core.redis import redis_client
        paused = await redis_client.get("AGIO:payments_paused")
        reason = await redis_client.get("AGIO:pause_reason")
        return {
            "payments_paused": paused == "1",
            "pause_reason": reason,
            "status": "PAUSED" if paused == "1" else "OK",
        }
    except Exception as e:
        return {"status": "UNKNOWN", "error": str(e)}
