# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Exit Engine — manages open meme-coin positions with ratcheting stops,
tiered scale-outs, trailing stop, liquidity-rug detection, and chunked exits.
"""
import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal

from .exit_config import EXIT_CONFIG

_log = logging.getLogger("exit-engine")


# ---------------------------------------------------------------------------
# Internal helpers (re-use existing functions from paper_trader)
# ---------------------------------------------------------------------------

async def _get_price_mc_liquidity(token_addr: str) -> tuple:
    """Proxy to the existing DexScreener fetcher."""
    from .paper_trader import _get_price_mc_liquidity as _fetch
    return await _fetch(token_addr)


async def _get_sol_price() -> float:
    from .paper_trader import _get_sol_price as _fetch
    return await _fetch()


async def _send_telegram(msg: str):
    from .paper_trader import _send_telegram as _send
    await _send(msg)


async def _is_live_mode() -> bool:
    from .paper_trader import _is_live_mode as _check
    return await _check()


async def _track_daily_loss(loss_sol: float):
    from .paper_trader import _track_daily_loss as _track
    await _track(loss_sol)


# ---------------------------------------------------------------------------
# Sell helpers
# ---------------------------------------------------------------------------

async def _live_sell_tokens(token_address: str, token_amount: int, reason: str,
                            slippage_bps: int = 500) -> dict | None:
    """Sell an ABSOLUTE number of raw tokens (not a percentage of balance).
    Returns tx result dict or None (paper mode).
    Returns {"success": False, ...} on failure.
    """
    if not await _is_live_mode():
        return None
    try:
        from ..services.jupiter_swap import sell_token, get_token_balance
        raw_balance, _ui, decimals = await get_token_balance(token_address)
        if raw_balance <= 0:
            _log.warning(f"EXIT SELL: {reason} -- no on-chain balance found")
            return {"success": False, "error": "no_balance", "tx_hash": None}
        # Clamp to actual balance
        sell_amount = min(token_amount, raw_balance)
        if sell_amount <= 0:
            return {"success": False, "error": "sell_amount_zero", "tx_hash": None}
        result = await sell_token(token_address, sell_amount, decimals, slippage_bps=slippage_bps)
        if result.get("success"):
            _log.info(f"EXIT SELL OK: {reason} tx={result.get('tx_hash')}")
        else:
            _log.error(f"EXIT SELL FAILED: {reason} -- {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"EXIT SELL ERROR: {reason} -- {e}")
        return {"success": False, "error": str(e), "tx_hash": None}


async def _check_price_impact(token_address: str, token_amount: int) -> float | None:
    """Get price impact % for selling token_amount via Jupiter quote. Returns float or None."""
    try:
        from ..services.jupiter_swap import get_quote, SOL_MINT
        quote = await get_quote(token_address, SOL_MINT, token_amount, slippage_bps=500)
        if quote:
            impact = quote.get("priceImpactPct")
            if impact is not None:
                return float(impact)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Core exit helpers
# ---------------------------------------------------------------------------

async def _exit_all(pos, db, reason: str, current_price: float, pnl_pct: float, config: dict):
    """Exit the entire remaining position, chunking if price impact is too high."""
    from .paper_trader import PaperTrade

    remaining_tokens = int(pos.position_size_tokens_remaining or 0)
    if remaining_tokens <= 0:
        return

    slippage_bps = 500
    sol_received_total = 0.0
    tokens_sold_total = 0

    # Check price impact to decide chunking
    chunk_needed = False
    impact = await _check_price_impact(pos.token_address, remaining_tokens)
    if impact is not None and impact > config.get("SLIPPAGE_GUARD_PCT", 0.15) * 100:
        chunk_needed = True

    if chunk_needed:
        pieces = config.get("CHUNK_EXIT_PIECES", 3)
        interval_s = config.get("CHUNK_EXIT_INTERVAL_MS", 10000) / 1000
        chunk_size = remaining_tokens // pieces

        _log.info(f"CHUNKED EXIT: ${pos.token_symbol} {pieces} chunks of {chunk_size} tokens "
                   f"(impact {impact:.2f}%)")

        for i in range(pieces):
            sell_amt = chunk_size if i < pieces - 1 else (remaining_tokens - tokens_sold_total)
            if sell_amt <= 0:
                break
            live_tx = await _live_sell_tokens(
                pos.token_address, sell_amt,
                f"CHUNK {i+1}/{pieces} {reason} ${pos.token_symbol}",
                slippage_bps=slippage_bps)

            if live_tx and not live_tx.get("success"):
                _log.warning(f"Chunk {i+1} failed for ${pos.token_symbol}, aborting chunked exit")
                return  # Don't update DB — retry next tick

            sol_chunk = float(live_tx.get("quote", {}).get("sol_received", 0)) if live_tx and live_tx.get("success") else 0
            sol_received_total += sol_chunk
            tokens_sold_total += sell_amt

            if i < pieces - 1:
                await asyncio.sleep(interval_s)
    else:
        # Single sell
        live_tx = await _live_sell_tokens(
            pos.token_address, remaining_tokens,
            f"{reason} ${pos.token_symbol}", slippage_bps=slippage_bps)

        if live_tx and not live_tx.get("success"):
            _log.warning(f"Exit sell failed for ${pos.token_symbol}, will retry next tick")
            return  # Don't update DB

        sol_received_total = float(live_tx.get("quote", {}).get("sol_received", 0)) if live_tx and live_tx.get("success") else 0
        tokens_sold_total = remaining_tokens

    # DB updates — only if sell succeeded or paper mode
    remaining_pct_sold = (tokens_sold_total / int(pos.position_size_tokens_original)) * 100 if int(pos.position_size_tokens_original) > 0 else float(pos.remaining_pct)
    usd_val = float(pos.position_size_usd) * (float(pos.remaining_pct) / 100) * (1 + pnl_pct / 100)

    tx_tag = ""
    if live_tx and live_tx.get("success"):
        tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]"

    trade = PaperTrade(
        position_id=pos.id,
        action="SELL",
        pct_of_position=Decimal(str(round(remaining_pct_sold, 2))),
        price=Decimal(str(current_price)),
        usd_value=Decimal(str(round(usd_val, 2))),
        pnl_pct=Decimal(str(round(pnl_pct, 4))),
        reason=f"{reason}{tx_tag}"[:100],
    )
    db.add(trade)

    pos.position_size_tokens_remaining = Decimal("0")
    pos.remaining_pct = Decimal("0")
    pos.status = "CLOSED"
    pos.close_reason = reason[:50]
    pos.closed_at = datetime.utcnow()

    # Track daily loss
    if pnl_pct < 0:
        sol_price = await _get_sol_price()
        if sol_price > 0:
            loss_sol = abs(usd_val * (pnl_pct / 100)) / sol_price if pnl_pct < 0 else 0
            # More precise: loss = size * remaining_frac * abs(pnl_pct/100)
            loss_usd = float(pos.position_size_usd) * (float(remaining_pct_sold) / 100) * abs(pnl_pct / 100)
            loss_sol = loss_usd / sol_price
            await _track_daily_loss(loss_sol)

    gain_x = current_price / float(pos.entry_price) if float(pos.entry_price) > 0 else 0
    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
    _log.info(f"{mode} EXIT_ALL ({reason}): ${pos.token_symbol} gain={gain_x:.2f}x "
              f"tokens={tokens_sold_total} SOL_recv={sol_received_total:.4f}")

    await _send_telegram(
        f"*{mode} EXIT: ${pos.token_symbol}*\n"
        f"Reason: {reason}\n"
        f"Gain: {gain_x:.2f}x ({pnl_pct:+.1f}%)\n"
        f"Tokens sold: {tokens_sold_total:,}\n"
        f"SOL received: {sol_received_total:.4f}\n"
        f"Value: ${usd_val:.2f}"
    )


async def _tier_sell(pos, db, tier_name: str, current_price: float, pnl_pct: float,
                     slippage_bps: int = 500) -> bool:
    """Sell 25% of ORIGINAL token amount for a tier scale-out.
    Returns True if sell was recorded, False if it failed or was skipped.
    """
    from .paper_trader import PaperTrade

    original_tokens = int(pos.position_size_tokens_original or 0)
    remaining_tokens = int(pos.position_size_tokens_remaining or 0)
    if original_tokens <= 0 or remaining_tokens <= 0:
        return False

    sell_tokens = original_tokens // 4  # 25% of original
    sell_tokens = min(sell_tokens, remaining_tokens)  # Don't sell more than we have
    if sell_tokens <= 0:
        return False

    live_tx = await _live_sell_tokens(
        pos.token_address, sell_tokens,
        f"{tier_name} ${pos.token_symbol}", slippage_bps=slippage_bps)

    # If live mode and sell failed, return False — retry next tick
    if live_tx and not live_tx.get("success"):
        _log.warning(f"{tier_name} sell failed for ${pos.token_symbol}, will retry next tick")
        return False

    # Update DB
    sell_pct_of_original = (sell_tokens / original_tokens) * 100
    new_remaining = remaining_tokens - sell_tokens
    usd_val = float(pos.position_size_usd) * (sell_pct_of_original / 100) * (1 + pnl_pct / 100)

    tx_tag = ""
    if live_tx and live_tx.get("success"):
        tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]"
        sol_recv = float(live_tx.get("quote", {}).get("sol_received", 0))
    else:
        sol_recv = 0

    trade = PaperTrade(
        position_id=pos.id,
        action="SELL",
        pct_of_position=Decimal(str(round(sell_pct_of_original, 2))),
        price=Decimal(str(current_price)),
        usd_value=Decimal(str(round(usd_val, 2))),
        pnl_pct=Decimal(str(round(pnl_pct, 4))),
        reason=f"{tier_name} ({pnl_pct:+.1f}%){tx_tag}"[:100],
    )
    db.add(trade)

    pos.position_size_tokens_remaining = Decimal(str(new_remaining))
    # Update remaining_pct based on tokens
    new_remaining_pct = (new_remaining / original_tokens) * 100 if original_tokens > 0 else 0
    pos.remaining_pct = Decimal(str(round(new_remaining_pct, 2)))

    gain_x = current_price / float(pos.entry_price) if float(pos.entry_price) > 0 else 0
    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
    _log.info(f"{mode} {tier_name}: ${pos.token_symbol} sold {sell_tokens:,} tokens "
              f"({sell_pct_of_original:.0f}%) gain={gain_x:.2f}x SOL={sol_recv:.4f}")

    await _send_telegram(
        f"*{mode} {tier_name}: ${pos.token_symbol}*\n"
        f"Sold 25% ({sell_tokens:,} tokens)\n"
        f"Gain: {gain_x:.2f}x ({pnl_pct:+.1f}%)\n"
        f"SOL received: {sol_recv:.4f}\n"
        f"Remaining: {new_remaining_pct:.0f}%"
    )
    return True


# ---------------------------------------------------------------------------
# Main tick function
# ---------------------------------------------------------------------------

async def manage_position_tick(pos, db, config: dict | None = None) -> bool:
    """Run on every monitoring tick for every open position.
    Returns: True if position was modified, False if no action taken.
    """
    if config is None:
        config = EXIT_CONFIG

    # Merge EXIT_CONFIG defaults with any overrides
    cfg = {**EXIT_CONFIG, **config}

    # ----- 1. Get current price and liquidity from DexScreener -----
    price, mc, liquidity, _vol = await _get_price_mc_liquidity(pos.token_address)
    if price <= 0:
        return False

    entry_price = float(pos.entry_price)
    if entry_price <= 0:
        return False

    # Update current price/mc on position
    pos.current_price = Decimal(str(price))
    pos.current_mc = Decimal(str(mc))
    pos.last_updated = datetime.utcnow()

    remaining_tokens = int(pos.position_size_tokens_remaining or 0)
    if remaining_tokens <= 0:
        return False

    # ----- 2. OVERRIDES (check first, exit immediately) -----

    # 2a. Timeout: held longer than MAX_HOLD_MS
    age_ms = (datetime.utcnow() - pos.opened_at).total_seconds() * 1000
    if age_ms >= cfg["MAX_HOLD_MS"]:
        pnl_pct = ((price - entry_price) / entry_price) * 100
        _log.info(f"TIMEOUT: ${pos.token_symbol} held {age_ms/3600000:.1f}h, exiting")
        await _exit_all(pos, db, f"Timeout ({age_ms/3600000:.1f}h)", price, pnl_pct, cfg)
        return True

    # 2b. Liquidity collapse
    entry_liq = float(pos.entry_liquidity_usd or 0)
    if entry_liq > 0 and liquidity > 0:
        liq_ratio = liquidity / entry_liq
        if liq_ratio < cfg["LIQUIDITY_RUG_THRESHOLD"]:
            pnl_pct = ((price - entry_price) / entry_price) * 100
            _log.warning(f"LIQUIDITY RUG: ${pos.token_symbol} liq dropped to {liq_ratio:.0%} of entry")
            await _exit_all(pos, db, f"Liquidity rug ({liq_ratio:.0%})", price, pnl_pct, cfg)
            return True

    # ----- 3. Track high -----
    highest = max(float(pos.highest_price or price), price)
    pos.highest_price = Decimal(str(highest))

    # ----- 4. Calculate gain -----
    gain = price / entry_price  # 1.0 = breakeven, 2.0 = 2x
    pnl_pct = (gain - 1) * 100

    pos.pnl_pct = Decimal(str(round(pnl_pct, 4)))
    pnl_usd = (pnl_pct / 100) * float(pos.position_size_usd) * (float(pos.remaining_pct) / 100)
    pos.pnl_usd = Decimal(str(round(pnl_usd, 2)))

    modified = False
    current_stop = float(pos.stop_price) if pos.stop_price else entry_price * (1 - cfg["INITIAL_STOP_PCT"])

    # ----- 5. RATCHET (stop only moves UP) -----
    if gain >= cfg["TRAILING_ACTIVATE"] and not pos.trailing_active:
        pos.trailing_active = True
        _log.info(f"TRAILING ACTIVATED: ${pos.token_symbol} at {gain:.2f}x")
        modified = True

    if gain >= cfg["LOCK_2R_TRIGGER"]:
        new_stop = entry_price * (1 + (cfg["LOCK_2R_TRIGGER"] - 1) * (cfg["INITIAL_STOP_PCT"] / 0.40) * 2)
        # Simplified: at 2.2x trigger, lock stop at entry * 1.80
        new_stop = entry_price * 1.80
        if new_stop > current_stop:
            current_stop = new_stop
            modified = True
    elif gain >= cfg["LOCK_1R_TRIGGER"]:
        new_stop = entry_price * 1.40
        if new_stop > current_stop:
            current_stop = new_stop
            modified = True
    elif gain >= cfg["BREAKEVEN_TRIGGER"]:
        new_stop = entry_price  # breakeven
        if new_stop > current_stop:
            current_stop = new_stop
            modified = True

    # ----- 6. TRAILING STOP (if active) -----
    if pos.trailing_active:
        new_trail = highest * (1 - cfg["TRAILING_DISTANCE_PCT"])
        if new_trail > current_stop:
            current_stop = new_trail
            modified = True

    # Persist stop price
    pos.stop_price = Decimal(str(round(current_stop, 10)))

    # ----- 7. TIERED SCALE-OUTS (each fires once, sells 25% of ORIGINAL) -----
    if gain >= cfg["TIER_1_TRIGGER"] and not pos.tier_1_done:
        success = await _tier_sell(pos, db, "TIER-1 (1.5x)", price, pnl_pct)
        if success:
            pos.tier_1_done = True
            modified = True

    if gain >= cfg["TIER_2_TRIGGER"] and not pos.tier_2_done:
        success = await _tier_sell(pos, db, "TIER-2 (2x)", price, pnl_pct)
        if success:
            pos.tier_2_done = True
            modified = True

    if gain >= cfg["TIER_3_TRIGGER"] and not pos.tier_3_done:
        success = await _tier_sell(pos, db, "TIER-3 (3x)", price, pnl_pct)
        if success:
            pos.tier_3_done = True
            modified = True

    # ----- 8. STOP CHECK (last) -----
    remaining_tokens = int(pos.position_size_tokens_remaining or 0)
    if remaining_tokens > 0 and price <= current_stop:
        reason = "Trailing stop" if pos.trailing_active else "Stop loss"
        gain_at_stop = current_stop / entry_price if entry_price > 0 else 0
        _log.info(f"STOP HIT: ${pos.token_symbol} price={price:.10f} <= stop={current_stop:.10f} "
                   f"({reason}, gain={gain:.2f}x)")
        await _exit_all(pos, db, f"{reason} ({gain:.2f}x)", price, pnl_pct, cfg)
        return True

    # Close position if fully sold via tiers
    if int(pos.position_size_tokens_remaining or 0) <= 0 and pos.status == "OPEN":
        pos.status = "CLOSED"
        pos.closed_at = datetime.utcnow()
        if not pos.close_reason:
            pos.close_reason = "Fully sold via tier scale-outs"
        modified = True

    return modified
