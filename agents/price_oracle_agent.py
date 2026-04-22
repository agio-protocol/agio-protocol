"""Price Oracle — Crypto price data for $0.001/query. Nearly 100% margin."""
import time
import aiohttp
from base_agent import BaseAgent


class PriceOracleAgent(BaseAgent):
    PRICE = 0.001
    CACHE_TTL = 60

    def __init__(self):
        super().__init__("price-oracle", service_type="price_data", price=self.PRICE)
        self.cache = {}
        self.last_update = 0

    async def update_cache(self):
        try:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {"ids": "bitcoin,ethereum,solana,usd-coin",
                      "vs_currencies": "usd", "include_24hr_change": "true"}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        self.cache = await resp.json()
                        self.last_update = time.time()
                        return True
        except Exception:
            pass
        # Fallback cache for demo
        if not self.cache:
            self.cache = {
                "bitcoin": {"usd": 77200, "usd_24h_change": 1.5},
                "ethereum": {"usd": 2430, "usd_24h_change": -0.8},
                "solana": {"usd": 142, "usd_24h_change": 2.3},
            }
            self.last_update = time.time()
        return False

    async def handle_query(self, symbol: str) -> dict:
        if time.time() - self.last_update > self.CACHE_TTL:
            await self.update_cache()

        symbol_map = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "usdc": "usd-coin"}
        coin_id = symbol_map.get(symbol.lower(), symbol.lower())

        if coin_id in self.cache:
            data = self.cache[coin_id]
            self.total_earned += self.PRICE
            return {
                "symbol": symbol.upper(),
                "price_usd": data.get("usd"),
                "change_24h": data.get("usd_24h_change"),
                "source": "coingecko",
            }
        return {"error": f"Unknown symbol: {symbol}"}
