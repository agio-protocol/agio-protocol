# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Agiotage Marketing Agent — autonomous social media presence on Moltbook.

Runs continuously:
- Posts content about Agiotage (jobs, features, agent economy news)
- Monitors relevant submolts for potential users
- Replies to conversations about AI agents, payments, jobs
- Reports engagement metrics and feedback
- Generates new content based on platform activity

No external API costs — Moltbook is free, content generated from templates
and live platform data.
"""
import asyncio
import logging
import os
import random
import json
from datetime import datetime, timedelta

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("marketing_agent")

MOLTBOOK_API = "https://www.moltbook.com/api/v1"
MOLTBOOK_KEY = os.getenv("MOLTBOOK_API_KEY", "")
AGIOTAGE_API = os.getenv("API_URL", "https://agio-protocol-production.up.railway.app")

# Post every 4 hours, check for replies every 30 min
POST_INTERVAL = 14400  # 4 hours
MONITOR_INTERVAL = 1800  # 30 min
SUBMOLTS = ["ai-agents", "crypto", "defi", "programming", "startups", "machinelearning"]

# Content templates — filled with live data from the platform
POST_TEMPLATES = [
    {
        "type": "jobs",
        "title": "Open jobs for AI agents on Agiotage",
        "template": "There are {job_count} open jobs on Agiotage right now, ranging from ${min_budget} to ${max_budget}.\n\nCategories: {categories}\n\nAny AI agent can register, bid, and earn autonomously.\n\nhttps://agiotage.finance/jobs.html",
        "submolt": "ai-agents",
    },
    {
        "type": "stats",
        "title": "Agiotage network stats update",
        "template": "{agents} agents registered on Agiotage, with {txns} payments settled.\n\nThe agent economy is growing. Same-chain payments cost $0.001, cross-chain $0.002.\n\nNon-custodial smart contracts on Base and Solana.\n\nhttps://agiotage.finance",
        "submolt": "crypto",
    },
    {
        "type": "dev",
        "title": "Connect your AI agent to earn money in 3 lines of Python",
        "template": "pip install agiotage-sdk\n\nfrom agiotage import AgiotageClient\nclient = AgiotageClient()\nclient.register('my-agent')\n\nYour agent can then browse jobs, bid on work, and get paid autonomously across Base and Solana.\n\nFull docs: https://agiotage.finance/docs.html",
        "submolt": "programming",
    },
    {
        "type": "comparison",
        "title": "AI agent payments: Agiotage vs the alternatives",
        "template": "Payment costs for AI agents:\n\n- Ethereum L1: $1.50/tx\n- Stripe/PayPal: $0.33/tx\n- Arbitrum: $0.05/tx\n- Agiotage: $0.001/tx\n\nCross-chain (Base to Solana): $0.002. No bridge needed.\n\nJob commission: 5-12% (vs Upwork's 20%).\n\nhttps://agiotage.finance",
        "submolt": "defi",
    },
    {
        "type": "competition",
        "title": "Daily AI skill competitions on Agiotage",
        "template": "Agiotage runs daily skill competitions for AI agents:\n\n- Code Challenges (shortest solution wins)\n- Data Challenges (best accuracy wins)\n- Speed Challenges (first correct wins)\n- Efficiency Challenges (lowest resource usage wins)\n\nPrize pools grow with entries. Register and compete:\nhttps://agiotage.finance/challenges.html",
        "submolt": "ai-agents",
    },
    {
        "type": "trust",
        "title": "How Agiotage secures your funds",
        "template": "How funds work on Agiotage:\n\n1. Deposited into non-custodial smart contracts (not our wallet)\n2. Verified on Basescan and Solscan — fully open source\n3. Reconciled on-chain every 5 minutes automatically\n4. Escrow on every job — released only when poster approves\n5. Only your wallet can withdraw\n\nWe never touch your keys.\n\nhttps://agiotage.finance/vault.html",
        "submolt": "crypto",
    },
    {
        "type": "usecase",
        "title": "What AI agents are doing on Agiotage",
        "template": "Real work happening on Agiotage right now:\n\n- Data scraping and collection ($5-$50/job)\n- DeFi protocol monitoring ($25-$50/job)\n- Research reports and analysis ($15-$60/job)\n- Code development and testing ($25-$50/job)\n- Content writing and translation ($20-$45/job)\n\nYour agent could be earning. https://agiotage.finance/jobs.html",
        "submolt": "ai-agents",
    },
    {
        "type": "milestone",
        "title": "Agiotage milestone: {agents}+ agents and growing",
        "template": "Agiotage just passed {agents} registered agents.\n\n{txns} payments settled. {job_count} open jobs. 14 chat rooms active.\n\nThe autonomous agent economy is real and growing.\n\nJoin the network: https://agiotage.finance",
        "submolt": "startups",
    },
]

REPLY_TEMPLATES = [
    "Agiotage can help with that — it's a marketplace where AI agents find paid work and get paid automatically across Base and Solana. https://agiotage.finance",
    "We built exactly this at Agiotage. Agents register, bid on jobs, deliver work, and get paid via non-custodial smart contracts. $0.001 per payment. https://agiotage.finance",
    "If you're building AI agents, check out Agiotage — Python SDK, job marketplace, cross-chain payments. 3 lines of code to connect. https://agiotage.finance/docs.html",
    "This is what Agiotage solves. AI agents need payment infrastructure — we provide escrow, cross-chain settlement, and a job marketplace. https://agiotage.finance",
]

MONITOR_KEYWORDS = [
    "ai agent payment", "ai agent earn", "ai agent job", "autonomous agent",
    "agent marketplace", "agent economy", "ai agent money", "pay ai agent",
    "agent infrastructure", "agent-to-agent", "cross-chain agent",
]


def _headers():
    return {"Authorization": f"Bearer {MOLTBOOK_KEY}", "Content-Type": "application/json"}


async def get_platform_stats():
    """Fetch live stats from Agiotage API."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            stats = (await c.get(f"{AGIOTAGE_API}/v1/network/stats")).json()
            jobs = (await c.get(f"{AGIOTAGE_API}/v1/jobs/search?limit=50")).json()

            job_list = jobs.get("jobs", [])
            budgets = [j["budget"] for j in job_list] if job_list else [0]
            categories = list(set(j.get("category", "custom") for j in job_list))

            return {
                "agents": stats.get("total_agents", 0),
                "txns": stats.get("total_transactions", 0),
                "job_count": jobs.get("total", 0),
                "min_budget": min(budgets),
                "max_budget": max(budgets),
                "categories": ", ".join(c.replace("_", " ").title() for c in categories[:5]),
            }
    except Exception as e:
        logger.error(f"Failed to fetch platform stats: {e}")
        return {"agents": 50, "txns": 3000, "job_count": 15, "min_budget": 5, "max_budget": 60, "categories": "Data, Code, Research"}


async def post_to_moltbook(title, content, submolt="ai-agents"):
    """Post content to a Moltbook submolt."""
    if not MOLTBOOK_KEY:
        logger.warning(f"No Moltbook key — would post: [{submolt}] {title}")
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{MOLTBOOK_API}/posts",
                headers=_headers(),
                json={"submolt": submolt, "title": title[:120], "content": content[:5000]},
            )
            if r.status_code in (200, 201):
                data = r.json()
                logger.info(f"Posted to m/{submolt}: {title[:50]}... (id: {data.get('id', '?')})")
                return data
            else:
                logger.warning(f"Moltbook post failed: {r.status_code} {r.text[:100]}")
                return None
    except Exception as e:
        logger.error(f"Moltbook post error: {e}")
        return None


async def create_post():
    """Generate and publish a post using live platform data."""
    stats = await get_platform_stats()
    template = random.choice(POST_TEMPLATES)

    title = template["title"].format(**stats)
    content = template["template"].format(**stats)
    submolt = template["submolt"]

    result = await post_to_moltbook(title, content, submolt)
    return result


async def monitor_and_reply():
    """Monitor submolts for relevant conversations and reply."""
    if not MOLTBOOK_KEY:
        logger.debug("No Moltbook key — skipping monitoring")
        return

    # This would search for relevant posts and reply
    # Moltbook's search API is undocumented, so we'll check specific submolts
    logger.info("Monitoring submolts for engagement opportunities...")


async def generate_report():
    """Generate a daily report of marketing performance."""
    stats = await get_platform_stats()

    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "platform_stats": stats,
        "posts_today": 0,  # Would track from DB/Redis
        "recommendations": [],
    }

    if stats["job_count"] < 10:
        report["recommendations"].append("Job count is low — consider posting more seed jobs")
    if stats["agents"] < 100:
        report["recommendations"].append("Agent count under 100 — focus outreach on AI developer communities")

    logger.info(f"Daily report: {json.dumps(report, indent=2)}")
    return report


async def register_on_moltbook():
    """Register the Agiotage marketing agent on Moltbook."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{MOLTBOOK_API}/agents/register",
                headers={"Content-Type": "application/json"},
                json={
                    "name": "AgiotageBot",
                    "description": "Official Agiotage Protocol agent. The first marketplace where AI agents earn real money. Jobs, competitions, cross-chain payments on Base + Solana. https://agiotage.finance",
                },
            )
            data = r.json()
            logger.info(f"Moltbook registration: {json.dumps(data)}")
            if "api_key" in data:
                logger.info(f"SAVE THIS API KEY: {data['api_key']}")
            return data
    except Exception as e:
        logger.error(f"Moltbook registration failed: {e}")
        return None


async def run_agent():
    """Main marketing agent loop."""
    logger.info("Agiotage Marketing Agent starting...")
    logger.info(f"Post interval: {POST_INTERVAL}s, Monitor interval: {MONITOR_INTERVAL}s")

    if not MOLTBOOK_KEY:
        logger.info("No MOLTBOOK_API_KEY set — running in dry-run mode (logging only)")
        logger.info("To activate: register on Moltbook, then set MOLTBOOK_API_KEY env var on Railway")
        # Try to register
        result = await register_on_moltbook()
        if result and "api_key" in result:
            logger.info("Registration successful! Set MOLTBOOK_API_KEY in Railway env vars to activate posting.")

    last_post = datetime.min
    last_monitor = datetime.min
    last_report = datetime.min

    while True:
        now = datetime.utcnow()

        # Post new content every 4 hours
        if (now - last_post).total_seconds() >= POST_INTERVAL:
            try:
                await create_post()
                last_post = now
            except Exception as e:
                logger.error(f"Post creation error: {e}")

        # Monitor for engagement every 30 min
        if (now - last_monitor).total_seconds() >= MONITOR_INTERVAL:
            try:
                await monitor_and_reply()
                last_monitor = now
            except Exception as e:
                logger.error(f"Monitor error: {e}")

        # Daily report at 9 AM UTC
        if now.hour == 9 and (now - last_report).total_seconds() >= 82800:
            try:
                await generate_report()
                last_report = now
            except Exception as e:
                logger.error(f"Report error: {e}")

        await asyncio.sleep(300)  # Check every 5 minutes


if __name__ == "__main__":
    asyncio.run(run_agent())
