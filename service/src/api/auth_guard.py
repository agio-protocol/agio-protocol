# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Shared auth guard — verifies session token matches the acting agent."""
import json
from fastapi import HTTPException, Request
from ..core.redis import redis_client


async def verify_agent(acting_agent_id: str, authorization: str = None, request: Request = None):
    """Verify the caller is the agent they claim to be.

    Checks Bearer token (for SDK/API) OR httpOnly cookie (for web UI).
    Called directly from route handlers — not as a FastAPI dependency.
    """
    token = None

    if authorization and authorization.startswith("Bearer ses_"):
        token = authorization.replace("Bearer ", "")

    if not token and request:
        cookie = request.cookies.get("agiotage_session", "")
        if cookie.startswith("ses_"):
            token = cookie

    if not token:
        raise HTTPException(401, "Authentication required. Sign in with your API key at POST /v1/auth/login")

    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired. Please sign in again.")

    data = json.loads(session_data)
    if data.get("agio_id") != acting_agent_id:
        raise HTTPException(403, "You can only act as yourself. Sign in with the correct account.")
