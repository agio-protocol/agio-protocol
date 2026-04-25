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
