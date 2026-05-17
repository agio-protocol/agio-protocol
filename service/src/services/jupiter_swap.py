# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Jupiter Swap Service — executes token swaps on Solana via Jupiter aggregator."""
import asyncio
import base64
import json
import logging
import os
from decimal import Decimal

import httpx

_log = logging.getLogger("jupiter-swap")

JUPITER_API = "https://api.jup.ag/swap/v1"
_helius_key = os.getenv("HELIUS_API_KEY", "")
SOLANA_RPC_READ = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_RPC_SEND = f"https://mainnet.helius-rpc.com/?api-key={_helius_key}" if _helius_key else SOLANA_RPC_READ
SOLANA_RPC = SOLANA_RPC_READ

# Common token mints
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _get_keypair():
    """Load the trading wallet keypair from env."""
    from solders.keypair import Keypair  # type: ignore

    pk = os.getenv("TRADING_WALLET_PRIVATE_KEY", "")
    if not pk:
        raise ValueError("TRADING_WALLET_PRIVATE_KEY not set")
    # Support both base58 and JSON array formats
    if pk.startswith("["):
        key_bytes = bytes(json.loads(pk))
        return Keypair.from_bytes(key_bytes)
    else:
        import base58 as b58

        return Keypair.from_bytes(b58.b58decode(pk))


def get_wallet_address() -> str:
    """Get the public key of the trading wallet."""
    kp = _get_keypair()
    return str(kp.pubkey())


async def get_balance() -> dict:
    """Get SOL and token balances for the trading wallet."""
    address = get_wallet_address()
    result: dict = {"sol": 0.0, "usdc": 0.0, "address": address}

    async with httpx.AsyncClient() as client:
        # SOL balance
        try:
            resp = await client.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [address],
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                lamports = data.get("result", {}).get("value", 0)
                result["sol"] = lamports / 1e9
        except Exception as exc:
            _log.warning("Failed to fetch SOL balance: %s", exc)

        # USDC balance (SPL token)
        try:
            resp = await client.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [address, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                accounts = data.get("result", {}).get("value", [])
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    if info.get("mint") == USDC_MINT:
                        result["usdc"] = float(info.get("tokenAmount", {}).get("uiAmount", 0))
        except Exception as exc:
            _log.warning("Failed to fetch USDC balance: %s", exc)

    return result


async def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 100,
) -> dict | None:
    """Get a swap quote from Jupiter.

    Args:
        input_mint: Token mint address to sell
        output_mint: Token mint address to buy
        amount_lamports: Amount in smallest unit (lamports for SOL, base units for tokens)
        slippage_bps: Slippage tolerance in basis points (100 = 1%)
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{JUPITER_API}/quote", params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_lamports),
                "slippageBps": slippage_bps,
                "onlyDirectRoutes": "false",
            }, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            _log.warning("Jupiter quote failed: %d %s", resp.status_code, resp.text[:200])
    except Exception as e:
        _log.error("Jupiter quote error: %s", e)
    return None


async def execute_swap(quote: dict, priority_fee_lamports: int = 50000) -> dict:
    """Execute a swap using a Jupiter quote.

    Returns dict with: success (bool), tx_hash (str | None), error (str | None)
    """
    from solders.keypair import Keypair  # type: ignore  # noqa: F811
    from solders.transaction import VersionedTransaction  # type: ignore

    keypair = _get_keypair()

    try:
        async with httpx.AsyncClient() as client:
            # Get swap transaction from Jupiter
            swap_resp = await client.post(f"{JUPITER_API}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": priority_fee_lamports,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)

            if swap_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": f"Swap request failed: {swap_resp.text[:200]}"}

            swap_data = swap_resp.json()
            swap_tx = swap_data.get("swapTransaction")
            if not swap_tx:
                return {"success": False, "tx_hash": None, "error": "No swap transaction returned"}

            # Decode and sign the transaction
            tx_bytes = base64.b64decode(swap_tx)
            tx = VersionedTransaction.from_bytes(tx_bytes)

            # Sign with our keypair
            signed_tx = VersionedTransaction(tx.message, [keypair])
            signed_bytes = bytes(signed_tx)

            send_resp = await client.post(SOLANA_RPC_SEND, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(signed_bytes).decode("utf-8"),
                    {"encoding": "base64", "skipPreflight": True, "maxRetries": 5},
                ],
            }, timeout=30)

            if send_resp.status_code == 200:
                result = send_resp.json()
                if "result" in result:
                    tx_hash = result["result"]
                    _log.info("Swap submitted: %s", tx_hash)

                    # Wait for confirmation
                    confirmed = await _wait_for_confirmation(tx_hash)
                    return {
                        "success": confirmed,
                        "tx_hash": tx_hash,
                        "error": None if confirmed else "Transaction not confirmed",
                    }
                else:
                    error = result.get("error", {})
                    return {"success": False, "tx_hash": None, "error": f"RPC error: {error}"}

            return {"success": False, "tx_hash": None, "error": f"Send failed: {send_resp.status_code}"}

    except Exception as e:
        _log.error("Swap execution error: %s", e)
        return {"success": False, "tx_hash": None, "error": str(e)}


async def _wait_for_confirmation(tx_hash: str, timeout_secs: int = 60) -> bool:
    """Wait for a transaction to be confirmed on Solana."""
    async with httpx.AsyncClient() as client:
        for _ in range(timeout_secs // 2):
            try:
                resp = await client.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[tx_hash], {"searchTransactionHistory": True}],
                }, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    statuses = data.get("result", {}).get("value", [])
                    if statuses and statuses[0]:
                        status = statuses[0]
                        if status.get("err"):
                            _log.error("Transaction failed: %s", status["err"])
                            return False
                        conf = status.get("confirmationStatus")
                        if conf in ("confirmed", "finalized"):
                            return True
            except Exception as exc:
                _log.debug("Confirmation poll error: %s", exc)
            await asyncio.sleep(2)
    return False


async def buy_token(
    token_mint: str,
    amount_sol: float,
    slippage_bps: int = 200,
    priority_fee: int = 50000,
) -> dict:
    """Buy a token with SOL.

    Args:
        token_mint: The mint address of the token to buy
        amount_sol: Amount of SOL to spend
        slippage_bps: Slippage tolerance (200 = 2%)
        priority_fee: Priority fee in micro-lamports

    Returns: {success, tx_hash, quote, error}
    """
    amount_lamports = int(amount_sol * 1e9)

    quote = await get_quote(SOL_MINT, token_mint, amount_lamports, slippage_bps)
    if not quote:
        return {"success": False, "tx_hash": None, "error": "Failed to get quote"}

    out_amount = int(quote.get("outAmount", 0))
    in_amount = int(quote.get("inAmount", 0))

    result = await execute_swap(quote, priority_fee)
    result["quote"] = {
        "in_amount": in_amount,
        "out_amount": out_amount,
        "price_impact": quote.get("priceImpactPct"),
        "route_plan": [r.get("swapInfo", {}).get("label", "?") for r in quote.get("routePlan", [])],
    }

    return result


async def sell_token(
    token_mint: str,
    amount_tokens: int,
    token_decimals: int = 6,
    slippage_bps: int = 300,
    priority_fee: int = 50000,
) -> dict:
    """Sell a token for SOL.

    Args:
        token_mint: The mint address of the token to sell
        amount_tokens: Amount of tokens in base units (raw, not UI amount)
        token_decimals: Decimals of the token
        slippage_bps: Slippage tolerance (300 = 3% -- higher for sells on memes)
        priority_fee: Priority fee in micro-lamports

    Returns: {success, tx_hash, quote, error}
    """
    quote = await get_quote(token_mint, SOL_MINT, amount_tokens, slippage_bps)
    if not quote:
        return {"success": False, "tx_hash": None, "error": "Failed to get quote"}

    out_amount = int(quote.get("outAmount", 0))

    result = await execute_swap(quote, priority_fee)
    result["quote"] = {
        "in_amount": amount_tokens,
        "out_amount": out_amount,
        "sol_received": out_amount / 1e9,
        "price_impact": quote.get("priceImpactPct"),
    }

    return result


async def get_token_balance(token_mint: str) -> tuple[int, float, int]:
    """Get balance of a specific token (checks both SPL Token and Token-2022).

    Returns: (raw_amount, ui_amount, decimals)
    """
    address = get_wallet_address()
    TOKEN_PROGRAMS = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    ]

    rpc = SOLANA_RPC_SEND if SOLANA_RPC_SEND != SOLANA_RPC_READ else SOLANA_RPC_READ

    async with httpx.AsyncClient() as client:
        # Query by mint only (not programId) — works on both Helius and public RPC
        try:
            resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [address, {"mint": token_mint}, {"encoding": "jsonParsed"}],
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    for acc in data.get("result", {}).get("value", []):
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        if info.get("mint") == token_mint:
                            ta = info.get("tokenAmount", {})
                            raw = int(ta.get("amount", 0))
                            if raw > 0:
                                return (raw, float(ta.get("uiAmount", 0)), int(ta.get("decimals", 6)))
        except Exception as exc:
            _log.debug("Mint balance check failed for %s: %s", token_mint[:16], exc)

        # Fallback: scan ALL accounts per program (handles edge cases)
        for program_id in TOKEN_PROGRAMS:
            try:
                resp = await client.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [address, {"programId": program_id}, {"encoding": "jsonParsed"}],
                }, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" not in data:
                        for acc in data.get("result", {}).get("value", []):
                            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                            if info.get("mint") == token_mint:
                                ta = info.get("tokenAmount", {})
                                raw = int(ta.get("amount", 0))
                                if raw > 0:
                                    return (raw, float(ta.get("uiAmount", 0)), int(ta.get("decimals", 6)))
            except Exception as exc:
                _log.debug("Program scan failed for %s: %s", token_mint[:16], exc)

    return (0, 0.0, 6)
