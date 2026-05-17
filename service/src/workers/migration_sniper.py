# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Migration Sniper — enters tokens at 90-99% bonding curve, sells post-migration on Raydium.

Strategy: Tokens that reach 90%+ curve have proven massive demand. We buy the last
few % of the curve, ride the migration to Raydium, and sell into the fresh AMM liquidity.
The migration event itself creates a breakout as Jupiter/DEX aggregators pick up the token.

Data: PumpPortal WebSocket (shared with regular sniper)
Entry: PumpPortal trade-local API
Exit: Jupiter swap API (post-migration, token is on Raydium)
"""
import asyncio
import base64
import json as _json
import logging
import os
import time
from datetime import datetime
from decimal import Decimal

import websockets
import httpx
from sqlalchemy import select, func, String, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("migration-sniper")

_PP_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "")
PUMPPORTAL_WS = f"wss://pumpportal.fun/api/data?api-key={_PP_API_KEY}" if _PP_API_KEY else "wss://pumpportal.fun/api/data"
PUMPPORTAL_API = "https://pumpportal.fun/api"
GRADUATION_SOL = 85

MIGRATION_WALLET_KEY_ENV = "MIGRATION_WALLET_PRIVATE_KEY"


# === CONFIG ===

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    # Position sizing
    "position_size_sol": 0.10,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "min_sol_reserve": 0.10,

    # Entry filters — tight zone right before graduation
    "graduation_real_sol": 85,
    "min_real_sol": 72,
    "max_real_sol": 75,
    "min_holders": 5,
    "max_token_age_minutes": 360,

    # Exit — post-migration momentum (data-driven)
    # Hold through graduation, sell into Raydium pump
    "tp1_pct": 30,
    "tp1_sell_pct": 34,
    "tp2_pct": 60,
    "tp2_sell_pct": 33,
    "trailing_stop_pct": 20,
    "hard_stop_pct": 25,
    "no_migration_timeout_minutes": 5,
    "post_migration_max_hold_minutes": 10,

    # Execution
    "slippage_pct": 15,
    "buy_priority_fee_sol": 0.005,
    "sell_priority_fee_sol": 0.01,
    "priority_fee_sol": 0.001,
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("migration_sniper_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class MigrationPosition(Base):
    __tablename__ = "migration_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(66), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entry_price_sol: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_curve_pct: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    entry_holders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    tokens_held: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    current_price_sol: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price_sol: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    migrated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash_buy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_mig_status", "status"),
        Index("idx_mig_mint", "mint"),
    )


class MigrationTrade(Base):
    __tablename__ = "migration_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price_sol: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === STATE ===

_daily_loss_sol = 0.0
_daily_loss_date = ""
_tracked_tokens: dict[str, dict] = {}
_seen_mints: set[str] = set()
# In-memory position cache for instant sell — no DB query needed
# mint -> {pos_id, entry_price, remaining_pct, symbol}
_open_positions: dict[str, dict] = {}
_sell_threshold_sol: float = 83.3  # pre-computed: 85 * 98/100
_stop_loss_pct: float = 50.0
_evaluating: set[str] = set()  # mints currently being evaluated (prevents concurrent entry)


async def _track_daily_loss(loss: float):
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    _daily_loss_sol += loss


async def _get_daily_loss() -> float:
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    return _daily_loss_sol


# === HELPERS ===

async def _send_telegram(text: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                                    "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


def _calc_price_from_curve(v_sol: float, v_tokens: float) -> float:
    if v_tokens <= 0:
        return 0
    return v_sol / v_tokens


def _calc_curve_pct(v_sol: float) -> float:
    return (v_sol / GRADUATION_SOL) * 100


async def _get_dexscreener_price(mint: str) -> tuple[float, float]:
    """Get price in SOL and USD from DexScreener (works post-migration on Raydium)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    pair = pairs[0]
                    price_usd = float(pair.get("priceUsd", 0) or 0)
                    price_native = float(pair.get("priceNative", 0) or 0)
                    return price_native, price_usd
    except Exception:
        pass
    return 0, 0


# === EXECUTION ===

async def _execute_buy(mint: str, amount_sol: float, config: dict) -> dict:
    if config.get("paper_mode", True):
        return {"success": True, "tx_hash": "paper_mode"}

    pk = os.getenv(MIGRATION_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{MIGRATION_WALLET_KEY_ENV} not set"}

    try:
        if pk.startswith("["):
            from solders.keypair import Keypair
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            from solders.keypair import Keypair
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{PUMPPORTAL_API}/trade-local", json={
                "publicKey": str(keypair.pubkey()),
                "action": "buy",
                "mint": mint,
                "amount": amount_sol,
                "denominatedInSol": "true",
                "slippage": config.get("slippage_pct", 15),
                "priorityFee": config.get("priority_fee_sol", 0.001),
                "pool": "pump",
            }, timeout=15)

            if resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": f"Pumpportal: {resp.status_code}"}

            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(resp.content)
            signed_tx = VersionedTransaction(tx.message, [keypair])

            helius_key = os.getenv("HELIUS_API_KEY", "")
            if helius_key:
                rpc = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            else:
                rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
            }, timeout=30)

            resp_json = send_resp.json()
            if send_resp.status_code == 200 and "result" in resp_json:
                return {"success": True, "tx_hash": resp_json["result"]}

            err_msg = resp_json.get("error", {}).get("message", "Send failed") if isinstance(resp_json.get("error"), dict) else str(resp_json.get("error", "Send failed"))
            return {"success": False, "tx_hash": None, "error": err_msg}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


async def _execute_sell_jupiter(mint: str, pct_of_remaining: float, config: dict) -> dict:
    """Sell via Jupiter (post-migration, token is on Raydium)."""
    if config.get("paper_mode", True):
        return {"success": True, "tx_hash": "paper_mode"}

    pk = os.getenv(MIGRATION_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{MIGRATION_WALLET_KEY_ENV} not set"}

    try:
        if pk.startswith("["):
            from solders.keypair import Keypair
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            from solders.keypair import Keypair
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        from ..services.jupiter_swap import get_token_balance
        balance = await get_token_balance(str(keypair.pubkey()), mint)
        if balance <= 0:
            return {"success": False, "tx_hash": None, "error": "No balance"}

        sell_amount = int(balance * (pct_of_remaining / 100))
        if sell_amount <= 0:
            return {"success": False, "tx_hash": None, "error": "Amount too small"}

        sol_mint = "So11111111111111111111111111111111111111112"
        jupiter_api = "https://api.jup.ag/swap/v1"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

        async with httpx.AsyncClient() as client:
            quote_resp = await client.get(f"{jupiter_api}/quote", params={
                "inputMint": mint, "outputMint": sol_mint,
                "amount": str(sell_amount),
                "slippageBps": int(config.get("slippage_pct", 15) * 100),
            }, timeout=10)
            if quote_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Quote failed"}

            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                "quoteResponse": quote_resp.json(),
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if swap_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Swap failed"}

            swap_tx = swap_resp.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "tx_hash": None, "error": "No swap tx"}

            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
            signed_tx = VersionedTransaction(tx.message, [keypair])

            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=30)

            if send_resp.status_code == 200 and "result" in send_resp.json():
                return {"success": True, "tx_hash": send_resp.json()["result"]}

            return {"success": False, "tx_hash": None, "error": "Send failed"}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


# === SIGNAL EVALUATION ===

async def _evaluate_token(token: dict, config: dict) -> str | None:
    """Evaluate a token for migration sniping using real_sol_reserves."""
    mint = token.get("mint", "")
    symbol = token.get("symbol", "")
    v_sol = token.get("v_sol", 0)
    v_tokens = token.get("v_tokens", 0)
    real_sol = token.get("real_sol", 0)

    # === DEDUP GATE (single source of truth) ===
    # _evaluating: mint is currently being evaluated by another coroutine
    # _seen_mints: mint was already bought (or attempted) — permanent block
    # _open_positions: mint has an open position
    # All three checks happen BEFORE any await, so no race condition.
    if not mint:
        return None
    if mint in _seen_mints:
        _log.debug(f"EVAL BLOCK ${symbol}: in _seen_mints (already bought/attempted)")
        return None
    if mint in _open_positions:
        _log.debug(f"EVAL BLOCK ${symbol}: has open position")
        return None
    if mint in _evaluating:
        _log.debug(f"EVAL BLOCK ${symbol}: already being evaluated by another coroutine")
        return None
    _evaluating.add(mint)  # Claim slot synchronously — no await between check and add

    try:
        _log.info(f"EVAL START ${symbol} mint={mint[:16]}... real_sol={real_sol:.1f}")

        daily_loss = await _get_daily_loss()
        if daily_loss >= config["daily_loss_limit_sol"]:
            _log.info(f"EVAL SKIP ${symbol}: daily loss {daily_loss:.3f} >= limit {config['daily_loss_limit_sol']}")
            return None

        grad_sol = config.get("graduation_real_sol", 85)
        min_sol = config.get("min_real_sol", 72)
        max_sol = config.get("max_real_sol", 84)

        if real_sol < min_sol or real_sol > max_sol:
            _log.info(f"EVAL SKIP ${symbol}: real_sol={real_sol:.1f} outside range [{min_sol}, {max_sol}]")
            return None

        async with async_session() as db:
            open_count = (await db.execute(
                select(func.count()).select_from(MigrationPosition)
                .where(MigrationPosition.status == "OPEN")
            )).scalar() or 0
            if open_count >= config["max_open_positions"]:
                _log.info(f"EVAL SKIP ${symbol}: {open_count} open positions >= max {config['max_open_positions']}")
                return None

            ever_traded = (await db.execute(
                select(func.count()).select_from(MigrationPosition)
                .where(MigrationPosition.mint == mint)
            )).scalar() or 0
            if ever_traded > 0:
                _seen_mints.add(mint)
                _log.info(f"EVAL SKIP ${symbol}: already traded (found in DB)")
                return None

        # Holder check — skip Helius entirely (credits exhausted), use passed-in count
        holders = token.get("holders", -1)
        if isinstance(holders, set):
            holders = len(holders)
        min_holders = config.get("min_holders", 5)
        if holders >= 0 and holders < min_holders:
            _log.info(f"SKIP {symbol}: {holders} holders < {min_holders}")
            return None

        # Security check — honeypot, sell tax, top10 holders
        try:
            from ..services.gmgn_client import get_token_security
            sec_result = await get_token_security(mint)
            if sec_result:
                sec_data = sec_result.get("data", {})
                sec_fails = []
                if sec_data.get("is_honeypot") == "yes":
                    sec_fails.append("honeypot")
                if float(sec_data.get("sell_tax", 0) or 0) > 0.10:
                    sec_fails.append("high_sell_tax")
                top10 = float(sec_data.get("top_10_holder_rate", 0) or 0)
                if top10 > 0.40:
                    sec_fails.append(f"top10={top10:.0%}")
                if sec_fails:
                    _log.info(f"SKIP {symbol}: security fail — {', '.join(sec_fails)}")
                    return None
        except Exception as e:
            _log.debug(f"GMGN security check error for ${symbol}: {e} — proceeding")

        price_sol = _calc_price_from_curve(v_sol, v_tokens) if v_sol > 0 and v_tokens > 0 else 0
        sol_remaining = grad_sol - real_sol
        curve_pct = (real_sol / grad_sol) * 100

        # Momentum check — don't buy into a dump
        # NOTE: DexScreener often has NO data for pump.fun tokens pre-migration
        # (they're not on any DEX yet). We MUST NOT reject tokens just because
        # DexScreener returns no pairs or low volume — that's expected for
        # bonding curve tokens. Only reject on NEGATIVE signals when data exists.
        try:
            async with httpx.AsyncClient() as client:
                ds_resp = await client.get(
                    f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", timeout=5)
                if ds_resp.status_code == 200:
                    ds_data = ds_resp.json()
                    ds_pairs = ds_data if isinstance(ds_data, list) else ds_data.get("pairs", [])
                    if ds_pairs:
                        ds_pair = ds_pairs[0]
                        ds_pc = ds_pair.get("priceChange", {})
                        ds_txns = ds_pair.get("txns", {})
                        m5_change = float(ds_pc.get("m5", 0) or 0)
                        m5_buys = int(ds_txns.get("m5", {}).get("buys", 0) or 0)
                        m5_sells = int(ds_txns.get("m5", {}).get("sells", 0) or 0)

                        if m5_change < -10:
                            _log.info(f"SKIP {symbol}: dumping 5m={m5_change:.0f}%")
                            return None
                        if m5_sells > 0 and m5_buys / max(m5_sells, 1) < 0.5:
                            _log.info(f"SKIP {symbol}: sell pressure {m5_buys}b/{m5_sells}s")
                            return None

                        # Volume/MC checks ONLY when DexScreener has meaningful data.
                        # Pre-migration pump.fun tokens won't have DEX pairs, so
                        # missing data is NOT a rejection signal.
                    # else: no pairs on DexScreener — expected for pump.fun tokens, proceed
        except Exception as e:
            _log.debug(f"DexScreener momentum check error for ${symbol}: {e} — proceeding")

        # Creator rug history check — GMGN wallet data
        try:
            from ..services.gmgn_client import get_wallet_stats
            creator = token.get("dev", "") or token.get("creator", "")
            if creator:
                creator_data = await get_wallet_stats(creator)
                if creator_data:
                    stats = creator_data.get("data", creator_data)
                    if isinstance(stats, dict):
                        wr = float(stats.get("winrate", 0) or 0)
                        total = int(stats.get("total_trades", 0) or 0)
                        if total >= 5 and wr < 0.20:
                            _log.info(f"SKIP {symbol}: creator WR={wr:.0%} over {total} trades (serial rugger)")
                            return None
        except Exception as e:
            _log.debug(f"Creator rug check error for ${symbol}: {e} — proceeding")

        _log.info(f"🚀 MIGRATION SNIPE: ${symbol} real_sol={real_sol:.1f}/{grad_sol} ({curve_pct:.0f}%) "
                  f"on-chain holders={holders} {sol_remaining:.1f}SOL to graduation")

        result = await _execute_buy(mint, config["position_size_sol"], config)
        mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

        if result.get("success"):
            # Buy succeeded — permanently block this mint
            _seen_mints.add(mint)
            tokens_received = (config["position_size_sol"] / price_sol) if price_sol > 0 else 0

            async with async_session() as db:
                pos = MigrationPosition(
                    mint=mint,
                    symbol=symbol[:20] if symbol else None,
                    name=token.get("name", "")[:100] if token.get("name") else None,
                    entry_price_sol=Decimal(str(price_sol)),
                    entry_curve_pct=Decimal(str(round(curve_pct, 2))),
                    entry_holders=holders,
                    position_size_sol=Decimal(str(config["position_size_sol"])),
                    tokens_held=Decimal(str(round(tokens_received, 2))),
                    current_price_sol=Decimal(str(price_sol)),
                    highest_price_sol=Decimal(str(price_sol)),
                    tx_hash_buy=result.get("tx_hash"),
                )
                db.add(pos)
                await db.flush()

                trade = MigrationTrade(
                    position_id=pos.id, action="BUY",
                    price_sol=Decimal(str(price_sol)),
                    amount_sol=Decimal(str(config["position_size_sol"])),
                    reason=f"Migration snipe at {curve_pct:.0f}% curve ({sol_remaining:.1f} SOL left)",
                    tx_hash=result.get("tx_hash"),
                )
                db.add(trade)
                await db.commit()

            # Cache position in memory for instant sell — zero-latency lookup
            _open_positions[mint] = {
                "pos_id": pos.id, "entry_price": price_sol, "symbol": symbol,
                "remaining_pct": 100.0,
            }
            # Pre-compute thresholds
            _sell_threshold_sol = 85 * config.get("sell_curve_pct", 98) / 100
            _stop_loss_pct = config.get("stop_loss_pct", 50)

            _log.info(f"{mode} MIGRATION BUY: ${symbol} @ {curve_pct:.0f}% curve, "
                      f"{config['position_size_sol']} SOL, {sol_remaining:.1f} SOL to migration")
            volume_sol = token.get("volume_sol", 0)
            await _send_telegram(
                f"🚀 *{mode} MIGRATION SNIPE: ${symbol}*\n\n"
                f"Curve: {curve_pct:.0f}% ({sol_remaining:.1f} SOL to graduation)\n"
                f"Holders: {holders}\n"
                f"Volume: {volume_sol:.1f} SOL\n"
                f"Size: {config['position_size_sol']} SOL\n"
                f"[pump.fun](https://pump.fun/{mint})"
            )
            return mint
        else:
            _log.warning(f"MIGRATION BUY FAILED: ${symbol} — {result.get('error')}")
            _seen_mints.add(mint)  # Block failed mints too — don't retry
            return None
    finally:
        # Release the concurrent-entry lock so this mint can be re-evaluated
        # if it failed checks (holders, momentum, etc.) but conditions change.
        # Permanent blocking is handled by _seen_mints (set on successful or failed buy).
        _evaluating.discard(mint)


# === POSITION MANAGEMENT ===

async def _manage_positions(config: dict):
    """Post-migration momentum: hold through graduation, sell into Raydium pump.
    Data shows curve sells at 98% only work 15% of the time. The real winners are
    tokens that graduate and pump on Raydium (+30% to +141% in paper data)."""
    async with async_session() as db:
        positions = (await db.execute(
            select(MigrationPosition).where(MigrationPosition.status == "OPEN")
        )).scalars().all()

    for pos in positions:
        try:
            token_state = _tracked_tokens.get(pos.mint, {})
            age_sec = (datetime.utcnow() - pos.opened_at).total_seconds()
            age_min = age_sec / 60
            remaining = float(pos.remaining_pct)

            # Price: use curve price pre-migration, DexScreener post-migration
            v_sol = token_state.get("v_sol", 0)
            v_tokens = token_state.get("v_tokens", 0)
            if pos.migrated:
                price_sol, _ = await _get_dexscreener_price(pos.mint)
            else:
                price_sol = _calc_price_from_curve(v_sol, v_tokens) if v_sol > 0 and v_tokens > 0 else 0
                if price_sol <= 0:
                    price_sol, _ = await _get_dexscreener_price(pos.mint)

            entry = float(pos.entry_price_sol)
            if entry <= 0 and price_sol > 0:
                entry = price_sol
                async with async_session() as db:
                    p = await db.get(MigrationPosition, pos.id)
                    if p:
                        p.entry_price_sol = Decimal(str(price_sol))
                        await db.commit()

            pnl_pct = ((price_sol - entry) / entry * 100) if entry > 0 and price_sol > 0 else 0
            highest = max(float(pos.highest_price_sol or price_sol), price_sol) if price_sol > 0 else float(pos.highest_price_sol or 0)
            highest_pnl = ((highest - entry) / entry * 100) if entry > 0 and highest > 0 else 0

            if price_sol > 0:
                async with async_session() as db:
                    p = await db.get(MigrationPosition, pos.id)
                    if p:
                        p.current_price_sol = Decimal(str(price_sol))
                        p.highest_price_sol = Decimal(str(highest))
                        p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                        await db.commit()

            # === HARD STOP — safety net for rugs ===
            hard_stop = config.get("hard_stop_pct", 25)
            if price_sol > 0 and pnl_pct <= -hard_stop:
                await _close_position(pos, remaining,
                                      f"Hard stop {pnl_pct:.1f}%", pnl_pct, config)
                continue

            # === PRE-MIGRATION: wait for graduation, timeout if stalled ===
            if not pos.migrated:
                no_mig_min = config.get("no_migration_timeout_minutes", 5)
                if age_min >= no_mig_min:
                    await _close_position(pos, remaining,
                                          f"No migration after {age_min:.0f}min ({pnl_pct:+.1f}%)",
                                          pnl_pct, config)
                    continue
                # Pre-migration: just hold and wait for graduation
                continue

            # === POST-MIGRATION: ratcheting TPs + trailing stop ===
            if price_sol <= 0:
                continue

            # TP1: sell 34% at +30%
            tp1 = config.get("tp1_pct", 30)
            tp1_sell = config.get("tp1_sell_pct", 34)
            if pnl_pct >= tp1 and remaining > (100 - tp1_sell):
                _log.info(f"💰 TP1: ${pos.symbol} +{pnl_pct:.0f}% — selling {tp1_sell}%")
                await _close_position(pos, tp1_sell,
                                      f"TP1 +{pnl_pct:.0f}% (sell {tp1_sell}%)",
                                      pnl_pct, config)
                continue

            # TP2: sell 33% at +60%
            tp2 = config.get("tp2_pct", 60)
            tp2_sell = config.get("tp2_sell_pct", 33)
            if pnl_pct >= tp2 and remaining > (100 - tp1_sell - tp2_sell + 1):
                _log.info(f"💰 TP2: ${pos.symbol} +{pnl_pct:.0f}% — selling {tp2_sell}%")
                await _close_position(pos, tp2_sell,
                                      f"TP2 +{pnl_pct:.0f}% (sell {tp2_sell}%)",
                                      pnl_pct, config)
                continue

            # Trailing stop: 20% from peak, only after TP1 hit
            trail_pct = config.get("trailing_stop_pct", 20)
            if highest_pnl >= tp1 and pnl_pct < highest_pnl * (1 - trail_pct / 100):
                await _close_position(pos, remaining,
                                      f"Trail stop ({pnl_pct:+.1f}%, peak +{highest_pnl:.0f}%)",
                                      pnl_pct, config)
                continue

            # Post-migration max hold
            post_max = config.get("post_migration_max_hold_minutes", 10)
            if age_min >= post_max:
                await _close_position(pos, remaining,
                                      f"Post-migration timeout {age_min:.0f}min ({pnl_pct:+.1f}%)",
                                      pnl_pct, config)
                continue

        except Exception as e:
            _log.debug(f"Position manage error {pos.symbol}: {e}")


async def _sell_pumpportal(mint: str, tokens_to_sell: float, config: dict) -> dict:
    """Sell on pump.fun bonding curve via PumpPortal."""
    try:
        pk = os.getenv(MIGRATION_WALLET_KEY_ENV, "")
        if not pk:
            return {"success": False, "error": "no key"}
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        if pk.startswith("["):
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{PUMPPORTAL_API}/trade-local", json={
                "publicKey": str(keypair.pubkey()),
                "action": "sell",
                "mint": mint,
                "amount": tokens_to_sell,
                "denominatedInSol": "false",
                "slippage": config.get("slippage_pct", 15),
                "priorityFee": config.get("sell_priority_fee_sol", 0.01),
                "pool": "pump",
            }, timeout=5)
            if resp.status_code != 200:
                return {"success": False, "error": f"PP {resp.status_code}"}

            tx = VersionedTransaction.from_bytes(resp.content)
            signed_tx = VersionedTransaction(tx.message, [keypair])
            _hk = os.getenv("HELIUS_API_KEY", "")
            rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
            }, timeout=10)
            if send_resp.status_code == 200 and "result" in send_resp.json():
                return {"success": True, "tx_hash": send_resp.json()["result"], "via": "pumpportal"}
            return {"success": False, "error": "send failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _sell_jupiter(mint: str, config: dict) -> dict:
    """Sell ALL tokens on Raydium via Jupiter (post-migration fallback)."""
    try:
        pk = os.getenv(MIGRATION_WALLET_KEY_ENV, "")
        if not pk:
            return {"success": False, "error": "no key"}
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        if pk.startswith("["):
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        # Get token balance
        sol_mint = "So11111111111111111111111111111111111111112"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        jupiter_api = "https://api.jup.ag/swap/v1"

        async with httpx.AsyncClient() as client:
            bal_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [str(keypair.pubkey()), {"mint": mint}, {"encoding": "jsonParsed"}],
            }, timeout=5)
            raw_amount = 0
            if bal_resp.status_code == 200:
                for acc in bal_resp.json().get("result", {}).get("value", []):
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    if info.get("mint") == mint:
                        raw_amount = int(info.get("tokenAmount", {}).get("amount", 0))
            if raw_amount <= 0:
                return {"success": False, "error": "no balance"}

            quote_resp = await client.get(f"{jupiter_api}/quote", params={
                "inputMint": mint, "outputMint": sol_mint,
                "amount": str(raw_amount),
                "slippageBps": int(config.get("slippage_pct", 15) * 100),
            }, timeout=5)
            if quote_resp.status_code != 200:
                return {"success": False, "error": "quote failed"}

            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                "quoteResponse": quote_resp.json(),
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            }, timeout=5)
            if swap_resp.status_code != 200:
                return {"success": False, "error": "swap failed"}

            swap_tx = swap_resp.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "error": "no swap tx"}

            tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
            signed_tx = VersionedTransaction(tx.message, [keypair])
            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=10)
            if send_resp.status_code == 200 and "result" in send_resp.json():
                return {"success": True, "tx_hash": send_resp.json()["result"], "via": "jupiter"}
            return {"success": False, "error": "send failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _close_position(pos: MigrationPosition, sell_pct: float, reason: str,
                          pnl_pct: float, config: dict):
    """Sell part or all of a migration position. In live mode, fires BOTH
    PumpPortal (bonding curve) and Jupiter (Raydium) simultaneously —
    whichever pool exists gets the sell."""
    actual_remaining = float(pos.remaining_pct)
    sell_fraction = sell_pct / actual_remaining if actual_remaining > 0 else 1
    tokens_to_sell = float(pos.tokens_held) * sell_fraction
    remaining = actual_remaining - sell_pct

    if config.get("paper_mode", True):
        result = {"success": True, "tx_hash": "paper_mode", "via": "paper"}
    else:
        # PARALLEL SELL — fire both PumpPortal and Jupiter simultaneously
        # Only one can succeed (token is either on curve OR on Raydium)
        _log.info(f"LIVE SELL: ${pos.symbol} firing parallel PP + Jupiter...")
        pp_task = asyncio.create_task(_sell_pumpportal(pos.mint, tokens_to_sell, config))
        jup_task = asyncio.create_task(_sell_jupiter(pos.mint, config))

        pp_result, jup_result = await asyncio.gather(pp_task, jup_task, return_exceptions=True)

        # Use whichever succeeded
        if isinstance(pp_result, dict) and pp_result.get("success"):
            result = pp_result
            _log.info(f"LIVE SELL SUCCESS via PumpPortal: ${pos.symbol} tx={result.get('tx_hash', '')[:20]}...")
        elif isinstance(jup_result, dict) and jup_result.get("success"):
            result = jup_result
            _log.info(f"LIVE SELL SUCCESS via Jupiter: ${pos.symbol} tx={result.get('tx_hash', '')[:20]}...")
        else:
            pp_err = pp_result.get("error", "?") if isinstance(pp_result, dict) else str(pp_result)
            jup_err = jup_result.get("error", "?") if isinstance(jup_result, dict) else str(jup_result)
            _log.warning(f"LIVE SELL FAILED: ${pos.symbol} PP={pp_err} JUP={jup_err}")
            result = {"success": False, "tx_hash": None, "error": f"PP={pp_err} JUP={jup_err}"}

    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    async with async_session() as db:
        p = await db.get(MigrationPosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(remaining, 0)))
            p.tokens_held = Decimal(str(max(float(p.tokens_held) - tokens_to_sell, 0)))
            if remaining <= 0:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()

            price_sol = float(p.current_price_sol or p.entry_price_sol)
            sol_value = float(p.position_size_sol) * (sell_pct / 100) * (1 + pnl_pct / 100)

            trade = MigrationTrade(
                position_id=pos.id, action="SELL",
                price_sol=Decimal(str(price_sol)),
                amount_sol=Decimal(str(round(sol_value, 6))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
                tx_hash=result.get("tx_hash"),
            )
            db.add(trade)
            await db.commit()

        # Clean up in-memory position cache when fully closed
        if remaining <= 0 and pos.mint in _open_positions:
            del _open_positions[pos.mint]

        if pnl_pct < 0 and remaining <= 0:
            loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
            await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{mode} MIGRATION SELL: ${pos.symbol} {sell_pct:.0f}% @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} MIGRATION SELL: ${pos.symbol}*\n"
        f"{reason}\n"
        f"Sold {sell_pct:.0f}%, PnL: {pnl_pct:+.1f}%\n"
        f"{'Migrated ✓' if pos.migrated else 'Pre-migration'}"
    )


# === GRADUATION SCANNER ===

async def _scan_graduating_tokens(ws, scan_config: dict = None) -> int:
    """Poll pump.fun for tokens near graduation (king-of-the-hill page) and subscribe to them."""
    found = 0
    try:
        async with httpx.AsyncClient() as client:
            # Use pump.fun frontend API to get tokens sorted by market cap (near graduation)
            resp = await client.get(
                "https://frontend-api-v3.pump.fun/coins/currently-live",
                params={"limit": 50, "offset": 0, "includeNsfw": "false"},
                headers={"User-Agent": "AgiotageMigrationSniper/1.0"},
                timeout=10,
            )
            if resp.status_code != 200:
                # Fallback: try the king-of-the-hill endpoint
                resp = await client.get(
                    "https://frontend-api-v3.pump.fun/coins/king-of-the-hill",
                    params={"limit": 50, "includeNsfw": "false"},
                    headers={"User-Agent": "AgiotageMigrationSniper/1.0"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    _log.debug(f"Pump.fun API returned {resp.status_code}")
                    return 0

            tokens = resp.json()
            if not isinstance(tokens, list):
                return 0

            _cfg = scan_config or DEFAULT_CONFIG
            grad_sol = _cfg.get("graduation_real_sol", 85)
            min_track_sol = 50.0  # track from 50 SOL — gives time to subscribe before entry zone

            for tok in tokens:
                mint = tok.get("mint", "")
                if not mint or mint in _tracked_tokens or mint in _seen_mints:
                    continue

                if tok.get("complete"):
                    continue

                real_sol_raw = float(tok.get("real_sol_reserves", 0) or 0)
                real_sol = real_sol_raw / 1e9
                mc_usd = float(tok.get("usd_market_cap", 0) or 0)
                v_sol_lamports = float(tok.get("virtual_sol_reserves", 0) or 0)
                v_tokens_raw = float(tok.get("virtual_token_reserves", 0) or 0)
                v_sol = v_sol_lamports / 1e9

                if real_sol >= min_track_sol and real_sol < grad_sol:
                    curve_pct = (real_sol / grad_sol) * 100
                    symbol = tok.get("symbol", "?")
                    _tracked_tokens[mint] = {
                        "symbol": symbol,
                        "name": tok.get("name", ""),
                        "dev": tok.get("creator", ""),
                        "v_sol": v_sol,
                        "v_tokens": v_tokens_raw / 1e6,
                        "real_sol": real_sol,
                        "created_at": time.time(),
                        "holders": set(),
                        "buys": 0,
                        "sells": 0,
                        "volume_sol": real_sol,
                        "last_trade_time": time.time(),
                    }
                    await ws.send(_json.dumps({
                        "method": "subscribeTokenTrade",
                        "keys": [mint],
                    }))
                    found += 1
                    _log.info(f"SCAN: ${symbol} real_sol={real_sol:.1f}/{grad_sol} ({curve_pct:.0f}%) MC=${mc_usd:,.0f} mint={mint[:16]}...")

    except Exception as e:
        _log.debug(f"Graduation scan error: {e}")
    return found


# === WEBSOCKET LOOP ===

async def _run_websocket(config: dict):
    while True:
        try:
            _log.info(f"Migration Sniper connecting to PumpPortal WebSocket")
            async with websockets.connect(
                PUMPPORTAL_WS,
                ping_interval=30,
                open_timeout=30,
                close_timeout=5,
                additional_headers={"User-Agent": "AgiotageMigrationSniper/1.0"},
                max_size=2**20,
            ) as ws:
                _log.info("PumpPortal WebSocket connected (migration sniper)")

                await ws.send(_json.dumps({"method": "subscribeNewToken"}))

                # Initial scan for tokens already near graduation
                found = await _scan_graduating_tokens(ws, config)
                _log.info(f"Initial scan: tracking {found} near-graduation tokens")

                async with async_session() as db:
                    open_positions = (await db.execute(
                        select(MigrationPosition).where(MigrationPosition.status == "OPEN")
                    )).scalars().all()
                    for pos in open_positions:
                        await ws.send(_json.dumps({
                            "method": "subscribeTokenTrade",
                            "keys": [pos.mint],
                        }))

                async for message in ws:
                    try:
                        data = _json.loads(message)
                        tx_type = data.get("txType", "")

                        if tx_type == "create":
                            mint = data.get("mint", "")
                            v_sol = float(data.get("vSolInBondingCurve", 0) or 0)
                            curve = _calc_curve_pct(v_sol)
                            # Track tokens that are already at 70%+ (close to migration zone)
                            if mint and curve >= 85:
                                _tracked_tokens[mint] = {
                                    "symbol": data.get("symbol", ""),
                                    "name": data.get("name", ""),
                                    "dev": data.get("traderPublicKey", ""),
                                    "v_sol": v_sol,
                                    "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                    "created_at": time.time(),
                                    "holders": set(),
                                    "buys": 0,
                                    "sells": 0,
                                    "volume_sol": float(data.get("initialBuy", 0) or 0),
                                    "last_trade_time": time.time(),
                                }
                                await ws.send(_json.dumps({
                                    "method": "subscribeTokenTrade",
                                    "keys": [mint],
                                }))

                        elif tx_type in ("buy", "sell"):
                            mint = data.get("mint", "")
                            if mint in _tracked_tokens:
                                t = _tracked_tokens[mint]
                                t["v_sol"] = float(data.get("vSolInBondingCurve", 0) or 0)
                                t["v_tokens"] = float(data.get("vTokensInBondingCurve", 0) or 0)
                                sol_amount = float(data.get("solAmount", 0) or 0)
                                trader = data.get("traderPublicKey", "")
                                t["last_trade_time"] = time.time()

                                if tx_type == "buy":
                                    t["buys"] = t.get("buys", 0) + 1
                                    t["volume_sol"] = t.get("volume_sol", 0) + sol_amount
                                    if trader and isinstance(t.get("holders"), set):
                                        t["holders"].add(trader)
                                else:
                                    t["sells"] = t.get("sells", 0) + 1

                                # Update real_sol from virtual reserves
                                # real_sol ≈ v_sol - 30 (virtual base)
                                t["real_sol"] = max(0, t["v_sol"] - 30)

                                # Check if token is in the pre-graduation zone
                                min_sol = config.get("min_real_sol", 72)
                                if t["real_sol"] >= min_sol * 0.90:
                                    eval_data = {
                                        "mint": mint,
                                        "symbol": t["symbol"],
                                        "name": t.get("name", ""),
                                        "v_sol": t["v_sol"],
                                        "v_tokens": t["v_tokens"],
                                        "real_sol": t["real_sol"],
                                        "holders": -1,
                                        "volume_sol": t.get("volume_sol", 0),
                                        "buys": t.get("buys", 0),
                                        "sells": t.get("sells", 0),
                                        "dev_pct": 0,
                                        "created_at": t.get("created_at"),
                                    }
                                    config = await get_config()
                                    new_mint = await _evaluate_token(eval_data, config)
                                    if new_mint:
                                        await ws.send(_json.dumps({
                                            "method": "subscribeTokenTrade",
                                            "keys": [new_mint],
                                        }))
                            else:
                                # Token not tracked yet — check if it's approaching migration
                                v_sol = float(data.get("vSolInBondingCurve", 0) or 0)
                                curve = _calc_curve_pct(v_sol)
                                if curve >= 85 and mint not in _seen_mints:
                                    _tracked_tokens[mint] = {
                                        "symbol": data.get("symbol", "?"),
                                        "name": "",
                                        "dev": "",
                                        "v_sol": v_sol,
                                        "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                        "created_at": time.time(),
                                        "holders": set(),
                                        "buys": 1 if data.get("txType") == "buy" else 0,
                                        "sells": 1 if data.get("txType") == "sell" else 0,
                                        "volume_sol": float(data.get("solAmount", 0) or 0),
                                        "last_trade_time": time.time(),
                                    }
                                    await ws.send(_json.dumps({
                                        "method": "subscribeTokenTrade",
                                        "keys": [mint],
                                    }))

                    except _json.JSONDecodeError:
                        pass
                    except Exception as e:
                        _log.debug(f"WS message error: {e}")

                    # Manage positions every ~10 seconds
                    now_sec = int(time.time())
                    if not hasattr(_run_websocket, '_last_manage') or \
                       now_sec - getattr(_run_websocket, '_last_manage', 0) >= 10:
                        _run_websocket._last_manage = now_sec
                        config = await get_config()
                        await _manage_positions(config)

                    # Re-scan for graduating tokens every 10 seconds
                    if not hasattr(_run_websocket, '_last_scan') or \
                       now_sec - getattr(_run_websocket, '_last_scan', 0) >= 10:
                        _run_websocket._last_scan = now_sec
                        await _scan_graduating_tokens(ws, config)

                    # Trim tracked tokens — protect near-graduation and open positions
                    if len(_tracked_tokens) > 500:
                        _trim_min = 50.0  # protect all tokens 50+ SOL from trim
                        oldest = sorted(_tracked_tokens.items(),
                                        key=lambda x: x[1].get("created_at", 0))[:250]
                        for mint, tdata in oldest:
                            if mint in _open_positions:
                                continue
                            if tdata.get("real_sol", 0) >= _trim_min:
                                continue
                            _tracked_tokens.pop(mint, None)

        except Exception as e:
            _log.error(f"Migration sniper WS error: {type(e).__name__}: {e}", exc_info=True)
            await asyncio.sleep(10)


# === MAIN LOOP ===

async def run():
    try:
        _log.info("Migration Sniper starting")
        await asyncio.sleep(5)

        try:
            async with async_session() as db:
                all_mints = (await db.execute(select(MigrationPosition.mint))).scalars().all()
                _seen_mints.update(all_mints)
                # Load open positions into memory cache for instant sell
                open_pos = (await db.execute(
                    select(MigrationPosition).where(MigrationPosition.status == "OPEN")
                )).scalars().all()
                for p in open_pos:
                    _open_positions[p.mint] = {
                        "pos_id": p.id, "entry_price": float(p.entry_price_sol),
                        "symbol": p.symbol or "?", "remaining_pct": float(p.remaining_pct),
                    }
                _log.info(f"Loaded {len(_seen_mints)} traded mints, {len(_open_positions)} open positions cached")
        except Exception as e:
            _log.warning(f"Could not load seen mints: {e}")

        config = await get_config()
        mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
        min_sol = config.get('min_real_sol', 72)
        max_sol = config.get('max_real_sol', 75)
        grad_sol = config.get('graduation_real_sol', 85)
        _log.info(f"Migration Sniper: {mode} — POST-MIGRATION MOMENTUM STRATEGY")
        _log.info(f"  Entry: {min_sol}-{max_sol} real_sol | Size: {config['position_size_sol']} SOL | Max: {config['max_open_positions']}")
        _log.info(f"  Exit: TP1 +{config.get('tp1_pct',30)}% sell {config.get('tp1_sell_pct',34)}%, "
                  f"TP2 +{config.get('tp2_pct',60)}% sell {config.get('tp2_sell_pct',33)}%, "
                  f"trail {config.get('trailing_stop_pct',20)}%, hard stop -{config.get('hard_stop_pct',25)}%")

        if not config.get("enabled"):
            _log.info("Migration sniper disabled in config")
            while True:
                await asyncio.sleep(300)

        # === PRODUCTION MODE: RPC + PumpPortal dual monitoring ===
        # PumpPortal WebSocket: catches every trade on every pump.fun token (real-time)
        # RPC accountSubscribe: block-level precision on specific tokens near graduation
        # Together: PumpPortal finds tokens moving fast, RPC locks on for precision entry

        from .migration_sniper_rpc import BondingCurveMonitor, discover_and_monitor

        if config.get("paper_mode", True):
            rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            ws_url = rpc_url.replace("https://", "wss://").replace("http://", "ws://")
            _log.info("RPC monitor using public RPC (paper mode — zero Helius usage)")
        else:
            helius_key = os.getenv("HELIUS_API_KEY", "")
            if helius_key:
                ws_url = f"wss://mainnet.helius-rpc.com/?api-key={helius_key}"
                _log.info("RPC monitor using Helius WebSocket (live mode)")
            else:
                rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
                ws_url = rpc_url.replace("https://", "wss://").replace("http://", "ws://")
                _log.info("RPC monitor using public RPC (no Helius key)")

        async def on_migration_ready(mint: str, info: dict, trigger: str):
            """Called by RPC monitor when a token hits the entry zone or graduates."""
            cfg = await get_config()

            if trigger == "graduated":
                async with async_session() as db:
                    pos = (await db.execute(
                        select(MigrationPosition)
                        .where(MigrationPosition.mint == mint, MigrationPosition.status == "OPEN")
                    )).scalar_one_or_none()
                    if pos:
                        pos.migrated = True
                        await db.commit()
                        _log.info(f"🎓 ${info['symbol']} MIGRATED — holding through, trailing stop active")
                        await _send_telegram(f"🎓 *${info['symbol']} MIGRATED!*\nHolding 100% — trailing stop active")
                        try:
                            from ..core.redis import redis_client
                            await redis_client.publish("graduation_events", _json.dumps({"mint": mint, "symbol": info.get('symbol', '?')}))
                        except Exception:
                            pass
                return

            symbol = info.get("symbol", "?")
            real_sol = info.get("real_sol", 0)

            token_data = {
                "mint": mint, "symbol": symbol,
                "v_sol": info.get("virtual_sol", 0) / 1e9 if info.get("virtual_sol") else 0,
                "v_tokens": info.get("virtual_tokens", 0) / 1e6 if info.get("virtual_tokens") else 0,
                "real_sol": real_sol, "holders": -1, "volume_sol": real_sol,
                "buys": 0, "sells": 0, "dev_pct": 0,
                "created_at": info.get("added_at"),
            }
            result = await _evaluate_token(token_data, cfg)
            if result:
                _log.info(f"RPC ENTRY: ${symbol} real_sol={real_sol:.2f}")

        monitor = BondingCurveMonitor(ws_url, on_migration_ready, config)
        _log.info(f"Starting dual monitor: PumpPortal WS + RPC (ws={ws_url[:40]}...)")

        _pp_subscribe_queue: asyncio.Queue = asyncio.Queue()

        async def pumpportal_feed():
            """PumpPortal WebSocket: catches pump.fun trades in real-time.
            Discovery loop pushes mints to _pp_subscribe_queue for subscription."""
            while True:
                try:
                    async with websockets.connect(
                        PUMPPORTAL_WS, ping_interval=30, open_timeout=30,
                        close_timeout=5, max_size=2**20,
                        additional_headers={"User-Agent": "AgiotageMigrationSniper/2.0"},
                    ) as ws:
                        _log.info("PumpPortal WebSocket connected (migration feed)")
                        # subscribeNewToken keeps connection alive and catches high-initial-buy tokens
                        await ws.send(_json.dumps({"method": "subscribeNewToken"}))
                        # Subscribe to any already-tracked tokens immediately
                        _pp_subscribed: set = set()
                        initial_mints = list(_tracked_tokens.keys())
                        if initial_mints:
                            await ws.send(_json.dumps({
                                "method": "subscribeTokenTrade",
                                "keys": initial_mints,
                            }))
                            _pp_subscribed.update(initial_mints)
                            _log.info(f"PP: initial subscribe to {len(initial_mints)} tokens")
                        _last_resub = time.time()

                        while True:
                            try:
                                message = await asyncio.wait_for(ws.recv(), timeout=3)
                            except asyncio.TimeoutError:
                                # No message — subscribe any new tracked tokens
                                new_mints = [m for m in _tracked_tokens if m not in _pp_subscribed]
                                if new_mints:
                                    await ws.send(_json.dumps({
                                        "method": "subscribeTokenTrade",
                                        "keys": new_mints,
                                    }))
                                    _pp_subscribed.update(new_mints)
                                    _log.info(f"PP: subscribed {len(new_mints)} tokens (total {len(_pp_subscribed)})")
                                continue

                            # Subscribe to new tracked tokens — check every message
                            new_mints = [m for m in _tracked_tokens if m not in _pp_subscribed]
                            if new_mints:
                                await ws.send(_json.dumps({
                                    "method": "subscribeTokenTrade",
                                    "keys": new_mints,
                                }))
                                _pp_subscribed.update(new_mints)
                                _log.info(f"PP: subscribed {len(new_mints)} tokens (total {len(_pp_subscribed)})")

                            try:
                                data = _json.loads(message)
                                tx_type = data.get("txType", "")
                                mint = data.get("mint", "")

                                if not mint:
                                    continue

                                v_sol = float(data.get("vSolInBondingCurve", 0) or 0)
                                real_sol = max(0, v_sol - 30)
                                cfg_live = await get_config()
                                grad_sol = cfg_live.get("graduation_real_sol", 85)
                                min_sol = cfg_live.get("min_real_sol", 72)

                                if tx_type == "create":
                                    symbol = data.get("symbol", "?")
                                    _tracked_tokens[mint] = {
                                        "symbol": symbol,
                                        "v_sol": v_sol,
                                        "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                        "real_sol": real_sol,
                                        "created_at": time.time(),
                                        "holders": set(),
                                        "buys": 0, "sells": 0,
                                        "volume_sol": real_sol,
                                        "last_trade_time": time.time(),
                                    }
                                    await ws.send(_json.dumps({
                                        "method": "subscribeTokenTrade", "keys": [mint]}))
                                    _pp_subscribed.add(mint)

                                elif tx_type in ("buy", "sell"):
                                    if mint in _tracked_tokens:
                                        t = _tracked_tokens[mint]
                                        t["v_sol"] = v_sol
                                        t["v_tokens"] = float(data.get("vTokensInBondingCurve", 0) or 0)
                                        t["real_sol"] = real_sol
                                        t["last_trade_time"] = time.time()
                                        trader = data.get("traderPublicKey", "")
                                        if tx_type == "buy":
                                            t["buys"] = t.get("buys", 0) + 1
                                            sol_amt = float(data.get("solAmount", 0) or 0)
                                            t["volume_sol"] = t.get("volume_sol", 0) + sol_amt
                                            if trader and isinstance(t.get("holders"), set):
                                                t["holders"].add(trader)
                                        else:
                                            t["sells"] = t.get("sells", 0) + 1

                                        # Hand off to Helius RPC monitor for precision tracking
                                        if real_sol >= 50 and not t.get("_rpc_added"):
                                            bc_addr = data.get("bondingCurveAddress", "")
                                            if bc_addr:
                                                t["_rpc_added"] = True
                                                symbol = t.get("symbol", "?")
                                                asyncio.create_task(
                                                    monitor.add_token(mint, bc_addr, symbol, real_sol))
                                                _log.info(f"PP→RPC handoff: ${symbol} real_sol={real_sol:.1f} bc={bc_addr[:16]}...")

                                    # === INSTANT HARD STOP — rug protection only ===
                                    # No curve sell — we hold through graduation
                                    if mint in _open_positions:
                                        cached = _open_positions[mint]
                                        entry_p = cached["entry_price"]
                                        v_tokens_now = float(data.get("vTokensInBondingCurve", 0) or 0)
                                        price_now = _calc_price_from_curve(v_sol, v_tokens_now) if v_tokens_now > 0 else 0
                                        pnl = ((price_now - entry_p) / entry_p * 100) if entry_p > 0 and price_now > 0 else 0
                                        hard_stop = cfg_live.get("hard_stop_pct", 25)

                                        if pnl <= -hard_stop:
                                            _log.info(f"🛑 INSTANT STOP: ${cached['symbol']} pnl={pnl:+.1f}%")
                                            async with async_session() as db:
                                                pos = await db.get(MigrationPosition, cached["pos_id"])
                                                if pos and pos.status == "OPEN":
                                                    await _close_position(pos, float(pos.remaining_pct),
                                                        f"Hard stop {pnl:.1f}%", pnl, cfg_live)
                                            del _open_positions[mint]

                                    # If this trade pushed real_sol into the entry zone, evaluate
                                    if real_sol >= min_sol * 0.90 and mint not in _seen_mints:
                                        symbol = data.get("symbol", "") or _tracked_tokens.get(mint, {}).get("symbol", "?")
                                        pct = (real_sol / grad_sol) * 100
                                        _log.info(f"APPROACHING: ${symbol} real_sol={real_sol:.1f} ({pct:.0f}%)")

                                        # Ensure token is tracked for future trades
                                        if mint not in _tracked_tokens:
                                            _tracked_tokens[mint] = {
                                                "symbol": symbol, "v_sol": v_sol,
                                                "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                                "real_sol": real_sol, "created_at": time.time(),
                                                "holders": set(), "buys": 1, "sells": 0,
                                                "volume_sol": real_sol, "last_trade_time": time.time(),
                                            }
                                            await ws.send(_json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))

                                        cfg = await get_config()
                                        t_data = _tracked_tokens.get(mint, {})
                                        token_data = {
                                            "mint": mint, "symbol": symbol,
                                            "v_sol": v_sol, "v_tokens": t_data.get("v_tokens", 0),
                                            "real_sol": real_sol, "holders": -1,
                                            "volume_sol": t_data.get("volume_sol", real_sol),
                                            "buys": t_data.get("buys", 0),
                                            "sells": t_data.get("sells", 0),
                                            "dev_pct": 0,
                                            "created_at": t_data.get("created_at"),
                                        }
                                        result = await _evaluate_token(token_data, cfg)
                                        if result:
                                            _log.info(f"PUMPPORTAL ENTRY: ${symbol} real_sol={real_sol:.1f}")

                                    elif real_sol >= min_sol * 0.60 and mint not in _tracked_tokens:
                                        # Start tracking tokens approaching the zone
                                        symbol = data.get("symbol", "?")
                                        _tracked_tokens[mint] = {
                                            "symbol": symbol,
                                            "v_sol": v_sol,
                                            "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                            "real_sol": real_sol,
                                            "created_at": time.time(),
                                            "holders": set(),
                                            "buys": 0, "sells": 0,
                                            "volume_sol": real_sol,
                                            "last_trade_time": time.time(),
                                        }
                                        await ws.send(_json.dumps({
                                            "method": "subscribeTokenTrade", "keys": [mint]}))

                            except _json.JSONDecodeError:
                                pass
                            except Exception as e:
                                _log.debug(f"PumpPortal msg error: {e}")

                            # Subscribe to newly discovered tokens + periodic stats
                            if time.time() - _last_resub >= 5:
                                _last_resub = time.time()
                                new_mints = [m for m in _tracked_tokens if m not in _pp_subscribed]
                                if new_mints:
                                    await ws.send(_json.dumps({
                                        "method": "subscribeTokenTrade",
                                        "keys": new_mints,
                                    }))
                                    _pp_subscribed.update(new_mints)

                            # Stats every 60s
                            if not hasattr(pumpportal_feed, '_last_stats'):
                                pumpportal_feed._last_stats = time.time()
                            if time.time() - pumpportal_feed._last_stats >= 60:
                                pumpportal_feed._last_stats = time.time()
                                hot = sum(1 for t in _tracked_tokens.values() if t.get("real_sol", 0) >= 30)
                                warm = sum(1 for t in _tracked_tokens.values() if 10 <= t.get("real_sol", 0) < 30)
                                _log.info(
                                    f"PP FIREHOSE: tracking {len(_tracked_tokens)} tokens "
                                    f"(subscribed {len(_pp_subscribed)}) | "
                                    f"{hot} hot (30+ SOL), {warm} warm (10-30 SOL) | "
                                    f"{len(_open_positions)} open positions"
                                )

                            # Trim stale tokens — aggressive pruning since we subscribe to ALL creates
                            if len(_tracked_tokens) > 2000:
                                now = time.time()
                                to_remove = []
                                for m, tdata in _tracked_tokens.items():
                                    if m in _open_positions:
                                        continue
                                    if tdata.get("real_sol", 0) >= 30:
                                        continue
                                    age = now - tdata.get("created_at", 0)
                                    idle = now - tdata.get("last_trade_time", 0)
                                    if age > 600 or idle > 300:
                                        to_remove.append(m)
                                for m in to_remove[:1000]:
                                    _tracked_tokens.pop(m, None)
                                if to_remove:
                                    _log.info(f"Pruned {len(to_remove)} stale tokens (tracked: {len(_tracked_tokens)})")

                except Exception as e:
                    _log.error(f"PumpPortal WS error: {type(e).__name__}: {e}")
                    await asyncio.sleep(10)

        async def position_loop():
            while True:
                try:
                    cfg = await get_config()
                    await _manage_positions(cfg)
                except Exception as e:
                    _log.debug(f"Position loop error: {e}")
                await asyncio.sleep(10)

        # Program-level monitor: watches EVERY pump.fun transaction
        from .migration_program_monitor import run_program_monitor

        async def on_program_detect(mint: str, symbol: str, real_sol: float, bc_addr: str):
            """Called when program monitor detects a token near graduation.
            Handles two zones:
            - Tracking zone (50-72 SOL): add to _tracked_tokens for PP subscription
            - Entry zone (72-84 SOL): evaluate for immediate buy
            """
            # Always add to tracked tokens so PumpPortal feed subscribes
            if mint not in _tracked_tokens:
                _tracked_tokens[mint] = {
                    "symbol": symbol, "v_sol": real_sol + 30, "v_tokens": 0,
                    "real_sol": real_sol, "created_at": time.time(),
                    "holders": set(), "buys": 1, "sells": 0,
                    "volume_sol": real_sol, "last_trade_time": time.time(),
                }
            else:
                # Update real_sol if we already have this token
                _tracked_tokens[mint]["real_sol"] = real_sol
                _tracked_tokens[mint]["last_trade_time"] = time.time()

            # Only evaluate for entry if in the entry zone (72-84 SOL)
            cfg = await get_config()
            min_sol = cfg.get("min_real_sol", 72)
            max_sol = cfg.get("max_real_sol", 84)

            if real_sol >= min_sol and real_sol <= max_sol and mint not in _seen_mints:
                t_data = _tracked_tokens.get(mint, {})
                v_sol_est = real_sol + 30
                v_tokens_est = t_data.get("v_tokens", 0)
                token_data = {
                    "mint": mint, "symbol": symbol,
                    "v_sol": t_data.get("v_sol", v_sol_est),
                    "v_tokens": v_tokens_est,
                    "real_sol": real_sol, "holders": -1,
                    "volume_sol": t_data.get("volume_sol", real_sol),
                    "buys": t_data.get("buys", 1), "sells": t_data.get("sells", 0),
                    "dev_pct": 0, "created_at": t_data.get("created_at", time.time()),
                }
                result = await _evaluate_token(token_data, cfg)
                if result:
                    _log.info(f"PROGRAM ENTRY: ${symbol} real_sol={real_sol:.1f}")

            # Publish graduation events for dip buyer
            if real_sol >= 84:
                try:
                    from ..core.redis import redis_client
                    await redis_client.publish("graduation_events",
                        _json.dumps({"mint": mint, "symbol": symbol}))
                except Exception:
                    pass

        await asyncio.gather(
            pumpportal_feed(),
            monitor.run(),
            discover_and_monitor(monitor, config, _pp_subscribe_queue, _tracked_tokens),
            run_program_monitor(on_program_detect, config),
            position_loop(),
        )
    except Exception as e:
        _log.error(f"Migration sniper FATAL ERROR: {e}", exc_info=True)
        raise
