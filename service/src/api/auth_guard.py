# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Shared auth guard — verifies session token matches the acting agent."""
import json
from fastapi import HTTPException
from ..core.redis import redis_client


async def verify_agent(acting_agent_id: str, authorization: str = None):
    """Verify the caller is the agent they claim to be.

    Checks Bearer token from Authorization header.
    Cookie-based auth is handled by CookieToHeaderMiddleware in main.py,
    which converts the httpOnly cookie into an Authorization header
    before this function is called.
    """
    token = None

    if authorization and authorization.startswith("Bearer ses_"):
        token = authorization.replace("Bearer ", "")

    if not token:
        raise HTTPException(401, "Authentication required. Sign in with your API key at POST /v1/auth/login")

    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired. Please sign in again.")

    data = json.loads(session_data)
    if data.get("agio_id") != acting_agent_id:
        raise HTTPException(403, "You can only act as yourself. Sign in with the correct account.")
