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
from .api.meme_routes import router as meme_router
from .api.smart_money_routes import router as smart_money_router
from .api.whale_routes import router as whale_router
from .api.sentiment_routes import router as sentiment_router
from .api.wallet_follow_routes import router as wallet_follow_router
from .api.correlation_routes import router as correlation_router
from .api.unusual_whales_routes import router as uw_router
from .api.alpha_routes import router as alpha_router
from .api.paper_trader_routes import router as paper_trader_router
from .api.crypto_trader_routes import router as crypto_trader_router
from .api.stock_trader_routes import router as stock_trader_router
from .api.momentum_routes import router as momentum_router
from .api.trading_routes import router as trading_router
from .api.copy_trader_routes import router as copy_trader_router
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
    import asyncio
    # Import all models so create_all picks them up
    from .workers.smart_money_tracker import SmartMoneyWallet, SmartMoneyTrade, ClusterSignal  # noqa
    from .workers.whale_tracker import WhaleTransaction, CryptoSignal  # noqa
    from .workers.stocks_tracker import StockWhaleMove, StockSignal  # noqa
    from .workers.sentiment_tracker import SocialMention, SentimentSignal  # noqa
    from .workers.wallet_follow import FollowedWallet, WalletTrade, WalletSignal  # noqa
    from .workers.correlation_engine import CorrelatedSignal  # noqa
    from .workers.paper_trader import PaperPosition, PaperTrade  # noqa
    from .workers.crypto_paper_trader import CryptoPaperPosition, CryptoPaperTrade  # noqa
    from .workers.stock_paper_trader import StockPaperPosition, StockPaperTrade  # noqa
    from .workers.momentum_scanner import MomentumSignal, VolumeBaseline  # noqa
    from .workers.copy_trader import CopyPosition, CopyTrade, TrackedWallet  # noqa
    from .workers.pumpfun_sniper import SnipePosition, SnipeTrade  # noqa
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns to meme_deployments if missing
        for col_sql in [
            "ALTER TABLE meme_deployments ADD COLUMN IF NOT EXISTS peak_fdv NUMERIC(18,2)",
            "ALTER TABLE meme_deployments ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP",
            "ALTER TABLE meme_deployments ADD COLUMN IF NOT EXISTS peak_liquidity NUMERIC(18,2)",
            "ALTER TABLE meme_deployments ADD COLUMN IF NOT EXISTS is_rugged BOOLEAN DEFAULT FALSE",
            "ALTER TABLE meme_deployments ADD COLUMN IF NOT EXISTS rugged_at TIMESTAMP",
            "ALTER TABLE top_deployers ADD COLUMN IF NOT EXISTS rug_count INTEGER DEFAULT 0",
            "ALTER TABLE smart_money_trades ADD COLUMN IF NOT EXISTS wallet_name VARCHAR(100)",
            "ALTER TABLE smart_money_trades ADD COLUMN IF NOT EXISTS wallet_twitter VARCHAR(100)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS weighted_score NUMERIC(8,2)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS avg_wallet_winrate NUMERIC(5,2)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS is_deployer_token BOOLEAN DEFAULT FALSE",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS price_at_signal NUMERIC(18,10)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS price_1h NUMERIC(18,10)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS price_6h NUMERIC(18,10)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS price_24h NUMERIC(18,10)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS pct_change_1h NUMERIC(8,4)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS pct_change_6h NUMERIC(8,4)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS pct_change_24h NUMERIC(8,4)",
            "ALTER TABLE cluster_signals ADD COLUMN IF NOT EXISTS outcome VARCHAR(20)",
            "ALTER TABLE social_mentions ADD COLUMN IF NOT EXISTS category VARCHAR(20) DEFAULT 'crypto'",
            "ALTER TABLE sentiment_signals ADD COLUMN IF NOT EXISTS category VARCHAR(20) DEFAULT 'crypto'",
            "ALTER TABLE social_mentions ADD COLUMN IF NOT EXISTS sentiment_score INTEGER",
            "ALTER TABLE social_mentions ADD COLUMN IF NOT EXISTS conviction INTEGER",
        ]:
            try:
                await conn.execute(__import__('sqlalchemy').text(col_sql))
            except Exception:
                pass
    # Start background workers
    from .workers.meme_tracker import run as meme_tracker_run
    from .workers.marketing_agent import run_agent as moltbook_run
    from .workers.meme_backfill import run as meme_backfill_run
    from .workers.smart_money_tracker import run as smart_money_run, SmartMoneyWallet, SmartMoneyTrade, ClusterSignal  # noqa
    from .workers.whale_tracker import run as whale_run, WhaleTransaction, CryptoSignal  # noqa
    from .workers.stocks_tracker import run as stocks_run, StockWhaleMove, StockSignal  # noqa
    from .workers.sentiment_tracker import run as sentiment_run, SocialMention, SentimentSignal  # noqa
    from .workers.wallet_follow import run as wallet_follow_run, FollowedWallet, WalletTrade, WalletSignal  # noqa
    meme_task = asyncio.create_task(meme_tracker_run())
    moltbook_task = asyncio.create_task(moltbook_run())
    backfill_task = asyncio.create_task(meme_backfill_run())
    smart_money_task = asyncio.create_task(smart_money_run())
    whale_task = asyncio.create_task(whale_run())
    stocks_task = asyncio.create_task(stocks_run())
    sentiment_task = asyncio.create_task(sentiment_run())
    wallet_follow_task = asyncio.create_task(wallet_follow_run())
    from .workers.correlation_engine import run as correlation_run
    from .workers.telegram_alerts import run as telegram_run
    from .workers.paper_trader import run as paper_trader_run
    from .workers.crypto_paper_trader import run as crypto_trader_run
    from .workers.stock_paper_trader import run as stock_trader_run
    from .workers.momentum_scanner import run as momentum_run
    # Copy trader: entries via Helius webhook (copy_trader_routes.py)
    # Position management runs here (lightweight — no GMGN polling)
    from .workers.copy_trader import _manage_positions as copy_manage
    async def _copy_position_loop():
        import asyncio as _aio
        from .workers.copy_trader import get_config as _get_copy_config
        await _aio.sleep(60)
        while True:
            try:
                cfg = await _get_copy_config()
                await copy_manage(cfg)
            except Exception:
                pass
            await _aio.sleep(30)
    copy_mgr_task = asyncio.create_task(_copy_position_loop())
    # pumpfun_sniper runs as separate Railway service
    correlation_task = asyncio.create_task(correlation_run())
    telegram_task = asyncio.create_task(telegram_run())
    paper_task = asyncio.create_task(paper_trader_run())
    crypto_trader_task = asyncio.create_task(crypto_trader_run())
    stock_trader_task = asyncio.create_task(stock_trader_run())
    momentum_task = asyncio.create_task(momentum_run())
    yield
    copy_mgr_task.cancel()
    meme_task.cancel()
    moltbook_task.cancel()
    backfill_task.cancel()
    smart_money_task.cancel()
    whale_task.cancel()
    stocks_task.cancel()
    sentiment_task.cancel()
    wallet_follow_task.cancel()
    correlation_task.cancel()
    telegram_task.cancel()
    paper_task.cancel()
    crypto_trader_task.cancel()
    stock_trader_task.cancel()
    momentum_task.cancel()
    await engine.dispose()


app = FastAPI(
    title="Agiotage Protocol API",
    description="Cross-chain micropayment settlement for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)

import os
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://agiotage.finance,https://spiffy-melomakarona-2fbb67.netlify.app,https://aquamarine-sprinkles-1ea5b9.netlify.app").split(",")
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
            # Skip x402 for webhook endpoints (they have their own auth)
            if request.url.path.startswith("/v1/copy-trader/webhook"):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ses_") or auth.startswith("Bearer agt_"):
                return await call_next(request)
            # No auth — let x402 handle it (returns 402 or passes through if paid)
            # If x402 payment succeeds, tag the request so auth_guard lets it through
            async def x402_call_next(request):
                request.scope["headers"] = [
                    *[(k, v) for k, v in request.scope["headers"] if k != b"authorization"],
                    (b"authorization", b"x402-paid"),
                ]
                return await call_next(request)
            return await self._x402.dispatch(request, x402_call_next)

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
app.include_router(meme_router)
app.include_router(smart_money_router)
app.include_router(whale_router)
app.include_router(sentiment_router)
app.include_router(wallet_follow_router)
app.include_router(correlation_router)
app.include_router(uw_router)
app.include_router(alpha_router)
app.include_router(paper_trader_router)
app.include_router(crypto_trader_router)
app.include_router(stock_trader_router)
app.include_router(momentum_router)
app.include_router(trading_router)
app.include_router(copy_trader_router)
# v1777325742
# 1777579990
