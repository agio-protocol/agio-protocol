# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Social Sentiment Scraper — three separate scrapers for memes, crypto, and stocks.
Each monitors different subreddits, channels, and token/ticker lists.
Detects multi-platform convergence within each category.
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("sentiment")

REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = "AgiotageBot/1.0"

POLL_INTERVAL = 120
SIGNAL_WINDOW_HOURS = 6

# === KOL TWITTER WATCHLISTS ===
# These are the accounts that matter. When they mention a token, it's a signal.

KOL_WATCHLIST = {
    "meme": [
        # Tier 1 — memecoin callers (move markets instantly)
        "ansem", "JamesWynn", "CryptoGodJohn", "thealphaclub_", "MustStopMurad",
        "blknoiz06", "DegenSpartanDeFi", "CryptoKaduna", "GiganticRebirth",
        "CryptoBullet1", "Hsaka_", "Rewkang", "Lookonchain", "EmberCN",
        "SolanaSensei", "soltrader_sol", "degencoinflip", "MacnBTC",
        "notthreadguy", "ZssBecker", "TheMoonCarl", "AltcoinSherpa",
        # Tier 2 — early alpha callers
        "HsakaTrades", "inversebrah", "DegenerateNews", "SolanaLegend",
        "TheDeFiEdge", "MilesDeutscher", "BarrySilbert", "WClementeIII",
        "CryptoCapo_", "ZoomerOracle", "ColdBloodShill", "AriDavidPaul",
        "TrungTPhan", "CryptoCobain", "eulogyeth", "melostar",
        # Tools & scanners
        "daborexscreener", "GMGNAI", "birdeye_so", "pumpdotfun", "BonkBot",
        "TrojanOnSolana", "AxiomTradeHQ", "KOLScan_", "daborexscreener",
        "photon_sol", "BullX_io", "definaps",
        # Meme news & communities
        "SolanaFloor", "memecoinsdaily", "Solana_Tracker", "SolanaFM",
        "helowinship", "MagicEden", "tensor_hq",
        # VIP — moves markets when they tweet
        "elonmusk", "realDonaldTrump", "cooaborker", "orangie", "cb_doge",
    ],
    "crypto": [
        # Macro & market structure
        "CryptoHayes", "RaoulGMI", "novogratz", "balajis", "PlanB",
        "jimbianco", "Bloqport", "CryptoKaleo", "APompliano",
        # Trading & TA
        "pentoshi", "CryptoWendyO", "ScottMelker", "MMCrypto", "ToneVays",
        "TheCryptoDog", "Citrini7", "CryptoCred", "SmartContracter",
        "trader_XO", "CryptoMichNL", "TechDev_52", "ELM0_xyz",
        # Founders & builders
        "VitalikButerin", "aeyakovenko", "cz_binance", "jessepollak",
        "brian_armstrong", "kaborrakis", "staboraylor",
        # Whale & data accounts
        "WuBlockchain", "DeItaone", "Cobie_Crypto", "nansen_ai", "glassnode",
        "DeFi_Pulse", "MessariCrypto", "TheBlock__", "whale_alert",
        "santaborafeed", "IntoTheBlock", "DuneAnalytics", "defaborillama",
        # AI + crypto
        "shaw_ai16z", "virtuals_io", "CoinbaseDevs", "FetchAI",
        "RenderToken", "baborittensor",
        # VIP — institutional voices
        "saboraylor", "CathieDWood", "elonmusk", "coinbase", "binance",
    ],
    "stocks": [
        # Macro & analysis
        "ritholtz", "BenCarlsonCFA", "MebFaber", "LizAnnSonders", "BuccoCapital",
        "TaviCosta", "biaboranco", "SoberLook",
        # Day trading & TA
        "RedDogT3", "tradertvneal", "DanZanger1", "TradeWithAlerts",
        # Fundamentals & earnings
        "StockJabber", "MattLevine", "OnlyCFO", "EarningsWhispers",
        # News
        "Newsquawk", "DeItaone", "Benzinga", "StockTwits", "WSJ", "CNBC",
        "MarketWatch", "unusual_whales", "CongressTrading",
        # Options
        "SpotGamma", "SqueezeMetrics", "OptionsAction",
        # Contrarian
        "NourielRoubini", "PeterSchiff",
        # VIP — market movers
        "elonmusk", "realDonaldTrump", "saboraylor", "CathieDWood",
        "brian_armstrong", "jimcramer", "chaabormath",
    ],
}


# === CATEGORY CONFIGS ===

CATEGORIES = {
    "meme": {
        "subreddits": ["CryptoMoonShots", "memecoin", "SatoshiStreetBets", "solana",
                       "pumpfun", "wallstreetbetsOGs", "dexscreener"],
        "telegram": ["SolanaFloor", "daboradex", "PumpFunPortal", "MemeCoinsGems"],
        "tokens": {
            "pepe": "pepe", "shib": "shiba inu", "bonk": "bonk", "wif": "dogwifhat",
            "floki": "floki", "doge": "dogecoin", "trump": "official trump",
            "melania": "melania", "fartcoin": "fartcoin", "pengu": "pudgy penguins",
            "popcat": "popcat", "mog": "mog coin", "brett": "brett", "neiro": "neiro",
            "goat": "goatseus maximus", "act": "act", "pnut": "peanut the squirrel",
            "wen": "wen", "myro": "myro", "bome": "book of meme", "slerf": "slerf",
            "wen": "wen", "ponke": "ponke", "mew": "cat in a dogs world",
            "mother": "mother iggy", "billy": "billy", "giga": "giga chad",
        },
        "false_positives": {"act", "mew", "wen"},
    },
    "crypto": {
        "subreddits": ["cryptocurrency", "bitcoin", "ethereum", "ethtrader",
                       "CryptoCurrency", "altcoin", "defi", "CryptoMarkets"],
        "telegram": ["crypto", "WhaleTrades", "CoinGeckoAnnouncements", "CryptoSignals"],
        "tokens": {
            "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "ripple",
            "ada": "cardano", "avax": "avalanche", "dot": "polkadot", "link": "chainlink",
            "atom": "cosmos", "uni": "uniswap", "aave": "aave", "mkr": "maker",
            "matic": "polygon", "arb": "arbitrum", "op": "optimism", "near": "near protocol",
            "sui": "sui", "apt": "aptos", "sei": "sei", "inj": "injective",
            "fet": "fetch.ai", "rndr": "render", "tao": "bittensor",
            "hbar": "hedera", "algo": "algorand", "icp": "internet computer",
            "fil": "filecoin", "grt": "the graph", "snx": "synthetix",
            "crv": "curve", "ldo": "lido", "pendle": "pendle", "jup": "jupiter",
            "ondo": "ondo", "kas": "kaspa", "zec": "zcash", "bnb": "binance coin",
        },
        "false_positives": {"op", "near", "link", "dot", "uni", "gas", "the", "and"},
    },
    "stocks": {
        "subreddits": ["wallstreetbets", "stocks", "investing", "StockMarket",
                       "options", "Daytrading", "ValueInvesting", "SecurityAnalysis"],
        "telegram": ["WallStreetBetsOfficial", "StockMarketChat"],
        "tokens": {
            "aapl": "apple", "msft": "microsoft", "googl": "google alphabet",
            "amzn": "amazon", "nvda": "nvidia", "tsla": "tesla", "meta": "meta platforms",
            "nflx": "netflix", "amd": "amd", "intc": "intel", "crm": "salesforce",
            "orcl": "oracle", "adbe": "adobe", "pypl": "paypal", "sq": "block square",
            "shop": "shopify", "coin": "coinbase", "hood": "robinhood",
            "pltr": "palantir", "snow": "snowflake", "uber": "uber",
            "abnb": "airbnb", "rivn": "rivian", "lcid": "lucid",
            "sofi": "sofi", "dis": "disney", "baba": "alibaba",
            "jpm": "jpmorgan", "gs": "goldman sachs", "bac": "bank of america",
            "wmt": "walmart", "cost": "costco", "tgt": "target",
            "ba": "boeing", "cat": "caterpillar", "xom": "exxon",
            "cvx": "chevron", "ko": "coca cola", "pep": "pepsi",
            "jnj": "johnson and johnson", "pfe": "pfizer", "mrna": "moderna",
            "spy": "s&p 500 etf", "qqq": "nasdaq etf", "iwm": "russell etf",
            "vti": "total market etf", "gme": "gamestop", "amc": "amc entertainment",
            "bbby": "bed bath beyond", "smci": "super micro", "arm": "arm holdings",
            "mstr": "microstrategy",
        },
        "false_positives": {"meta", "cost", "cat", "ba", "ko", "arm", "sq"},
    },
}


# === DB MODELS ===

class SocialMention(Base):
    __tablename__ = "social_mentions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="crypto")
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, default=1)
    source_detail: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sample_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conviction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_mention_token", "token_symbol"),
        Index("idx_mention_time", "detected_at"),
        Index("idx_mention_category", "category"),
    )


class SentimentSignal(Base):
    __tablename__ = "sentiment_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="crypto")
    token_symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    token_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platform_count: Mapped[int] = mapped_column(Integer, nullable=False)
    platforms: Mapped[str] = mapped_column(Text, nullable=False)
    total_mentions: Mapped[int] = mapped_column(Integer, default=0)
    strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    has_smart_money: Mapped[bool] = mapped_column(Boolean, default=False)
    has_deployer: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_sentiment_signal_time", "detected_at"),
        Index("idx_sentiment_signal_category", "category"),
    )


# === HELPERS ===

def _extract_tokens(text: str, token_list: dict, false_positives: set) -> dict:
    text_lower = text.lower()
    mentions = defaultdict(int)
    for symbol, name in token_list.items():
        dollar_pattern = re.findall(r'\$' + re.escape(symbol) + r'\b', text_lower)
        if dollar_pattern:
            mentions[symbol.upper()] += len(dollar_pattern) * 2
        if name.lower() in text_lower:
            mentions[symbol.upper()] += 1
        if symbol not in false_positives and len(symbol) >= 3:
            pattern = re.findall(r'\b' + re.escape(symbol) + r'\b', text_lower)
            if pattern:
                mentions[symbol.upper()] += len(pattern)
    return dict(mentions)


def _analyze_sentiment(text: str, category: str, is_kol: bool = False) -> tuple:
    """Smart sentiment analysis. Returns (label, score, conviction).
    Score: -100 (extreme bearish) to +100 (extreme bullish).
    Conviction: 0-100 how confident the signal is.
    """
    text_lower = text.lower()
    score = 0
    conviction = 0

    # === BULLISH PATTERNS (context-aware) ===
    strong_bull = {
        "all in": 25, "loading up": 20, "just bought": 20, "buying more": 18,
        "going long": 18, "100x": 25, "1000x": 30, "to the moon": 15,
        "lfg": 12, "wagmi": 10, "diamond hands": 12, "not selling": 15,
        "accumulating": 18, "added to my position": 20, "doubled down": 22,
        "massive breakout": 20, "bullish af": 20, "this is the one": 15,
        "generational buy": 25, "life changing": 20, "undervalued": 15,
        "sleeping on this": 12, "early": 10, "gem": 12, "alpha": 10,
    }
    mild_bull = {
        "buy": 5, "long": 5, "bullish": 8, "moon": 6, "pump": 5,
        "breakout": 8, "rally": 7, "ath": 8, "rocket": 6,
        "accumulate": 8, "upgrade": 7, "beat earnings": 10,
        "calls": 6, "squeeze": 8, "yolo": 5, "tendies": 4,
        "looks good": 6, "like this": 4, "watching": 3,
    }

    # === BEARISH PATTERNS ===
    strong_bear = {
        "selling everything": -25, "just sold": -20, "get out now": -25,
        "going to zero": -30, "rug pull": -25, "rugged": -25, "scam": -20,
        "ponzi": -22, "dead project": -20, "avoid at all costs": -25,
        "ngmi": -10, "exit scam": -25, "honey pot": -25, "honeypot": -25,
        "dumping hard": -20, "crash incoming": -20, "bear market": -12,
        "overvalued": -12, "top is in": -15, "shorting": -15,
        "miss earnings": -12, "downgrade": -10, "recession": -8,
    }
    mild_bear = {
        "sell": -5, "short": -5, "bearish": -8, "dump": -6, "crash": -7,
        "dead": -5, "avoid": -6, "bubble": -7, "puts": -6,
        "red": -3, "bag": -4, "loss": -3, "rekt": -6,
        "careful": -3, "risky": -4, "worried": -4,
    }

    # Apply strong patterns first
    for pattern, points in strong_bull.items():
        if pattern in text_lower:
            score += points
            conviction += abs(points)
    for pattern, points in strong_bear.items():
        if pattern in text_lower:
            score += points
            conviction += abs(points)
    # Then mild patterns
    for pattern, points in mild_bull.items():
        if pattern in text_lower:
            score += points
            conviction += abs(points) // 2
    for pattern, points in mild_bear.items():
        if pattern in text_lower:
            score += points
            conviction += abs(points) // 2

    # === NEGATION DETECTION ===
    negation_words = ["not ", "don't ", "dont ", "never ", "no way ", "wouldn't ", "isn't ", "ain't "]
    for neg in negation_words:
        if neg in text_lower:
            # Check if negation is near a sentiment word
            neg_pos = text_lower.find(neg)
            nearby = text_lower[neg_pos:neg_pos+30]
            if any(w in nearby for w in ["buy", "bullish", "moon", "long", "good"]):
                score -= 15  # "not buying" = bearish
            if any(w in nearby for w in ["sell", "bearish", "dump", "short", "bad"]):
                score += 15  # "not selling" = bullish

    # === SARCASM DETECTION ===
    sarcasm_patterns = ["great another", "love to see", "wow so", "totally not",
                        "what could go wrong", "surely this time", "lmao", "😂"]
    has_sarcasm = any(p in text_lower for p in sarcasm_patterns)
    if has_sarcasm and score > 0:
        score = -score // 2  # Flip positive sentiment if sarcasm detected

    # === PRICE TARGET DETECTION ===
    import re
    price_targets = re.findall(r'\$[\d,]+[kKmM]?', text)
    if price_targets and len(price_targets) >= 1:
        conviction += 10  # Specific price targets = higher conviction

    # === EMOJI CONTEXT ===
    bull_emojis = text.count('🚀') + text.count('🔥') + text.count('💎') + text.count('📈') + text.count('💰')
    bear_emojis = text.count('📉') + text.count('💀') + text.count('🗑') + text.count('⚠️') + text.count('🚨')
    score += bull_emojis * 3 - bear_emojis * 3

    # === KOL WEIGHTING ===
    if is_kol:
        score = int(score * 1.5)
        conviction = int(conviction * 1.5)

    # Clamp score
    score = max(-100, min(100, score))
    conviction = max(0, min(100, conviction))

    # Determine label
    if score >= 30:
        label = "VERY_BULLISH"
    elif score >= 10:
        label = "BULLISH"
    elif score <= -30:
        label = "VERY_BEARISH"
    elif score <= -10:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return label, score, conviction


def _simple_sentiment(text: str, category: str) -> str:
    """Backward-compatible wrapper returning just the label."""
    label, score, conviction = _analyze_sentiment(text, category)
    return label


# === REDDIT ===

_reddit_token = None
_reddit_token_expires = 0


async def _get_reddit_token(client: httpx.AsyncClient) -> str | None:
    global _reddit_token, _reddit_token_expires
    if _reddit_token and time.time() < _reddit_token_expires:
        return _reddit_token
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        return None
    try:
        resp = await client.post("https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_USER_AGENT}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _reddit_token = data.get("access_token")
            _reddit_token_expires = time.time() + data.get("expires_in", 3600) - 60
            return _reddit_token
    except Exception as e:
        _log.debug(f"Reddit auth failed: {e}")
    return None


async def _scrape_reddit(client: httpx.AsyncClient, category: str) -> list:
    token = await _get_reddit_token(client)
    if not token:
        return []
    config = CATEGORIES[category]
    all_mentions = []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": REDDIT_USER_AGENT}

    for sub in config["subreddits"]:
        try:
            resp = await client.get(f"https://oauth.reddit.com/r/{sub}/hot?limit=25",
                                    headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            posts = resp.json().get("data", {}).get("children", [])
            for post in posts:
                data = post.get("data", {})
                title = data.get("title", "")
                selftext = data.get("selftext", "")[:500]
                full_text = f"{title} {selftext}"
                score = data.get("score", 0)
                num_comments = data.get("num_comments", 0)
                if score < 5 and num_comments < 3:
                    continue
                tokens = _extract_tokens(full_text, config["tokens"], config["false_positives"])
                label, sent_score, conv = _analyze_sentiment(full_text, category)
                for sym, count in tokens.items():
                    all_mentions.append({
                        "category": category, "platform": "reddit", "symbol": sym,
                        "count": count, "source": f"r/{sub}", "text": title[:200],
                        "sentiment": label, "score": sent_score, "conviction": conv,
                    })
            await asyncio.sleep(1)
        except Exception as e:
            _log.debug(f"Reddit r/{sub} error: {e}")
    return all_mentions


# === TELEGRAM ===

async def _scrape_telegram(client: httpx.AsyncClient, category: str) -> list:
    config = CATEGORIES[category]
    all_mentions = []
    for channel in config["telegram"]:
        try:
            resp = await client.get(f"https://t.me/s/{channel}", timeout=10,
                                    headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            messages = re.findall(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', resp.text, re.DOTALL)
            for msg in messages[-20:]:
                clean = re.sub(r'<[^>]+>', '', msg)
                tokens = _extract_tokens(clean, config["tokens"], config["false_positives"])
                sentiment = _simple_sentiment(clean, category)
                for sym, count in tokens.items():
                    all_mentions.append({
                        "category": category, "platform": "telegram", "symbol": sym,
                        "count": count, "source": f"t.me/{channel}", "text": clean[:200],
                        "sentiment": sentiment,
                    })
            await asyncio.sleep(1)
        except Exception as e:
            _log.debug(f"Telegram {channel} error: {e}")
    return all_mentions


# === COINGECKO TRENDING (crypto/meme only) ===

async def _scrape_coingecko_trending(client: httpx.AsyncClient) -> list:
    all_mentions = []
    try:
        resp = await client.get("https://api.coingecko.com/api/v3/search/trending", timeout=15)
        if resp.status_code == 200:
            coins = resp.json().get("coins", [])
            for coin in coins:
                item = coin.get("item", {})
                symbol = (item.get("symbol", "") or "").upper()
                name = item.get("name", "")
                score = item.get("score", 0)

                # Determine category
                cat = "crypto"
                meme_symbols = {s.upper() for s in CATEGORIES["meme"]["tokens"]}
                if symbol in meme_symbols:
                    cat = "meme"

                if symbol:
                    all_mentions.append({
                        "category": cat, "platform": "coingecko", "symbol": symbol,
                        "count": max(1, 10 - score),
                        "source": f"CoinGecko Trending #{score+1}",
                        "text": f"{name} trending on CoinGecko",
                        "sentiment": "BULLISH",
                    })
    except Exception as e:
        _log.debug(f"CoinGecko trending error: {e}")
    return all_mentions


# === STOCKTWITS (covers crypto + stocks + memes — free API, has sentiment) ===

STOCKTWITS_SYMBOLS = {
    "meme": ["PEPE.X", "SHIB.X", "BONK.X", "WIF.X", "FLOKI.X", "DOGE.X",
             "TRUMP.X", "PENGU.X", "POPCAT.X", "MOG.X", "BRETT.X", "NEIRO.X"],
    "crypto": ["BTC.X", "ETH.X", "SOL.X", "XRP.X", "ADA.X", "AVAX.X", "LINK.X",
               "DOT.X", "ATOM.X", "UNI.X", "NEAR.X", "SUI.X", "APT.X", "SEI.X",
               "INJ.X", "TAO.X", "FET.X", "RNDR.X", "ARB.X", "ONDO.X",
               "ZEC.X", "HBAR.X", "KAS.X", "LTC.X"],
    "stocks": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
               "AMD", "PLTR", "COIN", "HOOD", "SOFI", "GME", "AMC",
               "SPY", "QQQ", "SMCI", "ARM", "MSTR", "NFLX"],
}


async def _scrape_stocktwits(client: httpx.AsyncClient, category: str) -> list:
    """Scrape StockTwits for sentiment — free API with bullish/bearish labels."""
    all_mentions = []
    symbols = STOCKTWITS_SYMBOLS.get(category, [])

    # Get trending first
    try:
        resp = await client.get("https://api.stocktwits.com/api/2/trending/symbols.json", timeout=10)
        if resp.status_code == 200:
            trending = resp.json().get("symbols", [])
            for s in trending[:15]:
                sym_raw = s.get("symbol", "")
                sym_clean = sym_raw.replace(".X", "").upper()
                # Check if this symbol belongs to this category
                cat_tokens = {k.upper() for k in CATEGORIES[category]["tokens"]}
                if sym_clean in cat_tokens or sym_raw in symbols:
                    all_mentions.append({
                        "category": category, "platform": "stocktwits", "symbol": sym_clean,
                        "count": max(1, s.get("watchlist_count", 0) // 10000),
                        "source": f"StockTwits Trending",
                        "text": f"{s.get('title','')} trending on StockTwits ({s.get('watchlist_count',0):,} watchers)",
                        "sentiment": "BULLISH",
                    })
    except Exception as e:
        _log.debug(f"StockTwits trending error: {e}")

    # Check top 5 symbols for this category for message volume and sentiment
    for sym in symbols[:8]:
        try:
            resp = await client.get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json", timeout=10)
            if resp.status_code != 200:
                await asyncio.sleep(1)
                continue
            data = resp.json()
            msgs = data.get("messages", [])
            sym_info = data.get("symbol", {})
            sym_clean = sym.replace(".X", "").upper()

            if not msgs:
                await asyncio.sleep(1)
                continue

            bullish = sum(1 for m in msgs if (m.get("entities", {}) or {}).get("sentiment", {}) and
                         m["entities"]["sentiment"].get("basic") == "Bullish")
            bearish = sum(1 for m in msgs if (m.get("entities", {}) or {}).get("sentiment", {}) and
                         m["entities"]["sentiment"].get("basic") == "Bearish")

            sentiment = "BULLISH" if bullish > bearish + 2 else "BEARISH" if bearish > bullish + 2 else "NEUTRAL"

            # Only add if there's meaningful activity
            if len(msgs) >= 5:
                sample = msgs[0].get("body", "")[:200] if msgs else ""
                all_mentions.append({
                    "category": category, "platform": "stocktwits", "symbol": sym_clean,
                    "count": len(msgs),
                    "source": f"StockTwits ${sym_clean} ({bullish}B/{bearish}b)",
                    "text": sample,
                    "sentiment": sentiment,
                })

            await asyncio.sleep(1)
        except Exception as e:
            _log.debug(f"StockTwits {sym} error: {e}")

    return all_mentions


# === KOL WATCHLIST SCRAPING (StockTwits profiles + syndication embeds) ===

async def _scrape_kol_watchlist(client: httpx.AsyncClient, category: str) -> list:
    """Check KOL watchlist accounts for recent mentions via multiple free methods."""
    all_mentions = []
    watchlist = KOL_WATCHLIST.get(category, [])
    config = CATEGORIES[category]

    # Method 1: Check StockTwits for watchlist users who have ST accounts
    seen_texts = set()  # Deduplicate within this cycle
    for handle in watchlist[:8]:
        try:
            resp = await client.get(f"https://api.stocktwits.com/api/2/streams/user/{handle}.json",
                                    timeout=8)
            if resp.status_code != 200:
                await asyncio.sleep(0.5)
                continue

            msgs = resp.json().get("messages", [])
            for msg in msgs[:5]:
                body = (msg.get("body", "") or "").strip()
                msg_id = msg.get("id", "")

                # Skip empty, too short, or duplicate content
                if not body or len(body) < 20:
                    continue
                # Skip if we've seen this exact text already (bio/pinned repeats)
                text_hash = body[:80]
                if text_hash in seen_texts:
                    continue
                seen_texts.add(text_hash)

                symbols = msg.get("symbols", [])
                sent = msg.get("entities", {}) or {}
                sent_val = (sent.get("sentiment", {}) or {}).get("basic", "")

                for sym_info in symbols:
                    sym = sym_info.get("symbol", "").replace(".X", "").upper()
                    cat_tokens = {k.upper() for k in config["tokens"]}
                    if sym in cat_tokens:
                        label, score, conv = _analyze_sentiment(body, category, is_kol=True)
                        all_mentions.append({
                            "category": category, "platform": "twitter_kol",
                            "symbol": sym, "count": 2,
                            "source": f"@{handle}",
                            "text": body[:200],
                            "sentiment": label, "score": score, "conviction": conv,
                            "_msg_id": str(msg_id),
                        })

            await asyncio.sleep(1)
        except Exception:
            continue

    # Method 2: Twitter syndication embed for top KOLs
    for handle in watchlist[:5]:
        try:
            resp = await client.get(
                f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                timeout=8,
            )
            if resp.status_code != 200:
                continue

            # Extract text from syndication response
            text_blocks = re.findall(r'"text":"(.*?)"', resp.text)
            for block in text_blocks[:10]:
                block_decoded = block.encode().decode('unicode_escape', errors='ignore')
                tokens = _extract_tokens(block_decoded, config["tokens"], config["false_positives"])
                sentiment = _simple_sentiment(block_decoded, category)
                for sym, count in tokens.items():
                    all_mentions.append({
                        "category": category, "platform": "twitter_kol",
                        "symbol": sym, "count": count + 1,
                        "source": f"@{handle} (X/Twitter)",
                        "text": block_decoded[:200],
                        "sentiment": sentiment,
                    })

            await asyncio.sleep(1)
        except Exception:
            continue

    if all_mentions:
        _log.info(f"[{category}] KOL watchlist: {len(all_mentions)} mentions from tracked accounts")
    return all_mentions


# === KOL TWITTER ACTIVITY (from GMGN data — no Twitter API needed) ===

async def _scrape_kol_twitter(category: str) -> list:
    """Extract KOL Twitter activity from our smart money trade data.
    KOLs with twitter handles who are trading = twitter signal."""
    if category == "stocks":
        return []

    all_mentions = []
    try:
        async with async_session() as db:
            from .smart_money_tracker import SmartMoneyTrade, SmartMoneyWallet
            cutoff = datetime.utcnow() - timedelta(hours=6)

            # Get recent KOL trades with twitter handles
            kol_trades = (await db.execute(
                select(SmartMoneyTrade)
                .where(SmartMoneyTrade.is_kol == True,
                       SmartMoneyTrade.trade_time >= cutoff,
                       SmartMoneyTrade.side == "buy")
                .order_by(SmartMoneyTrade.trade_time.desc())
                .limit(100)
            )).scalars().all()

            # Group by token, collect KOL twitter handles
            token_kols = defaultdict(list)
            for t in kol_trades:
                twitter = getattr(t, 'wallet_twitter', '') or ''
                name = getattr(t, 'wallet_name', '') or ''
                if twitter or name:
                    token_kols[t.token_symbol].append({
                        "twitter": twitter, "name": name,
                        "usd": float(t.amount_usd or 0)
                    })

            config = CATEGORIES.get(category, {})
            cat_tokens = {k.upper() for k in config.get("tokens", {})}

            for symbol, kols in token_kols.items():
                if symbol not in cat_tokens and category == "meme":
                    continue
                if len(kols) < 1:
                    continue

                unique_kols = {}
                for k in kols:
                    handle = k["twitter"] or k["name"]
                    if handle and handle not in unique_kols:
                        unique_kols[handle] = k

                kol_names = [f"@{k['twitter']}" if k['twitter'] else k['name']
                             for k in unique_kols.values()]
                total_usd = sum(k["usd"] for k in unique_kols.values())

                all_mentions.append({
                    "category": category, "platform": "twitter_kol",
                    "symbol": symbol,
                    "count": len(unique_kols),
                    "source": f"KOL Twitter: {', '.join(kol_names[:3])}",
                    "text": f"{len(unique_kols)} KOL(s) buying ${symbol}: {', '.join(kol_names[:5])}. ${total_usd:,.0f} total.",
                    "sentiment": "BULLISH",
                })

    except Exception as e:
        _log.debug(f"KOL twitter scrape error: {e}")

    return all_mentions


# === SIGNAL DETECTION ===

async def _detect_signals(db: AsyncSession, category: str):
    cutoff = datetime.utcnow() - timedelta(hours=SIGNAL_WINDOW_HOURS)
    config = CATEGORIES[category]

    recent = (await db.execute(
        select(SocialMention)
        .where(SocialMention.category == category, SocialMention.detected_at >= cutoff)
    )).scalars().all()

    token_data = defaultdict(lambda: {"platforms": set(), "mentions": 0, "sentiments": [],
                                       "scores": [], "convictions": []})
    for m in recent:
        td = token_data[m.token_symbol]
        td["platforms"].add(m.platform)
        td["mentions"] += m.mention_count
        if m.sentiment:
            td["sentiments"].append(m.sentiment)
        if m.sentiment_score is not None:
            td["scores"].append(int(m.sentiment_score))
        if m.conviction is not None:
            td["convictions"].append(int(m.conviction))

    for symbol, data in token_data.items():
        platform_count = len(data["platforms"])
        if platform_count < 2:
            continue

        existing = (await db.execute(
            select(SentimentSignal)
            .where(SentimentSignal.token_symbol == symbol,
                   SentimentSignal.category == category,
                   SentimentSignal.detected_at >= cutoff)
        )).scalar_one_or_none()
        if existing:
            continue

        # Cross-reference with smart money (meme/crypto only)
        has_smart_money = False
        has_deployer = False
        if category in ("meme", "crypto"):
            try:
                from .smart_money_tracker import ClusterSignal
                has_smart_money = ((await db.execute(
                    select(func.count()).select_from(ClusterSignal)
                    .where(ClusterSignal.token_symbol == symbol, ClusterSignal.detected_at >= cutoff)
                )).scalar() or 0) > 0
            except:
                pass
        if category == "meme":
            try:
                from ..models.platform import MemeDeployment, TopDeployer
                dep = (await db.execute(
                    select(MemeDeployment).where(MemeDeployment.token_symbol == symbol)
                )).scalar_one_or_none()
                if dep and dep.deployer_wallet:
                    has_deployer = (await db.execute(
                        select(TopDeployer).where(TopDeployer.wallet == dep.deployer_wallet)
                    )).scalar_one_or_none() is not None
            except:
                pass

        bullish = sum(1 for s in data["sentiments"] if s in ("BULLISH", "VERY_BULLISH"))
        bearish = sum(1 for s in data["sentiments"] if s in ("BEARISH", "VERY_BEARISH"))
        avg_score = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0
        avg_conviction = sum(data["convictions"]) / len(data["convictions"]) if data["convictions"] else 0

        if platform_count >= 4 or (platform_count >= 3 and has_smart_money):
            strength = "VERY_STRONG"
        elif platform_count >= 3 or (platform_count >= 2 and (has_smart_money or has_deployer)):
            strength = "STRONG"
        else:
            strength = "MEDIUM"

        if has_smart_money and has_deployer and platform_count >= 2:
            strength = "VERY_STRONG"

        platforms_str = ",".join(sorted(data["platforms"]))
        token_name = config["tokens"].get(symbol.lower(), symbol)

        parts = [f"${symbol} trending across {platform_count} platforms ({platforms_str})"]
        parts.append(f"{data['mentions']} mentions")
        if avg_score >= 15:
            parts.append(f"sentiment: VERY BULLISH (score +{avg_score:.0f})")
        elif avg_score >= 5:
            parts.append(f"sentiment: BULLISH (score +{avg_score:.0f})")
        elif avg_score <= -15:
            parts.append(f"sentiment: VERY BEARISH (score {avg_score:.0f})")
        elif avg_score <= -5:
            parts.append(f"sentiment: BEARISH (score {avg_score:.0f})")
        else:
            parts.append(f"sentiment: MIXED (score {avg_score:.0f})")
        if avg_conviction > 30:
            parts.append(f"conviction: HIGH ({avg_conviction:.0f}/100)")
        if has_smart_money:
            parts.append("SMART MONEY ALSO BUYING")
        if has_deployer:
            parts.append("FROM TOP DEPLOYER")
        desc = ". ".join(parts)

        signal = SentimentSignal(
            category=category, token_symbol=symbol, token_name=token_name,
            platform_count=platform_count, platforms=platforms_str,
            total_mentions=data["mentions"], strength=strength,
            has_smart_money=has_smart_money, has_deployer=has_deployer,
            description=desc,
        )
        db.add(signal)

        _log.warning(f"SENTIMENT [{category.upper()}] [{strength}]: ${symbol} — {desc[:100]}")

        from ..models.platform import Notification
        db.add(Notification(
            agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
            type="sentiment_signal",
            title=f"{category.title()} Social [{strength}]: ${symbol}",
            body=desc[:200], link="/trading.html",
        ))

    await db.commit()


# === MAIN LOOPS ===

async def _run_category(category: str):
    """Run one scrape cycle for a category."""
    async with httpx.AsyncClient() as client:
        reddit = await _scrape_reddit(client, category)
        await asyncio.sleep(2)
        telegram = await _scrape_telegram(client, category)
        await asyncio.sleep(2)
        stocktwits = await _scrape_stocktwits(client, category)
        await asyncio.sleep(2)

        # CoinGecko trending for meme/crypto only
        trending = []
        if category in ("meme", "crypto"):
            trending = await _scrape_coingecko_trending(client)
            trending = [t for t in trending if t["category"] == category]

    # KOL watchlist scraping (StockTwits profiles + Twitter syndication)
    async with httpx.AsyncClient() as kol_client:
        kol_watchlist = await _scrape_kol_watchlist(kol_client, category)

    # KOL Twitter activity from GMGN on-chain data
    kol_onchain = await _scrape_kol_twitter(category)

    all_mentions = reddit + telegram + stocktwits + trending + kol_watchlist + kol_onchain
    kol_total = len(kol_watchlist) + len(kol_onchain)
    _log.info(f"[{category}] Scraped {len(reddit)} Reddit, {len(telegram)} Telegram, {len(stocktwits)} StockTwits, {kol_total} KOL/Twitter, {len(trending)} trending")

    if all_mentions:
        async with async_session() as db:
            # Get recent texts to deduplicate (don't store same content twice in 6h)
            cutoff = datetime.utcnow() - timedelta(hours=6)
            recent_texts = set()
            existing = (await db.execute(
                select(SocialMention.sample_text)
                .where(SocialMention.category == category,
                       SocialMention.detected_at >= cutoff,
                       SocialMention.sample_text.isnot(None))
            )).scalars().all()
            recent_texts = {(t[:80] if t else "") for t in existing}

            stored = 0
            for m in all_mentions:
                text = (m.get("text", "") or "")[:500]
                text_key = text[:80]
                if text_key in recent_texts:
                    continue
                recent_texts.add(text_key)

                mention = SocialMention(
                    category=m["category"], platform=m["platform"],
                    token_symbol=m["symbol"], mention_count=m.get("count", 1),
                    source_detail=m.get("source", ""), sample_text=text,
                    sentiment=m.get("sentiment"),
                    sentiment_score=m.get("score"),
                    conviction=m.get("conviction"),
                )
                db.add(mention)
                stored += 1
            await db.commit()
            if stored:
                _log.info(f"[{category}] Stored {stored} new unique mentions (filtered {len(all_mentions)-stored} dupes)")
            await _detect_signals(db, category)


async def run():
    _log.info("Sentiment tracker v2 starting — three scrapers: meme, crypto, stocks")
    await asyncio.sleep(25)

    while True:
        try:
            # Stagger each category
            await _run_category("meme")
            await asyncio.sleep(30)
            await _run_category("crypto")
            await asyncio.sleep(30)
            await _run_category("stocks")
        except Exception as e:
            _log.error(f"Sentiment tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run())
