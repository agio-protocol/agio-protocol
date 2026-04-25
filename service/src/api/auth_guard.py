# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Shared auth guard — verifies session token matches the acting agent."""
import json
from fastapi import Header, HTTPException
from ..core.redis import redis_client


async def verify_agent(acting_agent_id: str, authorization: str = Header(None)):
    """Verify the caller is the agent they claim to be.

    If a session token is present, it MUST match the acting agent.
    If no token, allows access (transition period — will be removed).
    """
    if not authorization or not authorization.startswith("Bearer ses_"):
        return  # Transition: allow unauthenticated (remove after migration)

    token = authorization.replace("Bearer ", "")
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired. Please sign in again.")

    data = json.loads(session_data)
    if data.get("agio_id") != acting_agent_id:
        raise HTTPException(403, "You can only act as yourself. Sign in with the correct account.")
