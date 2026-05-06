# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Kraken Exchange Service — executes crypto trades on Kraken."""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse

import httpx

_log = logging.getLogger("kraken-exchange")

KRAKEN_API = "https://api.kraken.com"


def _get_credentials():
    """Load Kraken API key and secret from env."""
    key = os.getenv("KRAKEN_API_KEY", "")
    secret = os.getenv("KRAKEN_API_SECRET", "")
    if not key or not secret:
        raise ValueError("KRAKEN_API_KEY and KRAKEN_API_SECRET not set")
    return key, secret


def _sign_request(urlpath: str, data: dict, secret: str) -> str:
    """Generate Kraken API signature (HMAC-SHA512)."""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


async def _private_request(endpoint: str, data: dict = None) -> dict:
    """Make authenticated Kraken API request."""
    key, secret = _get_credentials()
    global _nonce_counter
    if data is None:
        data = {}
    _nonce_counter += 1
    data["nonce"] = str(int(time.time() * 1000) * 100 + (_nonce_counter % 100))

    urlpath = f"/0/private/{endpoint}"
    signature = _sign_request(urlpath, data, secret)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KRAKEN_API}{urlpath}",
            data=data,
            headers={"API-Key": key, "API-Sign": signature},
            timeout=15,
        )
        result = resp.json()
        if result.get("error") and len(result["error"]) > 0:
            _log.error("Kraken API error: %s", result["error"])
        return result


async def _public_request(endpoint: str, params: dict = None) -> dict:
    """Make public Kraken API request."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{KRAKEN_API}/0/public/{endpoint}",
            params=params or {},
            timeout=10,
        )
        return resp.json()


# Symbol mapping: our symbols to Kraken pairs
KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD", "AVAX": "AVAXUSD",
    "LINK": "LINKUSD", "DOGE": "XDGUSD", "ADA": "ADAUSD", "DOT": "DOTUSD",
    "MATIC": "MATICUSD", "NEAR": "NEARUSD", "ARB": "ARBUSD", "OP": "OPUSD",
    "SUI": "SUIUSD", "APT": "APTUSD", "INJ": "INJUSD", "TIA": "TIAUSD",
    "SEI": "SEIUSD", "JUP": "JUPUSD", "WIF": "WIFUSD", "PEPE": "PEPEUSD",
    "RENDER": "RENDERUSD", "FET": "FETUSD", "TAO": "TAOUSD", "ONDO": "ONDOUSD",
    "AAVE": "AAVEUSD", "TON": "TONUSD", "BONK": "BONKUSD", "CORN": "CORNUSD",
    "XRP": "XXRPZUSD", "HBAR": "HBARUSD", "PENDLE": "PENDLEUSD", "TRX": "TRXUSD",
    "UNI": "UNIUSD", "LDO": "LDOUSD", "KAVA": "KAVAUSD", "CRO": "CROUSD",
    "ZEC": "XZECZUSD", "DASH": "DASHUSD", "ETC": "XETCZUSD",
}


async def get_balance() -> dict:
    """Get account balances."""
    result = await _private_request("Balance")
    balances = result.get("result", {})
    return {
        "usd": float(balances.get("ZUSD", 0)),
        "balances": {k: float(v) for k, v in balances.items() if float(v) > 0},
    }


async def get_ticker(symbol: str) -> dict | None:
    """Get current ticker data for a symbol."""
    pair = KRAKEN_PAIRS.get(symbol.upper())
    if not pair:
        return None
    result = await _public_request("Ticker", {"pair": pair})
    data = result.get("result", {})
    if data:
        ticker = list(data.values())[0]
        return {
            "ask": float(ticker["a"][0]),
            "bid": float(ticker["b"][0]),
            "last": float(ticker["c"][0]),
            "volume_24h": float(ticker["v"][1]),
            "high_24h": float(ticker["h"][1]),
            "low_24h": float(ticker["l"][1]),
        }
    return None


async def get_price(symbol: str) -> float:
    """Get current price for a symbol."""
    ticker = await get_ticker(symbol)
    return ticker["last"] if ticker else 0


MAX_ORDER_USD = float(os.getenv("KRAKEN_MAX_ORDER_USD", "100"))

_nonce_counter = 0


async def place_order(
    symbol: str,
    side: str,
    amount_usd: float,
    order_type: str = "market",
) -> dict:
    """Place a market order on Kraken.

    Args:
        symbol: e.g. "SOL", "BTC", "ETH"
        side: "buy" or "sell"
        amount_usd: USD value to buy/sell
        order_type: "market" (default) or "limit"

    Returns: {success, order_id, price, volume, error}
    """
    pair = KRAKEN_PAIRS.get(symbol.upper())
    if not pair:
        return {"success": False, "order_id": None, "error": f"Unknown symbol: {symbol}"}

    # Hard cap on order size — prevents account drain
    if amount_usd > MAX_ORDER_USD:
        return {"success": False, "order_id": None, "error": f"Order ${amount_usd:.2f} exceeds max ${MAX_ORDER_USD:.2f}"}
    if amount_usd <= 0:
        return {"success": False, "order_id": None, "error": "Invalid order amount"}

    try:
        # Get current price to calculate volume
        price = await get_price(symbol)
        if price <= 0:
            return {"success": False, "order_id": None, "error": "Could not get price"}

        volume = amount_usd / price

        # Kraken requires minimum order sizes
        MIN_VOLUMES = {
            "BTC": 0.0001, "ETH": 0.005, "SOL": 0.1, "AVAX": 0.1,
            "LINK": 0.5, "DOGE": 50, "ADA": 10, "DOT": 0.5,
        }
        min_vol = MIN_VOLUMES.get(symbol.upper(), 0.1)
        if volume < min_vol:
            return {"success": False, "order_id": None, "error": f"Volume {volume:.6f} below minimum {min_vol}"}

        data = {
            "pair": pair,
            "type": side.lower(),
            "ordertype": order_type,
            "volume": f"{volume:.8f}",
        }

        # For limit orders, set price
        if order_type == "limit":
            data["price"] = f"{price:.6f}"

        result = await _private_request("AddOrder", data)

        if result.get("error") and len(result["error"]) > 0:
            return {"success": False, "order_id": None, "error": str(result["error"])}

        order_result = result.get("result", {})
        order_ids = order_result.get("txid", [])

        _log.info("Kraken %s %s: %.6f @ $%.2f (order: %s)",
                   side, symbol, volume, price, order_ids)

        return {
            "success": True,
            "order_id": order_ids[0] if order_ids else None,
            "price": price,
            "volume": volume,
            "usd_value": amount_usd,
            "error": None,
        }

    except Exception as e:
        _log.error("Kraken order error: %s", e)
        return {"success": False, "order_id": None, "error": str(e)}


async def buy(symbol: str, amount_usd: float) -> dict:
    """Buy crypto with USD."""
    return await place_order(symbol, "buy", amount_usd)


async def sell(symbol: str, amount_usd: float) -> dict:
    """Sell crypto for USD."""
    return await place_order(symbol, "sell", amount_usd)


async def sell_all(symbol: str) -> dict:
    """Sell entire balance of a symbol."""
    try:
        balances = await get_balance()
        # Find the symbol's balance
        # Kraken uses different key formats (XXBT for BTC, XETH for ETH, SOL for SOL)
        KRAKEN_BALANCE_KEYS = {
            "BTC": ["XXBT", "XBT", "BTC"], "ETH": ["XETH", "ETH"],
            "SOL": ["SOL"], "AVAX": ["AVAX"], "LINK": ["LINK"],
            "DOGE": ["XXDG", "XDG", "DOGE"], "ADA": ["ADA"], "DOT": ["DOT"],
        }
        keys = KRAKEN_BALANCE_KEYS.get(symbol.upper(), [symbol.upper()])
        balance = 0
        for k in keys:
            if k in balances.get("balances", {}):
                balance = balances["balances"][k]
                break

        if balance <= 0:
            return {"success": False, "order_id": None, "error": f"No {symbol} balance to sell"}

        price = await get_price(symbol)
        pair = KRAKEN_PAIRS.get(symbol.upper())
        if not pair:
            return {"success": False, "order_id": None, "error": f"Unknown symbol: {symbol}"}

        result = await _private_request("AddOrder", {
            "pair": pair,
            "type": "sell",
            "ordertype": "market",
            "volume": f"{balance:.8f}",
        })

        if result.get("error") and len(result["error"]) > 0:
            return {"success": False, "order_id": None, "error": str(result["error"])}

        order_result = result.get("result", {})
        order_ids = order_result.get("txid", [])

        return {
            "success": True,
            "order_id": order_ids[0] if order_ids else None,
            "price": price,
            "volume": balance,
            "usd_value": balance * price,
            "error": None,
        }
    except Exception as e:
        return {"success": False, "order_id": None, "error": str(e)}


async def get_open_orders() -> list:
    """Get all open orders."""
    result = await _private_request("OpenOrders")
    orders = result.get("result", {}).get("open", {})
    return [{"id": k, **v} for k, v in orders.items()]


async def cancel_order(order_id: str) -> bool:
    """Cancel an order."""
    result = await _private_request("CancelOrder", {"txid": order_id})
    return not bool(result.get("error"))
