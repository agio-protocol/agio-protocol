# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Copy Trade Bot API — webhook receiver for Helius + status endpoints."""
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request, Header, HTTPException

router = APIRouter(prefix="/v1/copy-trader", tags=["copy-trader"])
_log = logging.getLogger("copy-trader-api")

HELIUS_AUTH = os.getenv("HELIUS_WEBHOOK_SECRET", "agio-helius-webhook-2026")
SOL_MINT = "So11111111111111111111111111111111111111112"


async def _require_admin(x_admin_key: str = Header(None)):
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.post("/webhook/helius")
async def helius_webhook(request: Request):
    """Receive real-time swap events from Helius webhooks."""
    # Verify auth — check both Authorization and x-webhook-secret headers
    auth = request.headers.get("authorization", "") or request.headers.get("x-webhook-secret", "")
    if auth != HELIUS_AUTH and auth != f"Bearer {HELIUS_AUTH}":
        _log.warning(f"Helius webhook: bad auth header")
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    if not isinstance(body, list):
        body = [body]

    processed = 0
    for tx in body:
        try:
            tx_type = tx.get("type", "")
            if tx_type != "SWAP":
                continue

            signature = tx.get("signature", "")
            fee_payer = tx.get("feePayer", "")
            timestamp = tx.get("timestamp", 0)
            events = tx.get("events", {})
            swap = events.get("swap", {})

            if not swap or not fee_payer:
                continue

            # Determine what was bought/sold
            token_inputs = swap.get("tokenInputs", [])
            token_outputs = swap.get("tokenOutputs", [])
            native_input = swap.get("nativeInput")
            native_output = swap.get("nativeOutput")

            bought_token = None
            sold_token = None
            amount_sol = 0

            # BUY = SOL in, token out
            if native_input and token_outputs:
                amount_sol = (native_input.get("amount", 0) or 0) / 1e9
                for tok in token_outputs:
                    mint = tok.get("mint", "")
                    if mint and mint != SOL_MINT:
                        bought_token = {
                            "mint": mint,
                            "amount": tok.get("tokenAmount", 0),
                        }
                        break

            # SELL = token in, SOL out
            elif native_output and token_inputs:
                amount_sol = (native_output.get("amount", 0) or 0) / 1e9
                for tok in token_inputs:
                    mint = tok.get("mint", "")
                    if mint and mint != SOL_MINT:
                        sold_token = {
                            "mint": mint,
                            "amount": tok.get("tokenAmount", 0),
                        }
                        break

            # Token-to-token swap (not SOL)
            elif token_inputs and token_outputs:
                for tok in token_outputs:
                    mint = tok.get("mint", "")
                    if mint and mint != SOL_MINT:
                        bought_token = {
                            "mint": mint,
                            "amount": tok.get("tokenAmount", 0),
                        }
                        break

            # Dispatch to copy trader
            from ..workers.copy_trader import handle_helius_swap
            await handle_helius_swap(
                wallet_address=fee_payer,
                tx_hash=signature,
                timestamp=timestamp,
                bought_token=bought_token,
                sold_token=sold_token,
                amount_sol=amount_sol,
            )
            processed += 1

        except Exception as e:
            _log.debug(f"Helius webhook parse error: {e}")

    return {"processed": processed, "total": len(body)}


@router.get("/status")
async def copy_trader_status(x_admin_key: str = Header(None)):
    """Get copy trader status."""
    await _require_admin(x_admin_key)
    from sqlalchemy import select, func
    from ..core.database import async_session
    from ..workers.copy_trader import CopyPosition, CopyTrade, TrackedWallet, get_config

    async with async_session() as db:
        open_count = (await db.execute(
            select(func.count()).select_from(CopyPosition)
            .where(CopyPosition.status == "OPEN")
        )).scalar() or 0

        total_trades = (await db.execute(
            select(func.count()).select_from(CopyTrade)
        )).scalar() or 0

        tracked_wallets = (await db.execute(
            select(TrackedWallet).where(TrackedWallet.active == True)
        )).scalars().all()

        positions = (await db.execute(
            select(CopyPosition).where(CopyPosition.status == "OPEN")
        )).scalars().all()

    config = await get_config()

    return {
        "mode": "PAPER" if config.get("paper_mode", True) else "LIVE",
        "open_positions": open_count,
        "total_trades": total_trades,
        "tracked_wallets": [
            {"address": w.address[:12] + "...", "label": w.label,
             "winrate": float(w.winrate or 0), "tier": w.tier}
            for w in tracked_wallets
        ],
        "positions": [
            {"token": p.token_symbol, "pnl_pct": float(p.pnl_pct or 0),
             "wallet": p.wallet_label, "opened_at": str(p.opened_at)}
            for p in positions
        ],
    }
