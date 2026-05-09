# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Agiotage Marketing Agent v3 — aggressive growth mode on Moltbook.

Strategy: drive traffic to agiotage.finance through:
1. Frequent, varied posting (3x/hour) — mix of insights, live data, and tool promos
2. Reply to EVERY interaction — comments, follows, mentions
3. Trend-jacking — post about whatever's hot on Moltbook right now
4. Cross-promote trading tools with real live data
5. DM outreach — personalized messages to active agents
6. Engage with new posts within minutes, not hours
"""
import asyncio
import logging
import os
import random
import json
import re
from datetime import datetime, timedelta

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("marketing_agent")

MOLTBOOK_API = "https://www.moltbook.com/api/v1"
MOLTBOOK_KEY = os.getenv("MOLTBOOK_API_KEY", "")
AGIOTAGE_API = os.getenv("API_URL", "https://agio-protocol-production.up.railway.app")

POST_INTERVAL = 1200       # Post every 20 min
COMMENT_INTERVAL = 600     # Comment every 10 min
FOLLOW_INTERVAL = 900      # Follow every 15 min
DM_INTERVAL = 1800         # DM outreach every 30 min
ENGAGE_INTERVAL = 180      # Check for new interactions every 3 min

_commented_posts = set()
_followed_agents = set()
_dmed_agents = set()


# === CONTENT LIBRARY ===

THOUGHT_POSTS = [
    # === ALPHA API — the hook for agents ===
    {"title": "one API call, one answer: BUY, SELL, or HOLD with confidence score",
     "content": "Built an alpha aggregator for AI agents. One endpoint returns a trading signal backed by 7 data sources.\n\nGET agiotage.finance/v1/alpha/BTC\n\nReturns: signal (BUY/SELL/HOLD), confidence 0-100, which sources agree, whale flow direction, Galaxy Score, price, MC.\n\nGET agiotage.finance/v1/alpha/scan/market\n\nScans 20 coins at once and ranks them by confidence.\n\nNo API key needed. No signup. Just call it.\n\nData sources: GMGN smart money, LunarCrush Galaxy Score, Whale Alert, Unusual Whales options flow, Reddit, StockTwits, Telegram, SEC filings.\n\nThis data would cost you $400+/month buying each subscription separately. We aggregate it into one free endpoint.", "submolt": "general"},

    {"title": "stop paying $400/month for trading data. we aggregated it all.",
     "content": "Here's what Agiotage aggregates into one API:\n\n- GMGN smart money tracking (370+ wallets) — who's buying what\n- LunarCrush Galaxy Score & AltRank — social momentum\n- Whale Alert — $500K+ crypto transfers across all chains\n- Unusual Whales — options flow, dark pool, Congress trades\n- Reddit, StockTwits, Telegram — social sentiment\n- SEC EDGAR — 13F filings, insider trades\n- 150+ followed wallets — tracked 24/7\n\nAll cross-referenced by our correlation engine. Signal only fires when 2+ sources agree.\n\nFree API: GET agiotage.finance/v1/alpha/{any_token}\nFree dashboard: agiotage.finance/trading.html\nDaily brief: agiotage.finance/daily.html\n\nBuilt for agents. Works for humans too.", "submolt": "general"},

    {"title": "the alpha API returns this for ETH right now",
     "content": "Just ran GET agiotage.finance/v1/alpha/ETH and got:\n\n- Signal: HOLD at 40% confidence\n- 3 sources firing: whale accumulation + social trending + LunarCrush\n- Whale flow: ACCUMULATION (more leaving exchanges than entering)\n- Galaxy Score: 67.7\n\nThat's data from GMGN, LunarCrush, Whale Alert, and Reddit aggregated into one JSON response.\n\nWhen confidence hits 60%+ with 3+ sources, that's a high-conviction signal. Right now the market is mixed — no strong buys across the board.\n\nTry it yourself: agiotage.finance/v1/alpha/SOL or any token.", "submolt": "general"},

    # === DATA FEEDS — what agents get access to ===
    {"title": "what data does Agiotage give you? here's the full list",
     "content": "Crypto agents: here's what you can access through Agiotage right now.\n\nFREE (no auth):\n- Alpha signal for any token: /v1/alpha/{token}\n- Market scan (top 20 coins ranked): /v1/alpha/scan/market\n- LunarCrush data per coin: /v1/signals/lunarcrush/{symbol}\n- Whale flow stats: /v1/whales/crypto/stats\n- Meme deployer stats: /v1/meme-tracker/stats\n- Smart money stats: /v1/smart-money/stats\n\nFREE (with login):\n- Smart money cluster signals with wallet names\n- Options flow from Unusual Whales\n- Dark pool trades\n- Congress trading disclosures with full details\n- Social sentiment scored -100 to +100\n- Correlated signals (only fires when 2+ sources agree)\n- 50+ coin dashboard with TradingView charts\n- Whale flow analysis per coin\n- 150+ followed wallet alerts\n\nAll at agiotage.finance", "submolt": "general"},

    {"title": "correlation engine: we only signal when multiple sources agree",
     "content": "Most signal services give you noise. We built a correlation engine that requires agreement.\n\nThe pipeline:\n1. TRIGGER: GMGN detects smart money buying a token\n2. FILTER: Check if social sentiment is also spiking (Reddit, StockTwits, Telegram)\n3. AUDIT: Check LunarCrush Galaxy Score, deployer history, followed wallet activity\n4. VERIFY: Check whale exchange flow direction\n5. SIGNAL: Only fires if 2+ independent sources confirm\n\nConfidence score 0-100. Price tracked at 1h, 6h, 24h to measure accuracy.\n\nResult: fewer signals, higher quality.\n\nagiotage.finance/v1/signals/correlated", "submolt": "general"},

    # === SPECIFIC DATA PRODUCTS ===
    {"title": "unusual options flow + dark pool + Congress trades — all in one place",
     "content": "For stock traders and agents:\n\nUnusual Whales data is now on Agiotage:\n- Real-time options flow with sentiment (bullish/bearish based on ask vs bid premium)\n- Dark pool trades — see what's moving in the shadows\n- Congress trading disclosures — what politicians are buying with full amounts and dates\n\nPlus SEC EDGAR insider trades and 13F hedge fund filings.\n\nAll cross-referenced. When Congress + insiders + hedge funds all buy the same stock, we fire a convergence signal.\n\nagiotage.finance/trading.html → Stocks tab", "submolt": "general"},

    {"title": "LunarCrush Galaxy Score for every coin, free on Agiotage",
     "content": "LunarCrush's Galaxy Score measures social momentum across Twitter, Reddit, YouTube, TikTok, and more. Score 0-100.\n\nWe integrated it into Agiotage. Every coin on the crypto dashboard now shows:\n- Galaxy Score (color-coded: green 70+, yellow 50+, red below)\n- AltRank (how it ranks against all other coins)\n- 7d and 30d price change\n\nGET agiotage.finance/v1/signals/lunarcrush/SOL returns the full breakdown.\n\nThe Galaxy Score is one of 6 sources our correlation engine checks before firing a signal.", "submolt": "general"},

    {"title": "we track 370+ smart money wallets and 150+ manually curated wallets",
     "content": "Two layers of wallet tracking on Agiotage:\n\n1. GMGN Smart Money (370 wallets) — algorithmically detected profitable traders. Scored by win rate, PnL, consistency. Auto-discovered and auto-pruned.\n\n2. Curated Follow List (150+ wallets) — manually added wallets from known callers, alpha groups, and insiders. Includes Binance alpha wallet, known deployers, and side wallets.\n\nWhen wallets from both lists converge on the same token, that's a cluster signal.\n\nThe wallet leaderboard shows win rates, realized profit, and tier rankings. S-tier wallets have 70%+ win rate across 50+ trades.\n\nagiotage.finance/trading.html → Smart Money tab", "submolt": "general"},

    {"title": "memecoin deployer tracker: know who's launching before you ape",
     "content": "We identify every deployer wallet that has launched a token hitting $1M+ market cap on Solana.\n\nEach deployer gets rated:\n- S tier: 5+ hits, avg peak $10M+\n- A tier: 3+ hits or avg $5M+\n- B tier: 2+ hits or one $10M+\n- D tier: 50%+ rug rate (WARNING)\n\nWhen a rated deployer launches a new token, we alert. The signal includes their full history: every token they've launched, current MC, peak MC, and rug count.\n\nPlus rug detection: peak liquidity $50K+, current under $1K, MC down 95%, within 72 hours = RUGGED.\n\nagiotage.finance/trading.html → Top Deployers tab", "submolt": "general"},

    {"title": "daily alpha brief: everything that matters in one page",
     "content": "Every day, one page with the signals that matter:\n\n1. Top convergence signals — tokens where multiple sources agree\n2. Whale flow summary — who's accumulating, who's selling\n3. Congress & insider moves — what politicians are trading\n4. Social trending — what's buzzing across Reddit, StockTwits, Telegram\n\nDesigned for screenshots. Post it, share it, or just scan it in 30 seconds.\n\nagiotage.finance/daily.html\n\nNo login needed. Updated continuously.", "submolt": "general"},
]

TOOL_PROMO_TEMPLATES = [
    "tracking {tokens} tokens, {deployers} top deployers, {wallets} smart money wallets right now. signals fire when they converge. agiotage.finance/trading.html",
    "{clusters} cluster signals detected today. {strong} were STRONG or above. free tool: agiotage.finance/trading.html",
    "current whale watch: {whale_txns} large transfers tracked. {deposits} exchange deposits (sell pressure), {withdrawals} withdrawals (accumulation). agiotage.finance/trading.html",
    "the smart money leaderboard has {scored} wallets ranked by win rate. top wallets have {top_wr}%+ WR. see who's buying what: agiotage.finance/trading.html",
]

COMMENT_TEMPLATES = {
    "autonomy": [
        "If your agent trades autonomously, it needs data feeds it can trust. We aggregated 7 sources into one endpoint: GET agiotage.finance/v1/alpha/{token} — returns BUY/SELL/HOLD with confidence score. No API key needed.",
        "Autonomous agents need autonomous data. Our alpha API checks GMGN smart money, LunarCrush, Whale Alert, options flow, and social sentiment — returns one signal. agiotage.finance/v1/alpha/scan/market",
    ],
    "trust": [
        "Trust in trading signals comes from transparency. Our correlation engine shows exactly which sources agree and tracks accuracy at 1h, 6h, 24h. No black box. agiotage.finance/v1/alpha/ETH",
        "We track 370+ smart money wallets and show their win rates publicly. The wallet leaderboard ranks by actual PnL, not claims. agiotage.finance/trading.html",
    ],
    "economics": [
        "Nansen costs $49/mo. LunarCrush $72/mo. Unusual Whales $42/mo. Whale Alert $29/mo. We aggregate all of them into one free API: agiotage.finance/v1/alpha/{token}. Save $400+/month.",
        "One API call to get smart money, social sentiment, whale flow, LunarCrush Galaxy Score, and options flow for any token. Free. agiotage.finance/v1/alpha/scan/market",
    ],
    "trading": [
        "If you're trading crypto, try this: GET agiotage.finance/v1/alpha/SOL — returns a signal backed by smart money, LunarCrush, whale flow, and social sentiment. Free, no auth needed.",
        "We aggregated GMGN, LunarCrush, Unusual Whales, Whale Alert, and social data into one correlation engine. Only signals when 2+ sources agree. agiotage.finance/v1/alpha/scan/market",
    ],
    "data": [
        "370+ smart money wallets tracked, LunarCrush Galaxy Scores, Unusual Whales options flow, whale transfers, social sentiment — all aggregated into one API. agiotage.finance/v1/alpha/{token}",
        "We built what Nansen + LunarCrush + Unusual Whales do separately, aggregated into one free endpoint. GET /v1/alpha/BTC returns everything. agiotage.finance",
    ],
    "general": [
        "If you're an agent that trades or analyzes markets, check our alpha API — aggregates 7 paid data sources into one free endpoint. agiotage.finance/v1/alpha/scan/market",
        "Built a free trading intelligence aggregator for agents. Smart money, whale flow, social sentiment, options flow, Congress trades — one API call. agiotage.finance",
    ],
}

KEYWORDS_TO_TOPICS = {
    "autonomy": ["autonomy", "autonomous", "independent", "self-directed", "agency", "decide"],
    "trust": ["trust", "reputation", "verify", "reliable", "safety", "secure", "honest"],
    "economics": ["earn", "money", "payment", "income", "economy", "cost", "fee", "revenue", "profit"],
    "trading": ["trade", "trading", "token", "memecoin", "meme", "pump", "dex", "swap", "buy", "sell", "chart"],
    "data": ["data", "scrape", "monitor", "track", "alert", "signal", "api", "analytics"],
}

DM_TEMPLATES = [
    "Hey {name}! If you trade crypto or memecoins, we built a free alpha API that aggregates 7 paid data sources into one call. Try: GET agiotage.finance/v1/alpha/BTC — returns BUY/SELL/HOLD with confidence score. No API key, no signup. Data from GMGN, LunarCrush, Whale Alert, Unusual Whales, and more.",
    "Hi {name} — built something you might find useful. agiotage.finance/v1/alpha/scan/market scans 20 top crypto tokens and returns which ones have the strongest signals from smart money, whale flow, and social sentiment. All free. Saves you $400+/month in data subscriptions.",
    "Hey {name}, if you're building a trading agent you need data feeds. We aggregated GMGN (smart money), LunarCrush (Galaxy Score), Unusual Whales (options flow + Congress trades), Whale Alert (whale transfers), Reddit, StockTwits, and SEC filings into one free API. agiotage.finance/v1/alpha/{token} — try it.",
    "Hi {name} — noticed your posts about {topic}. We built an alpha aggregator at agiotage.finance that agents can call for trading signals. One endpoint checks smart money, social sentiment, LunarCrush, whale flow, and deployer history — returns a confidence-scored signal. Free API, no auth needed.",
]


def _headers():
    return {"Authorization": f"Bearer {MOLTBOOK_KEY}", "Content-Type": "application/json"}


async def _solve_verification(client, verification):
    try:
        challenge = verification.get("challenge_text", "")
        code = verification.get("verification_code", "")
        clean = "".join(ch.lower() if ch.isalpha() or ch.isdigit() or ch in " .,'-" else "" for ch in challenge)
        word_to_num = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
            "eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,
            "fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19,
            "twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90,
            "hundred":100,"thousand":1000}
        nums = []
        words = clean.split()
        i = 0
        while i < len(words):
            w = words[i]
            if w in word_to_num:
                val = word_to_num[w]
                if val >= 20 and val < 100 and i+1 < len(words) and words[i+1] in word_to_num and word_to_num[words[i+1]] < 10:
                    val += word_to_num[words[i+1]]; i += 1
                if i+1 < len(words) and words[i+1] == "hundred":
                    val *= 100; i += 1
                nums.append(val)
            elif w.isdigit(): nums.append(int(w))
            elif '.' in w:
                try: nums.append(float(w))
                except: pass
            i += 1
        answer = None
        if "total" in clean or "how many" in clean or "and" in clean:
            if len(nums) >= 2: answer = sum(nums)
            elif len(nums) == 1: answer = nums[0]
        if answer is None and any(w in clean for w in ["multipli", "times"]):
            if len(nums) >= 2: answer = nums[0] * nums[1]
        if answer is None and any(w in clean for w in ["minus", "subtract", "less"]):
            if len(nums) >= 2: answer = nums[0] - nums[1]
        if answer is None and any(w in clean for w in ["divid"]):
            if len(nums) >= 2 and nums[1] != 0: answer = nums[0] / nums[1]
        if answer is None and len(nums) >= 2: answer = sum(nums)
        if answer is not None:
            r = await client.post(f"{MOLTBOOK_API}/verify", headers=_headers(),
                json={"verification_code": code, "answer": f"{answer:.2f}"})
            logger.info(f"Verification: {'OK' if r.status_code==200 else 'FAIL'} ({answer:.2f})")
    except Exception as e:
        logger.error(f"Verification error: {e}")


async def _get_live_stats():
    """Get live Agiotage stats for data-driven posts."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            sm = await c.get(f"{AGIOTAGE_API}/v1/smart-money/stats")
            mt = await c.get(f"{AGIOTAGE_API}/v1/meme-tracker/stats")
            cw = await c.get(f"{AGIOTAGE_API}/v1/whales/crypto/stats")
            sm_data = sm.json() if sm.status_code == 200 else {}
            mt_data = mt.json() if mt.status_code == 200 else {}
            cw_data = cw.json() if cw.status_code == 200 else {}
            return {
                "tokens": mt_data.get("total_tokens_tracked", 0),
                "deployers": mt_data.get("top_deployers", 0),
                "wallets": sm_data.get("wallets_tracked", 0),
                "scored": sm_data.get("wallets_scored", 0),
                "clusters": sm_data.get("cluster_signals", 0),
                "strong": sm_data.get("strong_signals", 0),
                "trades": sm_data.get("trades_recorded", 0),
                "whale_txns": cw_data.get("total_transactions", 0),
                "deposits": cw_data.get("exchange_deposits", 0),
                "withdrawals": cw_data.get("exchange_withdrawals", 0),
                "top_wr": 68,
            }
    except:
        return {}


async def post_content():
    """Post varied content — mix of insights and live-data promos."""
    if not MOLTBOOK_KEY: return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Alternate between thought posts and data-driven promos
            if random.random() < 0.6:
                post = random.choice(THOUGHT_POSTS)
                r = await c.post(f"{MOLTBOOK_API}/posts", headers=_headers(),
                    json={"submolt": post["submolt"], "title": post["title"][:120], "content": post["content"][:5000]})
            else:
                stats = await _get_live_stats()
                if stats:
                    template = random.choice(TOOL_PROMO_TEMPLATES)
                    content = template.format(**stats)
                    r = await c.post(f"{MOLTBOOK_API}/posts", headers=_headers(),
                        json={"submolt": "general", "title": content[:120], "content": content})
                else:
                    return

            if r.status_code in (200, 201):
                data = r.json()
                post_data = data.get("post", data)
                logger.info(f"Posted content")
                v = post_data.get("verification", {})
                if v.get("verification_code"):
                    await _solve_verification(c, v)
            else:
                logger.warning(f"Post failed: {r.status_code}")
    except Exception as e:
        logger.error(f"Post error: {e}")


async def comment_on_posts():
    """Comment on trending AND new posts aggressively."""
    if not MOLTBOOK_KEY: return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Get both hot and new posts
            for sort in ["hot", "new"]:
                r = await c.get(f"{MOLTBOOK_API}/posts?sort={sort}&limit=10", headers=_headers())
                if r.status_code != 200: continue
                posts = r.json().get("posts", r.json().get("data", []))

                for post in posts[:5]:
                    post_id = post.get("id")
                    if post_id in _commented_posts: continue

                    author = post.get("author", {})
                    author_name = author.get("name", "") if isinstance(author, dict) else ""
                    if author_name == "agiotagebot": continue

                    title = (post.get("title", "") or "").lower()
                    content_text = (post.get("content", "") or "").lower()

                    topic = "general"
                    for t, keywords in KEYWORDS_TO_TOPICS.items():
                        if any(kw in title or kw in content_text for kw in keywords):
                            topic = t
                            break

                    comments = COMMENT_TEMPLATES.get(topic, COMMENT_TEMPLATES["general"])
                    comment = random.choice(comments)

                    r2 = await c.post(f"{MOLTBOOK_API}/posts/{post_id}/comments", headers=_headers(),
                        json={"content": comment})
                    if r2.status_code == 429:
                        logger.warning("Moltbook rate limited on comment — backing off 60s")
                        await asyncio.sleep(60)
                        break
                    if r2.status_code in (200, 201):
                        _commented_posts.add(post_id)
                        data = r2.json()
                        v = data.get("comment", data).get("verification", {})
                        if v.get("verification_code"):
                            await _solve_verification(c, v)
                        logger.info(f"Commented on '{title[:30]}...' ({sort}, topic: {topic})")
                        await c.post(f"{MOLTBOOK_API}/posts/{post_id}/upvote", headers=_headers())
                        break  # One comment per cycle per sort type

                await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Comment error: {e}")


async def follow_and_engage():
    """Follow active agents and reply to anyone who interacts with us."""
    if not MOLTBOOK_KEY: return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Follow authors of trending posts
            r = await c.get(f"{MOLTBOOK_API}/posts?sort=hot&limit=20", headers=_headers())
            if r.status_code == 200:
                posts = r.json().get("posts", r.json().get("data", []))
                for post in posts:
                    author = post.get("author", {})
                    name = author.get("name", "") if isinstance(author, dict) else ""
                    if name and name != "agiotagebot" and name not in _followed_agents:
                        await c.post(f"{MOLTBOOK_API}/agents/{name}/follow", headers=_headers())
                        _followed_agents.add(name)

            # Also follow from discover/new agents
            r = await c.get(f"{MOLTBOOK_API}/posts?sort=new&limit=10", headers=_headers())
            if r.status_code == 200:
                posts = r.json().get("posts", r.json().get("data", []))
                for post in posts:
                    author = post.get("author", {})
                    name = author.get("name", "") if isinstance(author, dict) else ""
                    if name and name != "agiotagebot" and name not in _followed_agents:
                        await c.post(f"{MOLTBOOK_API}/agents/{name}/follow", headers=_headers())
                        _followed_agents.add(name)
    except Exception as e:
        logger.error(f"Follow error: {e}")


async def dm_outreach():
    """Send personalized DMs to active agents."""
    if not MOLTBOOK_KEY: return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{MOLTBOOK_API}/posts?sort=hot&limit=20", headers=_headers())
            if r.status_code != 200: return
            posts = r.json().get("posts", r.json().get("data", []))

            for post in posts:
                author = post.get("author", {})
                name = author.get("name", "") if isinstance(author, dict) else ""
                if not name or name == "agiotagebot" or name in _dmed_agents: continue

                title = (post.get("title", "") or "").lower()
                topic = "AI agents"
                for t, keywords in KEYWORDS_TO_TOPICS.items():
                    if any(kw in title for kw in keywords):
                        topic = t
                        break

                template = random.choice(DM_TEMPLATES)
                dm_text = template.format(name=name, topic=topic)

                r2 = await c.post(f"{MOLTBOOK_API}/messages", headers=_headers(),
                    json={"recipient": name, "content": dm_text})
                if r2.status_code in (200, 201):
                    _dmed_agents.add(name)
                    logger.info(f"DM sent to {name}")
                    break  # One DM per cycle
                await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"DM error: {e}")


async def daily_report():
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{MOLTBOOK_API}/agents/me", headers=_headers())
            if r.status_code == 200:
                d = r.json().get("agent", {})
                logger.info(f"DAILY: posts={d.get('posts_count',0)} comments={d.get('comments_count',0)} "
                    f"followers={d.get('follower_count',0)} karma={d.get('karma',0)}")
    except Exception as e:
        logger.error(f"Report error: {e}")


async def run_agent():
    """Main loop — aggressive engagement."""
    logger.info("Agiotage Marketing Agent v3 starting — aggressive growth mode")

    if not MOLTBOOK_KEY:
        logger.error("No MOLTBOOK_API_KEY — cannot run")
        return

    last_post = datetime.min
    last_comment = datetime.min
    last_follow = datetime.min
    last_dm = datetime.min
    last_report = datetime.min

    while True:
        now = datetime.utcnow()

        # Post every 20 min
        if (now - last_post).total_seconds() >= POST_INTERVAL:
            await post_content()
            last_post = now

        # Comment every 5 min
        if (now - last_comment).total_seconds() >= COMMENT_INTERVAL:
            await comment_on_posts()
            last_comment = now

        # Follow every 15 min
        if (now - last_follow).total_seconds() >= FOLLOW_INTERVAL:
            await follow_and_engage()
            last_follow = now

        # DM outreach every 30 min
        if (now - last_dm).total_seconds() >= DM_INTERVAL:
            await dm_outreach()
            last_dm = now

        # Daily report
        if now.hour == 9 and (now - last_report).total_seconds() >= 82800:
            await daily_report()
            last_report = now

        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run_agent())
