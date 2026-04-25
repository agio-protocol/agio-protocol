# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Authentication API — API keys, wallet signatures, session tokens.

Three-layer auth:
1. API key (agt_ prefix, bcrypt hashed, generated at registration)
2. Wallet signature (EVM ecrecover / Ed25519 verify)
3. Session tokens (ses_ prefix, Redis-backed, 24h TTL)
"""
import secrets
import time
import json
import hashlib
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..core.redis import redis_client
from ..models.agent import Agent

router = APIRouter(prefix="/v1/auth")
logger = logging.getLogger("auth")

SESSION_TTL = 86400  # 24 hours
CHALLENGE_TTL = 300  # 5 minutes
MAX_AUTH_FAILURES = 5
LOCKOUT_BASE_MINUTES = 15


def generate_api_key() -> str:
    return "agt_" + secrets.token_hex(32)


def generate_session_token() -> str:
    return "ses_" + secrets.token_hex(32)


def hash_api_key(key: str) -> str:
    import bcrypt
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()


def verify_api_key(key: str, hashed: str) -> bool:
    if not hashed:
        return False
    import bcrypt
    try:
        return bcrypt.checkpw(key.encode(), hashed.encode())
    except Exception:
        return False


class LoginRequest(BaseModel):
    agio_id: str
    api_key: str


class ChallengeRequest(BaseModel):
    agio_id: str


class VerifyRequest(BaseModel):
    agio_id: str
    challenge: str
    signature: str


class RegenerateRequest(BaseModel):
    agio_id: str
    current_api_key: str


async def _check_lockout(agent) -> None:
    if agent.locked_until and datetime.utcnow() < agent.locked_until:
        remaining = int((agent.locked_until - datetime.utcnow()).total_seconds())
        raise HTTPException(429, f"Account locked. Try again in {remaining} seconds.")


async def _record_failure(db: AsyncSession, agent) -> None:
    failures = (agent.auth_failures or 0) + 1
    lock_until = None
    if failures >= MAX_AUTH_FAILURES:
        lockout_mins = LOCKOUT_BASE_MINUTES * (2 ** ((failures - MAX_AUTH_FAILURES) // MAX_AUTH_FAILURES))
        lockout_mins = min(lockout_mins, 120)
        lock_until = datetime.utcnow() + timedelta(minutes=lockout_mins)
        logger.warning(f"Auth lockout: {agent.agio_id[:20]}... ({failures} failures, locked {lockout_mins}min)")
    try:
        await db.execute(
            update(Agent).where(Agent.id == agent.id).values(
                auth_failures=failures, locked_until=lock_until
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()


async def _create_session(agent, auth_method: str) -> dict:
    token = generate_session_token()
    session_data = {
        "agio_id": agent.agio_id,
        "wallet": agent.wallet_address,
        "tier": agent.tier,
        "chain": "solana" if not agent.wallet_address.startswith("0x") else "base",
        "authenticated_via": auth_method,
        "created_at": time.time(),
    }
    # Invalidate any existing session
    old_key = f"agent_session:{agent.agio_id}"
    old_token = await redis_client.get(old_key)
    if old_token:
        await redis_client.delete(f"session:{old_token}")

    await redis_client.setex(f"session:{token}", SESSION_TTL, json.dumps(session_data))
    await redis_client.setex(old_key, SESSION_TTL, token)
    return {"session_token": token, **session_data, "expires_in": SESSION_TTL}


# === Public: Registration returns API key ===

async def generate_key_for_agent(db: AsyncSession, agent) -> str:
    """Generate and store a new API key for an agent. Returns plaintext key (store nowhere)."""
    plaintext = generate_api_key()
    hashed = hash_api_key(plaintext)
    await db.execute(
        update(Agent).where(Agent.id == agent.id).values(
            api_key_hash=hashed, key_created_at=datetime.utcnow(), auth_failures=0, locked_until=None
        )
    )
    await db.commit()
    return plaintext


# === Endpoint: Login with API key ===

@router.post("/login")
async def login(req: LoginRequest, response: Response = None, db: AsyncSession = Depends(get_db)):
    """Authenticate with Agiotage ID + API key. Returns session token + sets httpOnly cookie."""
    try:
        agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agio_id))).scalar_one_or_none()
    except Exception:
        # Auto-migrate: add auth columns if they don't exist
        await db.rollback()
        from sqlalchemy import text
        for col_sql in [
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128) DEFAULT ''",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS auth_failures INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS key_created_at TIMESTAMP",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_auth_at TIMESTAMP",
        ]:
            try:
                await db.execute(text(col_sql))
            except Exception:
                pass
        await db.commit()
        agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(401, "Invalid credentials")

    await _check_lockout(agent)

    key_hash = ""
    try:
        key_hash = agent.api_key_hash or ""
    except Exception:
        key_hash = ""

    if not key_hash or not verify_api_key(req.api_key, key_hash):
        await _record_failure(db, agent)
        raise HTTPException(401, "Invalid credentials")

    # Success — reset failures
    await db.execute(
        update(Agent).where(Agent.id == agent.id).values(
            auth_failures=0, locked_until=None, last_auth_at=datetime.utcnow()
        )
    )
    await db.commit()

    session = await _create_session(agent, "api_key")
    logger.info(f"Login: {agent.agio_id[:20]}... via API key")

    # Set httpOnly cookie for web UI (immune to XSS)
    if response:
        response.set_cookie(
            key="agiotage_session",
            value=session["session_token"],
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=SESSION_TTL,
            path="/",
        )
    return session


# === Endpoint: Request wallet signature challenge ===

@router.post("/challenge")
async def request_challenge(req: ChallengeRequest, db: AsyncSession = Depends(get_db)):
    """Request a challenge string for wallet signature authentication."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    nonce = secrets.token_hex(8)
    ts = int(time.time())
    challenge = f"agiotage-auth-{req.agio_id}-{ts}-{nonce}"

    await redis_client.setex(f"auth_challenge:{req.agio_id}", CHALLENGE_TTL, challenge)

    return {"challenge": challenge, "expires_in": CHALLENGE_TTL}


# === Endpoint: Verify wallet signature ===

@router.post("/verify")
async def verify_signature(req: VerifyRequest, db: AsyncSession = Depends(get_db)):
    """Verify a wallet signature against a challenge. Returns session token."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(401, "Invalid credentials")

    await _check_lockout(agent)

    # Check challenge exists and matches
    stored = await redis_client.get(f"auth_challenge:{req.agio_id}")
    if not stored or stored != req.challenge:
        raise HTTPException(401, "Challenge expired or invalid")

    # Delete challenge (prevent replay)
    await redis_client.delete(f"auth_challenge:{req.agio_id}")

    # Verify signature
    is_solana = not agent.wallet_address.startswith("0x")
    verified = False

    if is_solana:
        try:
            from solders.pubkey import Pubkey
            from solders.signature import Signature as SolSignature
            pubkey = Pubkey.from_string(agent.wallet_address)
            sig = SolSignature.from_string(req.signature)
            verified = sig.verify(pubkey, req.challenge.encode())
        except Exception as e:
            logger.warning(f"Solana sig verify failed: {e}")
            verified = False
    else:
        try:
            from eth_account.messages import encode_defunct
            from web3 import Web3
            message = encode_defunct(text=req.challenge)
            recovered = Web3().eth.account.recover_message(message, signature=req.signature)
            verified = recovered.lower() == agent.wallet_address.lower()
        except Exception as e:
            logger.warning(f"EVM sig verify failed: {e}")
            verified = False

    if not verified:
        await _record_failure(db, agent)
        raise HTTPException(401, "Signature verification failed")

    await db.execute(
        update(Agent).where(Agent.id == agent.id).values(
            auth_failures=0, locked_until=None, last_auth_at=datetime.utcnow()
        )
    )
    await db.commit()

    session = await _create_session(agent, "wallet_signature")
    logger.info(f"Login: {agent.agio_id[:20]}... via wallet signature")
    return session


# === Endpoint: Regenerate API key ===

@router.post("/regenerate-key")
async def regenerate_key(req: RegenerateRequest, db: AsyncSession = Depends(get_db)):
    """Generate a new API key. Requires current key for verification."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(401, "Invalid credentials")

    key_hash = ""
    try:
        key_hash = agent.api_key_hash or ""
    except Exception:
        pass

    if not key_hash or not verify_api_key(req.current_api_key, key_hash):
        raise HTTPException(401, "Invalid current API key")

    new_key = await generate_key_for_agent(db, agent)

    # Invalidate all sessions
    old_key = f"agent_session:{agent.agio_id}"
    old_token = await redis_client.get(old_key)
    if old_token:
        await redis_client.delete(f"session:{old_token}")
    await redis_client.delete(old_key)

    logger.info(f"Key regenerated: {agent.agio_id[:20]}...")
    return {"agio_id": agent.agio_id, "api_key": new_key, "message": "Save this key — it will not be shown again."}


# === Endpoint: Logout ===

@router.post("/logout")
async def logout(authorization: str = Header(None), agiotage_session: str = Cookie(None), response: Response = None):
    """Invalidate the current session and clear cookie."""
    token = None
    if authorization and authorization.startswith("Bearer ses_"):
        token = authorization.replace("Bearer ", "")
    elif agiotage_session and agiotage_session.startswith("ses_"):
        token = agiotage_session

    if not token:
        raise HTTPException(401, "No valid session")
    session_data = await redis_client.get(f"session:{token}")
    if session_data:
        data = json.loads(session_data)
        await redis_client.delete(f"session:{token}")
        await redis_client.delete(f"agent_session:{data['agio_id']}")
    if response:
        response.delete_cookie("agiotage_session", path="/")
    return {"status": "logged_out"}


# === Endpoint: Validate session ===

@router.get("/session")
async def validate_session(authorization: str = Header(None)):
    """Check if a session token is valid. Returns session data."""
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "No valid session")
    token = authorization.replace("Bearer ", "")
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired or invalid")
    return json.loads(session_data)


# === Endpoint: Migrate existing agents (temporary) ===

@router.post("/migrate")
async def migrate_agent(agio_id: str, db: AsyncSession = Depends(get_db)):
    """One-time key generation for existing agents without API keys."""
    try:
        agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    except Exception:
        await db.rollback()
        from sqlalchemy import text
        for col_sql in [
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128) DEFAULT ''",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS auth_failures INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS key_created_at TIMESTAMP",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_auth_at TIMESTAMP",
        ]:
            try: await db.execute(text(col_sql))
            except Exception: pass
        await db.commit()
        agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    has_key = False
    try:
        has_key = bool(agent.api_key_hash)
    except Exception:
        pass

    if has_key:
        raise HTTPException(400, "Agent already has an API key. Use /v1/auth/regenerate-key to get a new one.")

    new_key = await generate_key_for_agent(db, agent)
    return {
        "agio_id": agent.agio_id,
        "api_key": new_key,
        "message": "Save this key securely — it will not be shown again. Use it with POST /v1/auth/login to authenticate.",
    }


# === Middleware helper: extract session from request ===

async def get_current_agent(authorization: str = Header(None)) -> Optional[dict]:
    """Extract and validate session from Authorization header. Returns None if not authenticated."""
    if not authorization or not authorization.startswith("Bearer ses_"):
        return None
    token = authorization.replace("Bearer ", "")
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        return None
    return json.loads(session_data)


async def require_auth(authorization: str = Header(...)) -> dict:
    """Require valid authentication. Raises 401 if not authenticated."""
    session = await get_current_agent(authorization)
    if not session:
        raise HTTPException(401, "Authentication required. Use POST /v1/auth/login with your API key.")
    return session
