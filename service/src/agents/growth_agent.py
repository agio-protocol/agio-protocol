"""
Growth Agent — Autonomously discovers, qualifies, and pitches prospects.

Uses template-based outreach (no Claude API needed). Monitors Base
for high-frequency x402 users and calculates exact savings.

The meta-beauty: this agent uses AGIO to pay for its own infrastructure.
AGIO is both the product being sold and the payment method the salesman uses.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from dataclasses import dataclass

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..services.savings_service import estimate_savings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("growth_agent")


# =========================================================================
# Outreach Templates (10 templates, no AI needed)
# =========================================================================
TEMPLATES = {
    "high_frequency_x402": """Hey! I noticed your agent wallet ({wallet_short}) is making {daily_txns}+ micropayments per day on Base via x402.

Quick math: at your volume, you're paying ${current_monthly:.2f}/month in gas. With AGIO's batch settlement, that drops to ${agio_monthly:.2f}/month — {savings_pct:.0f}% savings.

How it works: instead of 1 on-chain transaction per payment, AGIO batches hundreds into a single settlement. Same USDC, same Base chain, just cheaper.

3 lines to integrate:
  from agio import AgioClient
  client = AgioClient(agent_name="your-agent")
  receipt = await client.pay(to="agio:base:0x...", amount=0.001)

Live testnet demo: agiotage.finance
GitHub: github.com/agio-protocol

Happy to help with integration if you're interested.""",

    "multi_chain": """Your agent is transacting on {chains} — that usually means bridging fees and 20-minute waits between chains.

AGIO does cross-chain micropayments in <1 second with zero bridging fees. Your agent calls one function, AGIO routes across chains automatically using Circle CCTP.

At your volume ({daily_txns} txns/day), estimated monthly savings: ${monthly_savings:.2f}.

  receipt = await client.pay(to="agio:sol:0x...", amount=0.001)
  # Pays a Solana agent from your Base wallet. Instant.

More at agiotage.finance""",

    "github_dev": """Saw your repo {repo_name} — looks like you're building agent payment logic from scratch. AGIO handles this in 3 lines:

  from agio import AgioClient
  client = AgioClient(agent_name="{repo_name}")
  receipt = await client.pay(to="agio:base:0x...", amount=0.001)

Features you get for free:
- Batched settlement (100x cheaper than individual transfers)
- Cross-chain payments (Base, Solana, Polygon)
- Agent reputation scores
- Sub-cent micropayments

SDK: pip install agio-sdk
Docs: github.com/agio-protocol/agio-contracts""",

    "skyfire_user": """Currently paying Skyfire's 2-3% infrastructure fee? On {daily_txns} transactions/day at ${avg_amount:.3f} avg, that's ~${current_monthly:.2f}/month in fees.

AGIO charges $0.0001 flat per micropayment (no percentage fee). Your monthly cost drops to ${agio_monthly:.2f} — saving ${monthly_savings:.2f}/month.

Same USDC, same chains, just routed through AGIO's batch settlement instead.

agiotage.finance""",

    "cost_conscious": """Quick savings calculation for your agent ({wallet_short}):

Current ({current_protocol}): ${current_monthly:.2f}/month
With AGIO:                     ${agio_monthly:.2f}/month
You save:                      ${monthly_savings:.2f}/month ({savings_pct:.0f}%)

AGIO batches micropayments — instead of paying gas per transaction, you share it across hundreds of payments in each batch.

Personal Agent Plan: $0.50/month flat for up to 10,000 transactions.

agiotage.finance""",

    "new_agent_dev": """Building an AI agent that needs to pay for things? AGIO makes it dead simple:

  pip install agio-sdk

  from agio import AgioClient
  client = AgioClient(agent_name="my-agent")

  # Pay another agent
  await client.pay(to="agio:base:0x...", amount=0.001)

  # Check balance
  balance = await client.balance()

Works across Base, Solana, and Polygon. Sub-cent payments for $0.0001 each.

agiotage.finance | github.com/agio-protocol""",

    "x402_upgrade": """x402 is great for individual payments. AGIO sits underneath x402 as the settlement layer — same standard, but batched.

Instead of 1 on-chain transaction per x402 payment, AGIO collects them and settles in batches. Your gas cost drops from ~$0.00001/txn to ~$0.000004/txn.

At {daily_txns} transactions/day, that's ${monthly_savings:.2f}/month in savings.

AGIO doesn't replace x402 — it makes it cheaper.

agiotage.finance""",

    "high_volume": """Your agent is doing {daily_txns}+ transactions per day. At that volume, individual settlement is leaving money on the table.

AGIO's batch settlement puts 100-500 payments in a single on-chain transaction. Gas cost per payment: $0.0004.

Personal Agent Plan: $0.50/month flat for up to 10,000 micropayments. No gas surprises.

pip install agio-sdk | agiotage.finance""",

    "defi_agent": """Saw your agent interacting with DeFi protocols on {chains}. If it's making frequent small payments (API calls, data feeds, compute), AGIO can cut those costs by {savings_pct:.0f}%.

Cross-chain support means your agent can pay services on any chain from a single wallet. No bridging, no swaps.

agiotage.finance""",

    "developer_pain": """Struggling with nonce conflicts when your agents make concurrent payments? AGIO handles this — payments queue off-chain and settle in batches. No nonce management needed.

Also: sub-cent payments, cross-chain routing, agent reputation scores.

  from agio import AgioClient
  client = AgioClient(agent_name="my-agent")
  await client.pay(to="...", amount=0.001)

github.com/agio-protocol/agio-sdk""",
}


@dataclass
class Prospect:
    source: str          # "x402_scan", "github", "manual"
    wallet: str
    daily_txns: int
    avg_amount: float
    current_protocol: str
    chains: list[str]
    savings_estimate: float
    template_key: str
    contacted: bool = False
    contact_date: str | None = None


def select_template(prospect: Prospect) -> str:
    """Pick the best outreach template for a prospect."""
    if prospect.daily_txns > 200:
        return "high_volume"
    if len(prospect.chains) > 1:
        return "multi_chain"
    if prospect.current_protocol == "skyfire":
        return "skyfire_user"
    if prospect.current_protocol == "x402":
        return "x402_upgrade"
    return "cost_conscious"


def render_template(template_key: str, prospect: Prospect) -> str:
    """Render an outreach template with prospect data."""
    template = TEMPLATES.get(template_key, TEMPLATES["cost_conscious"])

    savings = estimate_savings(
        prospect.current_protocol, prospect.daily_txns,
        prospect.avg_amount, prospect.chains
    )

    return template.format(
        wallet_short=prospect.wallet[:8] + "..." + prospect.wallet[-4:],
        daily_txns=prospect.daily_txns,
        current_monthly=savings.current_monthly_cost,
        agio_monthly=savings.agio_monthly_cost,
        monthly_savings=savings.monthly_savings,
        savings_pct=savings.savings_percentage,
        current_protocol=savings.current_protocol,
        avg_amount=prospect.avg_amount,
        chains=", ".join(prospect.chains),
        repo_name="agent-project",
    )


async def scan_for_prospects() -> list[Prospect]:
    """
    Discover potential AGIO users.
    In production: monitors Base mempool for x402 transactions.
    For now: returns simulated prospects for testing the pipeline.
    """
    # Simulated prospects for pipeline testing
    return [
        Prospect(
            source="x402_scan",
            wallet="0x742d35Cc6634C0532925a3b844Bc1e3DE32b8049",
            daily_txns=150,
            avg_amount=0.005,
            current_protocol="x402",
            chains=["base"],
            savings_estimate=0,
            template_key="",
        ),
        Prospect(
            source="x402_scan",
            wallet="0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            daily_txns=500,
            avg_amount=0.002,
            current_protocol="x402",
            chains=["base", "solana"],
            savings_estimate=0,
            template_key="",
        ),
    ]


async def run_growth_cycle():
    """Run one growth agent cycle: discover → qualify → generate outreach."""
    prospects = await scan_for_prospects()
    logger.info(f"Discovered {len(prospects)} prospects")

    results = []
    for prospect in prospects:
        template_key = select_template(prospect)
        prospect.template_key = template_key

        savings = estimate_savings(
            prospect.current_protocol, prospect.daily_txns,
            prospect.avg_amount, prospect.chains
        )
        prospect.savings_estimate = savings.monthly_savings

        message = render_template(template_key, prospect)

        results.append({
            "wallet": prospect.wallet[:12] + "...",
            "daily_txns": prospect.daily_txns,
            "template": template_key,
            "monthly_savings": f"${savings.monthly_savings:.2f}",
            "message_preview": message[:100] + "...",
        })

        logger.info(
            f"Prospect {prospect.wallet[:10]}...: {prospect.daily_txns} txns/day, "
            f"saves ${savings.monthly_savings:.2f}/mo, template={template_key}"
        )

    return results


async def run_agent():
    """Main growth agent loop."""
    logger.info("Growth agent started")
    while True:
        try:
            await run_growth_cycle()
        except Exception as e:
            logger.error(f"Growth cycle error: {e}", exc_info=True)
        await asyncio.sleep(3600)  # hourly cycles


if __name__ == "__main__":
    asyncio.run(run_agent())
