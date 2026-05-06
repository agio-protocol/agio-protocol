# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Alpaca Exchange Service — executes stock trades via Alpaca Markets."""
import logging
import os

import httpx

_log = logging.getLogger("alpaca-exchange")

# Paper vs live determined by which URL is used
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"


def _get_base_url():
    return os.getenv("ALPACA_BASE_URL", ALPACA_PAPER_URL)


def _get_headers():
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET not set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


MAX_ORDER_USD = float(os.getenv("ALPACA_MAX_ORDER_USD", "200"))


async def get_account() -> dict:
    """Get account info (cash, equity, buying power)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_get_base_url()}/v2/account",
                headers=_get_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "success": True,
                    "cash": float(data.get("cash", 0)),
                    "equity": float(data.get("equity", 0)),
                    "buying_power": float(data.get("buying_power", 0)),
                    "portfolio_value": float(data.get("portfolio_value", 0)),
                    "currency": data.get("currency", "USD"),
                    "account_blocked": data.get("account_blocked", False),
                    "trading_blocked": data.get("trading_blocked", False),
                    "error": None,
                }
            else:
                return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        _log.error("Alpaca get_account error: %s", e)
        return {"success": False, "error": str(e)}


async def get_positions() -> list:
    """Get all open positions."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_get_base_url()}/v2/positions",
                headers=_get_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                positions = resp.json()
                return [
                    {
                        "symbol": p.get("symbol", ""),
                        "qty": float(p.get("qty", 0)),
                        "market_value": float(p.get("market_value", 0)),
                        "avg_entry_price": float(p.get("avg_entry_price", 0)),
                        "current_price": float(p.get("current_price", 0)),
                        "unrealized_pl": float(p.get("unrealized_pl", 0)),
                        "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                        "side": p.get("side", "long"),
                    }
                    for p in positions
                ]
            else:
                _log.error("Alpaca get_positions HTTP %s: %s", resp.status_code, resp.text)
                return []
    except Exception as e:
        _log.error("Alpaca get_positions error: %s", e)
        return []


async def get_position(symbol: str) -> dict | None:
    """Get a single open position by symbol."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_get_base_url()}/v2/positions/{symbol.upper()}",
                headers=_get_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                p = resp.json()
                return {
                    "symbol": p.get("symbol", ""),
                    "qty": float(p.get("qty", 0)),
                    "market_value": float(p.get("market_value", 0)),
                    "avg_entry_price": float(p.get("avg_entry_price", 0)),
                    "current_price": float(p.get("current_price", 0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                    "side": p.get("side", "long"),
                }
            elif resp.status_code == 404:
                return None  # No position in this symbol
            else:
                _log.error("Alpaca get_position HTTP %s: %s", resp.status_code, resp.text)
                return None
    except Exception as e:
        _log.error("Alpaca get_position error: %s", e)
        return None


async def get_price(symbol: str) -> float:
    """Get latest price via Alpaca data API, with Yahoo Finance fallback."""
    # Try Alpaca data API first
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{ALPACA_DATA_URL}/v2/stocks/{symbol.upper()}/trades/latest",
                headers=_get_headers(),
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                trade = data.get("trade", {})
                price = float(trade.get("p", 0))
                if price > 0:
                    return price
    except Exception as e:
        _log.warning("Alpaca price API failed for %s: %s — trying Yahoo", symbol, e)

    # Yahoo Finance fallback
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    price = float(meta.get("regularMarketPrice", 0))
                    if price > 0:
                        return price
    except Exception as e:
        _log.error("Yahoo price fallback failed for %s: %s", symbol, e)

    return 0


async def buy(symbol: str, amount_usd: float) -> dict:
    """Market buy with fractional shares via notional order.

    Args:
        symbol: e.g. "AAPL", "MSFT"
        amount_usd: USD value to buy

    Returns: {success, order_id, price, qty, error}
    """
    # Hard cap on order size
    if amount_usd > MAX_ORDER_USD:
        return {"success": False, "order_id": None, "price": None, "qty": None,
                "error": f"Order ${amount_usd:.2f} exceeds max ${MAX_ORDER_USD:.2f}"}
    if amount_usd <= 0:
        return {"success": False, "order_id": None, "price": None, "qty": None,
                "error": "Invalid order amount"}

    try:
        price = await get_price(symbol)
        if price <= 0:
            return {"success": False, "order_id": None, "price": None, "qty": None,
                    "error": f"Could not get price for {symbol}"}

        order_payload = {
            "symbol": symbol.upper(),
            "notional": f"{amount_usd:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_get_base_url()}/v2/orders",
                headers=_get_headers(),
                json=order_payload,
                timeout=15,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("id", "")
                filled_qty = float(data.get("filled_qty", 0)) or (amount_usd / price)
                filled_price = float(data.get("filled_avg_price", 0)) or price

                _log.info("Alpaca BUY %s: $%.2f notional @ ~$%.2f (order: %s)",
                          symbol, amount_usd, filled_price, order_id)

                return {
                    "success": True,
                    "order_id": order_id,
                    "price": filled_price,
                    "qty": filled_qty,
                    "usd_value": amount_usd,
                    "error": None,
                }
            else:
                error_msg = "Order rejected"
                try:
                    msg = resp.json().get("message", "")
                    error_msg = msg[:100] if msg else f"HTTP {resp.status_code}"
                except Exception:
                    error_msg = f"HTTP {resp.status_code}"
                _log.error("Alpaca BUY failed: %s", error_msg)
                return {"success": False, "order_id": None, "price": price, "qty": None,
                        "error": f"HTTP {resp.status_code}: {error_msg}"}

    except Exception as e:
        _log.error("Alpaca buy error: %s", e)
        return {"success": False, "order_id": None, "price": None, "qty": None, "error": str(e)}


async def sell(symbol: str, amount_usd: float) -> dict:
    """Market sell partial position via notional order.

    Args:
        symbol: e.g. "AAPL", "MSFT"
        amount_usd: USD value to sell

    Returns: {success, order_id, price, qty, error}
    """
    if amount_usd <= 0:
        return {"success": False, "order_id": None, "price": None, "qty": None,
                "error": "Invalid order amount"}

    try:
        price = await get_price(symbol)
        if price <= 0:
            return {"success": False, "order_id": None, "price": None, "qty": None,
                    "error": f"Could not get price for {symbol}"}

        order_payload = {
            "symbol": symbol.upper(),
            "notional": f"{amount_usd:.2f}",
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_get_base_url()}/v2/orders",
                headers=_get_headers(),
                json=order_payload,
                timeout=15,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("id", "")
                filled_qty = float(data.get("filled_qty", 0)) or (amount_usd / price)
                filled_price = float(data.get("filled_avg_price", 0)) or price

                _log.info("Alpaca SELL %s: $%.2f notional @ ~$%.2f (order: %s)",
                          symbol, amount_usd, filled_price, order_id)

                return {
                    "success": True,
                    "order_id": order_id,
                    "price": filled_price,
                    "qty": filled_qty,
                    "usd_value": amount_usd,
                    "error": None,
                }
            else:
                error_msg = resp.text
                try:
                    error_msg = resp.json().get("message", resp.text)
                except Exception:
                    pass
                _log.error("Alpaca SELL failed HTTP %s: %s", resp.status_code, error_msg)
                return {"success": False, "order_id": None, "price": price, "qty": None,
                        "error": f"HTTP {resp.status_code}: {error_msg}"}

    except Exception as e:
        _log.error("Alpaca sell error: %s", e)
        return {"success": False, "order_id": None, "price": None, "qty": None, "error": str(e)}


async def sell_all(symbol: str) -> dict:
    """Close entire position in a symbol.

    Uses Alpaca's DELETE /v2/positions/{symbol} endpoint which liquidates the full position.

    Returns: {success, order_id, price, qty, error}
    """
    try:
        price = await get_price(symbol)

        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{_get_base_url()}/v2/positions/{symbol.upper()}",
                headers=_get_headers(),
                timeout=15,
            )

            if resp.status_code in (200, 204):
                data = resp.json() if resp.status_code == 200 else {}
                order_id = data.get("id", "")

                _log.info("Alpaca SELL ALL %s: closed entire position (order: %s)",
                          symbol, order_id)

                return {
                    "success": True,
                    "order_id": order_id,
                    "price": price,
                    "qty": None,
                    "error": None,
                }
            elif resp.status_code == 404:
                return {"success": False, "order_id": None, "price": None, "qty": None,
                        "error": f"No open position in {symbol}"}
            else:
                error_msg = resp.text
                try:
                    error_msg = resp.json().get("message", resp.text)
                except Exception:
                    pass
                _log.error("Alpaca SELL ALL failed HTTP %s: %s", resp.status_code, error_msg)
                return {"success": False, "order_id": None, "price": price, "qty": None,
                        "error": f"HTTP {resp.status_code}: {error_msg}"}

    except Exception as e:
        _log.error("Alpaca sell_all error: %s", e)
        return {"success": False, "order_id": None, "price": None, "qty": None, "error": str(e)}


async def get_open_orders() -> list:
    """Get all open orders."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_get_base_url()}/v2/orders",
                headers=_get_headers(),
                params={"status": "open"},
                timeout=10,
            )
            if resp.status_code == 200:
                orders = resp.json()
                return [
                    {
                        "id": o.get("id", ""),
                        "symbol": o.get("symbol", ""),
                        "side": o.get("side", ""),
                        "type": o.get("type", ""),
                        "notional": o.get("notional"),
                        "qty": o.get("qty"),
                        "filled_qty": o.get("filled_qty"),
                        "status": o.get("status", ""),
                        "submitted_at": o.get("submitted_at", ""),
                    }
                    for o in orders
                ]
            else:
                _log.error("Alpaca get_open_orders HTTP %s: %s", resp.status_code, resp.text)
                return []
    except Exception as e:
        _log.error("Alpaca get_open_orders error: %s", e)
        return []


async def cancel_order(order_id: str) -> dict:
    """Cancel an order by ID.

    Returns: {success, error}
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{_get_base_url()}/v2/orders/{order_id}",
                headers=_get_headers(),
                timeout=10,
            )
            if resp.status_code in (200, 204):
                _log.info("Alpaca cancelled order: %s", order_id)
                return {"success": True, "error": None}
            elif resp.status_code == 404:
                return {"success": False, "error": f"Order {order_id} not found"}
            else:
                error_msg = resp.text
                try:
                    error_msg = resp.json().get("message", resp.text)
                except Exception:
                    pass
                return {"success": False, "error": f"HTTP {resp.status_code}: {error_msg}"}
    except Exception as e:
        _log.error("Alpaca cancel_order error: %s", e)
        return {"success": False, "error": str(e)}
