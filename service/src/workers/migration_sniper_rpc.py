# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
On-chain RPC monitor for migration sniper — sub-second bonding curve tracking.

Subscribes directly to pump.fun bonding curve accounts via Solana RPC WebSocket.
Decodes realSolReserves from raw 151-byte account data every block (~400ms).
Fires buy when realSolReserves approaches graduation threshold (85 SOL).

This replaces the 15-second pump.fun API polling with block-level precision.
"""
import asyncio
import base64
import json as _json
import logging
import struct
import time
import os

import websockets
import httpx

_log = logging.getLogger("migration-rpc")

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GRADUATION_LAMPORTS = 85_000_000_000  # 85 SOL in lamports
ACCOUNT_DATA_SIZE = 151

# Byte offsets in the 151-byte bonding curve account
OFFSET_VIRTUAL_TOKEN_RES = 8
OFFSET_VIRTUAL_SOL_RES = 16
OFFSET_REAL_TOKEN_RES = 24
OFFSET_REAL_SOL_RES = 32
OFFSET_TOKEN_TOTAL_SUPPLY = 40
OFFSET_COMPLETE = 48


def decode_bonding_curve(data: bytes) -> dict | None:
    """Decode a pump.fun bonding curve account from raw bytes."""
    if len(data) < 49:
        return None
    return {
        "virtual_token_reserves": struct.unpack('<Q', data[OFFSET_VIRTUAL_TOKEN_RES:OFFSET_VIRTUAL_TOKEN_RES+8])[0],
        "virtual_sol_reserves": struct.unpack('<Q', data[OFFSET_VIRTUAL_SOL_RES:OFFSET_VIRTUAL_SOL_RES+8])[0],
        "real_token_reserves": struct.unpack('<Q', data[OFFSET_REAL_TOKEN_RES:OFFSET_REAL_TOKEN_RES+8])[0],
        "real_sol_reserves": struct.unpack('<Q', data[OFFSET_REAL_SOL_RES:OFFSET_REAL_SOL_RES+8])[0],
        "token_total_supply": struct.unpack('<Q', data[OFFSET_TOKEN_TOTAL_SUPPLY:OFFSET_TOKEN_TOTAL_SUPPLY+8])[0],
        "complete": bool(data[OFFSET_COMPLETE]),
    }


class BondingCurveMonitor:
    """Monitors bonding curve accounts via Solana RPC WebSocket for real-time
    realSolReserves tracking. Fires callback when graduation threshold is approached."""

    def __init__(self, rpc_ws_url: str, on_migration_ready: callable, config: dict):
        self.rpc_ws = rpc_ws_url
        self.on_migration_ready = on_migration_ready
        self.config = config
        # mint -> {bonding_curve_addr, symbol, subscription_id, last_real_sol, ...}
        self._tracked: dict[str, dict] = {}
        self._sub_id_to_mint: dict[int, str] = {}
        self._ws = None
        self._next_id = 1
        self._fired_mints: set[str] = set()

    def _get_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def add_token(self, mint: str, bonding_curve_addr: str, symbol: str,
                        current_real_sol: float = 0):
        """Subscribe to a bonding curve account for real-time monitoring."""
        if mint in self._tracked or mint in self._fired_mints:
            return

        self._tracked[mint] = {
            "bc_addr": bonding_curve_addr,
            "symbol": symbol,
            "real_sol": current_real_sol,
            "real_sol_lamports": int(current_real_sol * 1e9),
            "complete": False,
            "sub_id": None,
            "added_at": time.time(),
        }

        if self._ws:
            await self._subscribe_account(mint, bonding_curve_addr)

        _log.info(f"RPC tracking: ${symbol} bc={bonding_curve_addr[:16]}... "
                  f"real_sol={current_real_sol:.1f}")

    async def _subscribe_account(self, mint: str, bc_addr: str):
        """Send accountSubscribe for a bonding curve account."""
        req_id = self._get_id()
        await self._ws.send(_json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "accountSubscribe",
            "params": [
                bc_addr,
                {"encoding": "base64", "commitment": "confirmed"}
            ]
        }))
        self._tracked[mint]["_pending_req_id"] = req_id

    async def remove_token(self, mint: str):
        """Unsubscribe from a token's bonding curve."""
        info = self._tracked.pop(mint, None)
        if info and info.get("sub_id") and self._ws:
            try:
                await self._ws.send(_json.dumps({
                    "jsonrpc": "2.0",
                    "id": self._get_id(),
                    "method": "accountUnsubscribe",
                    "params": [info["sub_id"]]
                }))
            except Exception:
                pass
            self._sub_id_to_mint.pop(info["sub_id"], None)

    async def run(self):
        """Main WebSocket loop — connects, subscribes, processes account updates."""
        while True:
            try:
                ws_url = self.rpc_ws
                _log.info(f"RPC WebSocket connecting: {ws_url[:50]}...")
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    open_timeout=15,
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    self._ws = ws
                    _log.info(f"RPC WebSocket connected — subscribing to {len(self._tracked)} accounts")

                    # Re-subscribe all tracked tokens
                    for mint, info in self._tracked.items():
                        await self._subscribe_account(mint, info["bc_addr"])

                    async for message in ws:
                        try:
                            data = _json.loads(message)

                            # Handle subscription confirmations
                            if "id" in data and "result" in data and isinstance(data["result"], int):
                                sub_id = data["result"]
                                req_id = data["id"]
                                matched = False
                                for mint, info in self._tracked.items():
                                    if info.get("_pending_req_id") == req_id:
                                        info["sub_id"] = sub_id
                                        self._sub_id_to_mint[sub_id] = mint
                                        del info["_pending_req_id"]
                                        matched = True
                                        _log.debug(f"RPC sub confirmed: {info.get('symbol','?')} sub={sub_id}")
                                        break
                                if not matched:
                                    _log.warning(f"RPC: unmatched sub confirm req={req_id} sub={sub_id}")

                            # Handle account notifications
                            elif data.get("method") == "accountNotification":
                                params = data.get("params", {})
                                sub_id = params.get("subscription")
                                mint = self._sub_id_to_mint.get(sub_id)
                                if not mint or mint not in self._tracked:
                                    continue

                                account_data = params.get("result", {}).get("value", {}).get("data", [])
                                if not account_data or not isinstance(account_data, list):
                                    continue

                                raw = base64.b64decode(account_data[0])
                                decoded = decode_bonding_curve(raw)
                                if not decoded:
                                    continue

                                info = self._tracked[mint]
                                old_sol = info["real_sol_lamports"]
                                new_sol = decoded["real_sol_reserves"]
                                info["real_sol_lamports"] = new_sol
                                info["real_sol"] = new_sol / 1e9
                                info["complete"] = decoded["complete"]
                                info["virtual_sol"] = decoded["virtual_sol_reserves"]
                                info["virtual_tokens"] = decoded["virtual_token_reserves"]

                                # Check graduation
                                if decoded["complete"] and mint not in self._fired_mints:
                                    _log.info(f"🎓 ${info['symbol']} GRADUATED on-chain!")
                                    self._fired_mints.add(mint)
                                    async def _safe_callback(m, i, t):
                                        try:
                                            await self.on_migration_ready(m, i, t)
                                        except Exception as e:
                                            _log.error(f"Callback failed for {i.get('symbol','?')}: {e}")
                                            self._fired_mints.discard(m)
                                    asyncio.create_task(_safe_callback(mint, info, "graduated"))
                                    continue

                                # Check if approaching graduation threshold
                                sol_remaining = GRADUATION_LAMPORTS - new_sol
                                min_sol = int(self.config.get("min_real_sol", 72) * 1e9)
                                max_sol = int(self.config.get("max_real_sol", 84) * 1e9)

                                if (new_sol >= min_sol and new_sol <= max_sol
                                        and mint not in self._fired_mints):
                                    sol_remaining_f = sol_remaining / 1e9
                                    _log.info(
                                        f"🎯 ${info['symbol']} HIT ENTRY: "
                                        f"real_sol={new_sol/1e9:.2f} "
                                        f"remaining={sol_remaining_f:.2f} SOL")
                                    self._fired_mints.add(mint)
                                    async def _safe_entry(m, i, t):
                                        try:
                                            await self.on_migration_ready(m, i, t)
                                        except Exception as e:
                                            _log.error(f"Entry callback failed for {i.get('symbol','?')}: {e}")
                                            self._fired_mints.discard(m)
                                    asyncio.create_task(_safe_entry(mint, info, "threshold"))

                        except _json.JSONDecodeError:
                            pass
                        except Exception as e:
                            _log.debug(f"RPC message error: {e}")

                    # Clean up stale tokens (>30 min old, never graduated)
                    now = time.time()
                    stale = [m for m, i in self._tracked.items()
                             if now - i.get("added_at", 0) > 1800]
                    for m in stale:
                        await self.remove_token(m)

            except Exception as e:
                _log.error(f"RPC WebSocket error: {type(e).__name__}: {e}")
                self._ws = None
                await asyncio.sleep(5)


async def discover_and_monitor(monitor: BondingCurveMonitor, config: dict,
                               pp_subscribe_queue: asyncio.Queue = None,
                               shared_tracked: dict = None):
    """Discovery loop: polls pump.fun API to find tokens approaching graduation,
    then hands them to the RPC monitor for block-level tracking."""
    seen_mints: set[str] = set()

    while True:
        try:
            await asyncio.sleep(3)
            async with httpx.AsyncClient() as client:
                tokens = []
                for offset in [0, 50, 100, 150]:
                    try:
                        resp = await client.get(
                            "https://frontend-api-v3.pump.fun/coins/currently-live",
                            params={"limit": 50, "offset": offset, "includeNsfw": "false"},
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            page = resp.json()
                            if isinstance(page, list):
                                tokens.extend(page)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                if not tokens:
                    await asyncio.sleep(5)
                    continue

                grad_sol = config.get("graduation_real_sol", 85)
                min_real_sol = config.get("min_real_sol", 72)
                # Track tokens at 50+ SOL real_sol — close enough to graduation
                # that PumpPortal subscription is worth the slot.
                # Previously 50% of min_real_sol (36 SOL) which was still too
                # conservative. The program monitor now handles the wider net.
                track_threshold = 50.0

                for tok in tokens:
                    mint = tok.get("mint", "")
                    if not mint or mint in seen_mints or tok.get("complete"):
                        continue

                    real_sol = float(tok.get("real_sol_reserves", 0) or 0) / 1e9
                    bc_addr = tok.get("bonding_curve", "")

                    if real_sol >= track_threshold and real_sol < grad_sol and bc_addr:
                        seen_mints.add(mint)
                        symbol = tok.get("symbol", "?")
                        await monitor.add_token(mint, bc_addr, symbol, real_sol)
                        # Also populate the shared _tracked_tokens dict for PumpPortal feed
                        if shared_tracked is not None and mint not in shared_tracked:
                            v_sol_lamports = float(tok.get("virtual_sol_reserves", 0) or 0)
                            v_tokens_raw = float(tok.get("virtual_token_reserves", 0) or 0)
                            shared_tracked[mint] = {
                                "symbol": symbol,
                                "name": tok.get("name", ""),
                                "dev": tok.get("creator", ""),
                                "v_sol": v_sol_lamports / 1e9,
                                "v_tokens": v_tokens_raw / 1e6,
                                "real_sol": real_sol,
                                "created_at": time.time(),
                                "holders": set(),
                                "buys": 0, "sells": 0,
                                "volume_sol": real_sol,
                                "last_trade_time": time.time(),
                            }
                        if pp_subscribe_queue:
                            try:
                                pp_subscribe_queue.put_nowait(mint)
                            except asyncio.QueueFull:
                                pass

        except Exception as e:
            _log.debug(f"Discovery error: {e}")

        await asyncio.sleep(5)
