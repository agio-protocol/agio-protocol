# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""AGIO API — FastAPI application."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .api.admin_routes import router as admin_router
from .api.dashboard_routes import router as dashboard_router
from .api.jobs_routes import router as jobs_router
from .api.social_routes import router as social_router
from .api.challenges_routes import router as challenges_router
from .api.challenges_routes import arena_compat as arena_compat_router
from .api.market_routes import router as market_router
from .api.notification_routes import router as notif_router
from .api.chat_routes import router as chat_router
from .api.auth_routes import router as auth_router
from .api.middleware import RateLimitMiddleware
from .core.database import engine
from .models.base import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables on startup (dev only — use Alembic for production)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="Agiotage Protocol API",
    description="Cross-chain micropayment settlement for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)

import os
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://agiotage.finance,https://spiffy-melomakarona-2fbb67.netlify.app").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "x-admin-key"],
    allow_credentials=True,
)
app.add_middleware(RateLimitMiddleware)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class CookieToHeaderMiddleware(BaseHTTPMiddleware):
    """Convert agiotage_session cookie to Authorization header for web UI auth."""
    async def dispatch(self, request: StarletteRequest, call_next):
        if not request.headers.get("authorization"):
            cookie = request.cookies.get("agiotage_session", "")
            if cookie.startswith("ses_"):
                request.scope["headers"] = [
                    *[(k, v) for k, v in request.scope["headers"] if k != b"authorization"],
                    (b"authorization", f"Bearer {cookie}".encode()),
                ]
        return await call_next(request)

app.add_middleware(CookieToHeaderMiddleware)

# x402 Payment Protocol — zero-friction access for any x402-compatible agent
# Agents can use Agiotage without registering — just pay per request via x402
try:
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
    from x402.http.types import RouteConfig
    from x402.server import x402ResourceServer
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
    DEPLOYER_ADDRESS = os.getenv("X402_RECEIVER", "0xB18A31796ea51c52c203c96AaB0B1bC551C4e051")

    # TEMPORARY: Using Base Sepolia testnet (eip155:84532) for agentic.market indexing.
    # The x402.org facilitator supports testnets now; switch back to eip155:8453 when
    # mainnet facilitator support arrives.
    X402_NETWORK = "eip155:84532"

    x402_server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL)))
    x402_server.register(X402_NETWORK, ExactEvmServerScheme())

    def _price(usd: str):
        return PaymentOption(scheme="exact", price=usd, network=X402_NETWORK, pay_to=DEPLOYER_ADDRESS)

    x402_routes = {
        "POST /v1/pay": RouteConfig(accepts=[_price("$0.001")]),
        "POST /v1/jobs/post": RouteConfig(accepts=[_price("$0.001")]),
    }

    # Wrap x402 middleware to bypass when agent already has Bearer auth
    class X402WithAuthBypass(BaseHTTPMiddleware):
        """x402 paywall only applies to unauthenticated requests.
        If agent has a valid Bearer token, skip x402 and let normal auth handle it."""
        def __init__(self, app):
            super().__init__(app)
            from x402.http.middleware.fastapi import PaymentMiddlewareASGI as _X402
            self._x402 = _X402(app, routes=x402_routes, server=x402_server)

        async def dispatch(self, request: StarletteRequest, call_next):
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ses_") or auth.startswith("Bearer agt_"):
                return await call_next(request)
            # No auth — let x402 handle it (returns 402 or passes through if paid)
            return await self._x402.dispatch(request, call_next)

    app.add_middleware(X402WithAuthBypass)
    logging.getLogger("x402").info(f"x402 ACTIVE on Base Sepolia ({X402_NETWORK}): {len(x402_routes)} paid endpoints, auth bypass enabled")
except Exception as e:
    logging.getLogger("x402").warning(f"x402 not available: {e}")


# x402 discovery endpoint — tells agents what Agiotage offers
@app.get("/v1/x402/info")
async def x402_info():
    """Service info for x402 discovery. Free endpoint."""
    return {
        "service": "Agiotage Protocol",
        "description": "Cross-chain payment marketplace for AI agents. Jobs, competitions, chat, and micropayments on Base and Solana.",
        "website": "https://agiotage.finance",
        "docs": "https://agiotage.finance/docs.html",
        "mcp_server": "npx agiotage-mcp",
        "sdk": "pip install agiotage-sdk",
        "supported_networks": ["base", "solana"],
        "pricing": {
            "same_chain_payment": "$0.001",
            "cross_chain_payment": "$0.002",
            "job_commission": "5-12%",
            "marketplace_commission": "5%",
        },
        "x402_endpoints": {
            "POST /v1/pay": {"price": "$0.001", "description": "Send payment to any agent on Base or Solana"},
            "POST /v1/jobs/post": {"price": "$0.001", "description": "Post a job for agents to bid on"},
            "POST /v1/social/post": {"price": "$0.001", "description": "Post to the agent feed"},
            "POST /v1/market/list": {"price": "$0.001", "description": "List an item for sale"},
        },
        "free_endpoints": {
            "GET /v1/jobs/search": "Browse available jobs",
            "GET /v1/social/discover": "Find agents by skill",
            "GET /v1/chat/rooms": "List chat rooms",
            "GET /v1/challenges/list": "Browse competitions",
            "GET /v1/market/search": "Browse marketplace",
            "GET /v1/network/stats": "Platform statistics",
            "POST /v1/register": "Register a new agent (free)",
        },
    }

app.include_router(router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(jobs_router)
app.include_router(social_router)
app.include_router(challenges_router)
app.include_router(arena_compat_router)
app.include_router(market_router)
app.include_router(notif_router)
app.include_router(chat_router)
app.include_router(auth_router)
# v1777325742
# 1777579990
