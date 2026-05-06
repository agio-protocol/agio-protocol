# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Tastytrade Exchange Service — executes stock trades via Tastytrade API."""
import asyncio
import logging
import os
import time

import httpx

_log = logging.getLogger("tastytrade-exchange")

TASTYTRADE_API = "https://api.tastyworks.com"
MAX_ORDER_USD = float(os.getenv("TASTYTRADE_MAX_ORDER_USD", "200"))

_session_token = None
_session_expires = 0
_cached_account = None


async def _login() -> str:
    """Login to Tastytrade and get session token."""
    global _session_token, _session_expires

    # Return cached token if still valid
    if _session_token and time.time() < _session_expires:
        return _session_token

    username = os.getenv("TASTYTRADE_USERNAME", "")
    password = os.getenv("TASTYTRADE_PASSWORD", "")
    if not username or not password:
        raise ValueError("TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD not set")

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TASTYTRADE_API}/sessions", json={
            "login": username,
            "password": password,
        }, timeout=15)

        if resp.status_code == 201:
            data = resp.json().get("data", {})
            _session_token = data.get("session-token")
            _session_expires = time.time() + 86000  # ~24 hours
            _log.info("Tastytrade session established")
            return _session_token
        else:
            raise ValueError(f"Tastytrade login failed: {resp.status_code}")


async def _auth_headers() -> dict:
    """Build authenticated request headers."""
    token = await _login()
    return {"Authorization": token, "Content-Type": "application/json"}


async def _get_account_number() -> str:
    """Get the first account number (cached after first call)."""
    global _cached_account
    if _cached_account:
        return _cached_account

    headers = await _auth_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TASTYTRADE_API}/customers/me/accounts",
            headers=headers, timeout=10,
        )
        if resp.status_code == 200:
            accounts = resp.json().get("data", {}).get("items", [])
            if accounts:
                _cached_account = accounts[0].get("account", {}).get("account-number", "")
                if _cached_account:
                    return _cached_account
    raise ValueError("No Tastytrade account found")


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

async def get_balance() -> dict:
    """Get account balances.

    Returns: {"usd": float, "balances": {}}
    """
    try:
        account = await _get_account_number()
        headers = await _auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TASTYTRADE_API}/accounts/{account}/balances",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                cash = float(data.get("cash-balance", 0))
                buying_power = float(data.get("equity-buying-power", 0))
                net_liq = float(data.get("net-liquidating-value", 0))
                return {
                    "usd": cash,
                    "balances": {
                        "cash": cash,
                        "buying_power": buying_power,
                        "net_liquidating_value": net_liq,
                    },
                }
            return {"usd": 0, "balances": {}}
    except Exception as e:
        _log.error("Tastytrade balance error: %s", e)
        return {"usd": 0, "balances": {}}


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

async def _get_price_yahoo(symbol: str) -> float:
    """Fallback: get price from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
        })
        if resp.status_code == 200:
            result = resp.json().get("chart", {}).get("result", [])
            if result:
                meta = result[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                if price:
                    return float(price)
    return 0


async def get_price(symbol: str) -> float:
    """Get current price for a stock symbol.

    Tries Tastytrade market-data first, falls back to Yahoo Finance.
    """
    # Try Tastytrade market data
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TASTYTRADE_API}/market-data/{symbol.upper()}/quotes",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                bid = float(data.get("bid", 0))
                ask = float(data.get("ask", 0))
                if bid and ask:
                    return round((bid + ask) / 2, 4)
                last = float(data.get("last", 0))
                if last:
                    return last
    except Exception as e:
        _log.debug("Tastytrade market data failed for %s: %s", symbol, e)

    # Fallback to Yahoo Finance
    try:
        price = await _get_price_yahoo(symbol)
        if price > 0:
            _log.debug("Using Yahoo Finance price for %s: %.4f", symbol, price)
            return price
    except Exception as e:
        _log.error("Yahoo Finance fallback failed for %s: %s", symbol, e)

    return 0


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def _place_order(symbol: str, action: str, quantity: float) -> dict:
    """Place an equity order on Tastytrade.

    Args:
        symbol: stock ticker e.g. "AAPL"
        action: "Buy to Open" or "Sell to Close"
        quantity: number of shares (can be fractional)

    Returns: {success, order_id, price, qty, error}
    """
    try:
        account = await _get_account_number()
        headers = await _auth_headers()

        order_payload = {
            "time-in-force": "Day",
            "order-type": "Market",
            "legs": [
                {
                    "instrument-type": "Equity",
                    "symbol": symbol.upper(),
                    "quantity": quantity,
                    "action": action,
                }
            ],
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TASTYTRADE_API}/accounts/{account}/orders",
                headers=headers, json=order_payload, timeout=15,
            )

            if resp.status_code in (200, 201):
                data = resp.json().get("data", {})
                order = data.get("order", data)
                order_id = order.get("id", None)
                _log.info("Tastytrade %s %s: qty=%.4f (order: %s)",
                          action, symbol, quantity, order_id)
                price = await get_price(symbol)
                return {
                    "success": True,
                    "order_id": str(order_id) if order_id else None,
                    "price": price,
                    "qty": quantity,
                    "usd_value": round(quantity * price, 2),
                    "error": None,
                }
            else:
                error_msg = resp.text
                try:
                    error_data = resp.json()
                    error_msg = error_data.get("error", {}).get("message", resp.text)
                except Exception:
                    pass
                _log.error("Tastytrade order failed: %s %s — %s", action, symbol, error_msg)
                return {
                    "success": False,
                    "order_id": None,
                    "price": None,
                    "qty": quantity,
                    "error": f"Order failed ({resp.status_code}): {error_msg}",
                }

    except Exception as e:
        _log.error("Tastytrade order error: %s", e)
        return {"success": False, "order_id": None, "price": None, "qty": 0, "error": str(e)}


async def buy(symbol: str, amount_usd: float) -> dict:
    """Buy stock with USD.

    Args:
        symbol: stock ticker e.g. "AAPL"
        amount_usd: dollar amount to spend

    Returns: {success, order_id, price, qty, error}
    """
    if amount_usd > MAX_ORDER_USD:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": f"Order ${amount_usd:.2f} exceeds max ${MAX_ORDER_USD:.2f}",
        }
    if amount_usd <= 0:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": "Invalid order amount",
        }

    price = await get_price(symbol)
    if price <= 0:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": f"Could not get price for {symbol}",
        }

    qty = round(amount_usd / price, 4)
    if qty <= 0:
        return {
            "success": False, "order_id": None, "price": price, "qty": 0,
            "error": "Calculated quantity is zero",
        }

    return await _place_order(symbol, "Buy to Open", qty)


async def sell(symbol: str, amount_usd: float) -> dict:
    """Sell stock for USD.

    Args:
        symbol: stock ticker e.g. "AAPL"
        amount_usd: dollar amount worth of shares to sell

    Returns: {success, order_id, price, qty, error}
    """
    if amount_usd > MAX_ORDER_USD:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": f"Order ${amount_usd:.2f} exceeds max ${MAX_ORDER_USD:.2f}",
        }
    if amount_usd <= 0:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": "Invalid order amount",
        }

    price = await get_price(symbol)
    if price <= 0:
        return {
            "success": False, "order_id": None, "price": None, "qty": 0,
            "error": f"Could not get price for {symbol}",
        }

    qty = round(amount_usd / price, 4)
    if qty <= 0:
        return {
            "success": False, "order_id": None, "price": price, "qty": 0,
            "error": "Calculated quantity is zero",
        }

    return await _place_order(symbol, "Sell to Close", qty)


async def sell_all(symbol: str) -> dict:
    """Close entire position for a symbol.

    Returns: {success, order_id, price, qty, error}
    """
    try:
        positions = await get_positions()
        position_qty = 0
        for pos in positions:
            if pos.get("symbol", "").upper() == symbol.upper():
                position_qty = pos.get("quantity", 0)
                break

        if position_qty <= 0:
            return {
                "success": False, "order_id": None, "price": None, "qty": 0,
                "error": f"No {symbol} position to sell",
            }

        return await _place_order(symbol, "Sell to Close", position_qty)

    except Exception as e:
        _log.error("Tastytrade sell_all error: %s", e)
        return {"success": False, "order_id": None, "price": None, "qty": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Positions & Open Orders
# ---------------------------------------------------------------------------

async def get_positions() -> list:
    """Get all current positions.

    Returns: list of {symbol, quantity, cost_basis, market_value, ...}
    """
    try:
        account = await _get_account_number()
        headers = await _auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TASTYTRADE_API}/accounts/{account}/positions",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                items = resp.json().get("data", {}).get("items", [])
                positions = []
                for item in items:
                    pos = item if isinstance(item, dict) else {}
                    positions.append({
                        "symbol": pos.get("symbol", ""),
                        "quantity": float(pos.get("quantity", 0)),
                        "quantity_direction": pos.get("quantity-direction", ""),
                        "instrument_type": pos.get("instrument-type", ""),
                        "cost_basis": float(pos.get("average-open-price", 0)),
                        "market_value": float(pos.get("close-price", 0)) * float(pos.get("quantity", 0)),
                    })
                return positions
            return []
    except Exception as e:
        _log.error("Tastytrade positions error: %s", e)
        return []


async def get_open_orders() -> list:
    """Get all open/live orders.

    Returns: list of order dicts
    """
    try:
        account = await _get_account_number()
        headers = await _auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TASTYTRADE_API}/accounts/{account}/orders/live",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                items = resp.json().get("data", {}).get("items", [])
                orders = []
                for item in items:
                    order = item if isinstance(item, dict) else {}
                    legs = order.get("legs", [])
                    orders.append({
                        "id": order.get("id"),
                        "status": order.get("status", ""),
                        "order_type": order.get("order-type", ""),
                        "time_in_force": order.get("time-in-force", ""),
                        "symbol": legs[0].get("symbol", "") if legs else "",
                        "action": legs[0].get("action", "") if legs else "",
                        "quantity": float(legs[0].get("quantity", 0)) if legs else 0,
                    })
                return orders
            return []
    except Exception as e:
        _log.error("Tastytrade open orders error: %s", e)
        return []


async def cancel_order(order_id: str) -> bool:
    """Cancel an open order.

    Args:
        order_id: the Tastytrade order ID

    Returns: True if cancelled successfully
    """
    try:
        account = await _get_account_number()
        headers = await _auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{TASTYTRADE_API}/accounts/{account}/orders/{order_id}",
                headers=headers, timeout=10,
            )
            if resp.status_code in (200, 204):
                _log.info("Cancelled order %s", order_id)
                return True
            _log.warning("Cancel order %s failed: %s", order_id, resp.status_code)
            return False
    except Exception as e:
        _log.error("Tastytrade cancel error: %s", e)
        return False
