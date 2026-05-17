# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Program-level monitor for pump.fun — aggressive pump.fun API polling for
near-graduation token detection.

Previously used Helius getTransaction to parse every buy — that's gone (credits
exhausted by whale bot). Now polls pump.fun's frontend API every 5 seconds across
multiple endpoints and pages to find tokens approaching the 85 SOL graduation
threshold. When a token enters the entry zone (72-84 SOL), fires the callback
so the main sniper can evaluate and buy.

Zero Helius usage. Zero getTransaction. Pure HTTP polling of pump.fun API.
"""
import asyncio
import json as _json
import logging
import time

import httpx

_log = logging.getLogger("migration-program")

# API endpoints — pump.fun frontend API returns real_sol_reserves directly
PUMP_API_BASE = "https://frontend-api-v3.pump.fun"
CURRENTLY_LIVE_URL = f"{PUMP_API_BASE}/coins/currently-live"
KING_OF_HILL_URL = f"{PUMP_API_BASE}/coins/king-of-the-hill"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Poll interval — 5 seconds for aggressive detection
POLL_INTERVAL = 10

# Track which mints we've already fired the callback for (prevent duplicate entries)
_fired_mints: set[str] = set()
# Track mints we've seen to avoid re-logging
_seen_mints_log: set[str] = set()
# Stats
_scan_count = 0
_tokens_found = 0


async def _fetch_page(client: httpx.AsyncClient, url: str, params: dict) -> list:
    """Fetch a single page from pump.fun API. Returns list of tokens or empty."""
    try:
        resp = await client.get(url, params=params,
                                headers={"User-Agent": USER_AGENT}, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except Exception as e:
        _log.debug(f"API fetch error {url} offset={params.get('offset', '?')}: {e}")
    return []


async def _scan_all_endpoints(client: httpx.AsyncClient) -> list:
    """Fetch tokens from pump.fun API endpoints SEQUENTIALLY to avoid rate limits.
    Scans 8 pages (400 tokens) of currently-live for maximum coverage.
    King-of-the-hill is Cloudflare blocked (returns empty) so we skip it."""
    seen = set()
    all_tokens = []

    for offset in [0, 50, 100, 150]:
        page = await _fetch_page(client, CURRENTLY_LIVE_URL,
                                 {"limit": 50, "offset": offset, "includeNsfw": "false"})
        for tok in page:
            mint = tok.get("mint", "")
            if mint and mint not in seen:
                seen.add(mint)
                all_tokens.append(tok)
        await asyncio.sleep(0.5)

    return all_tokens


async def run_program_monitor(on_approaching: callable, config: dict):
    """Aggressive pump.fun API poller — replaces the old logsSubscribe + getTransaction
    approach that required Helius credits.

    Every 5 seconds:
    1. Fetches 300+ tokens from pump.fun API (currently-live + king-of-the-hill)
    2. Checks real_sol_reserves on each token
    3. Any token at 50+ SOL real_sol -> fires callback for tracking + evaluation
    4. Any token at 72-84 SOL -> fires callback for immediate entry evaluation

    This is the PRIMARY detection layer now that Helius is unavailable.
    """
    global _scan_count, _tokens_found

    _log.info("Program monitor starting — pump.fun API poller mode (no Helius, no getTransaction)")
    _log.info(f"Poll interval: {POLL_INTERVAL}s | Endpoints: currently-live (6 pages) + king-of-the-hill")

    # Reuse a single httpx client for connection pooling
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        timeout=httpx.Timeout(10.0),
    ) as client:
        while True:
            try:
                scan_start = time.time()
                tokens = await _scan_all_endpoints(client)
                scan_duration = time.time() - scan_start
                _scan_count += 1

                if not tokens:
                    if _scan_count % 12 == 0:  # Log every ~60s if empty
                        _log.warning(f"Program monitor: 0 tokens from API (scan #{_scan_count})")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                grad_sol = config.get("graduation_real_sol", 85)
                min_sol = config.get("min_real_sol", 72)
                max_sol = config.get("max_real_sol", 84)
                # Track from 50 SOL — gives PumpPortal time to subscribe before entry zone
                track_threshold = 50.0

                near_grad_count = 0
                entry_zone_count = 0
                fired_this_scan = 0

                for tok in tokens:
                    mint = tok.get("mint", "")
                    if not mint or tok.get("complete"):
                        continue

                    real_sol_raw = float(tok.get("real_sol_reserves", 0) or 0)
                    real_sol = real_sol_raw / 1e9
                    symbol = tok.get("symbol", "?")

                    if real_sol < track_threshold or real_sol >= grad_sol:
                        continue

                    near_grad_count += 1

                    # Already fired for this mint — skip
                    if mint in _fired_mints:
                        continue

                    bc_addr = tok.get("bonding_curve", "")

                    # Token is in or approaching the entry zone
                    if real_sol >= min_sol and real_sol <= max_sol:
                        # ENTRY ZONE — fire callback for immediate evaluation
                        entry_zone_count += 1
                        _fired_mints.add(mint)
                        fired_this_scan += 1
                        _tokens_found += 1
                        _log.info(
                            f"PROGRAM DETECT (entry zone): ${symbol} "
                            f"real_sol={real_sol:.1f}/{grad_sol} "
                            f"({real_sol/grad_sol*100:.0f}%) mint={mint[:16]}..."
                        )
                        try:
                            await on_approaching(mint, symbol, real_sol, bc_addr)
                        except Exception as e:
                            _log.error(f"Callback error for ${symbol}: {e}")
                            _fired_mints.discard(mint)

                    elif real_sol >= track_threshold:
                        # TRACKING ZONE (50-72 SOL) — fire callback so token gets
                        # added to _tracked_tokens and PumpPortal subscribes to it.
                        # When trades push it past 72 SOL, PumpPortal will catch it
                        # in real-time.
                        _fired_mints.add(mint)
                        fired_this_scan += 1
                        _tokens_found += 1
                        if mint not in _seen_mints_log:
                            _seen_mints_log.add(mint)
                            _log.info(
                                f"PROGRAM TRACK: ${symbol} "
                                f"real_sol={real_sol:.1f}/{grad_sol} "
                                f"({real_sol/grad_sol*100:.0f}%) — queued for PP subscribe"
                            )
                        try:
                            await on_approaching(mint, symbol, real_sol, bc_addr)
                        except Exception as e:
                            _log.debug(f"Track callback error for ${symbol}: {e}")
                            _fired_mints.discard(mint)

                # Periodic stats log
                if _scan_count % 12 == 0:  # Every ~60 seconds
                    _log.info(
                        f"Program monitor stats: scan #{_scan_count} "
                        f"({scan_duration:.1f}s) | {len(tokens)} tokens fetched | "
                        f"{near_grad_count} near-grad (50+ SOL) | "
                        f"{entry_zone_count} in entry zone | "
                        f"{fired_this_scan} new callbacks | "
                        f"total detected: {_tokens_found} | "
                        f"fired set: {len(_fired_mints)}"
                    )

                # Prune fired set — tokens that graduated or went stale
                # Re-check mints after 5 minutes in case they dropped back
                if len(_fired_mints) > 500:
                    _fired_mints.clear()
                    _seen_mints_log.clear()
                    _log.info("Cleared fired_mints cache (>500 entries)")

            except Exception as e:
                _log.error(f"Program monitor scan error: {type(e).__name__}: {e}")

            await asyncio.sleep(POLL_INTERVAL)
