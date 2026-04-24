# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Savings Calculator — Shows agents exactly how much they'd save with AGIO.

Hard numbers sell better than marketing copy. This is what the growth agent
uses to pitch prospects, and what the /v1/estimate/savings endpoint returns.

Fee models for competing protocols (verified April 2026):
- x402 on Base: gas only (~$0.00001/txn) but Base-only, no batching
- Skyfire: $0.00002/txn + 2-3% infrastructure markup
- Visa Trusted Agent Protocol: $0.30 minimum per transaction
- Ethereum L1 direct: $0.50-5.00 gas per transaction
- Solana direct: $0.00025 per transaction
- AGIO: $0.0001/txn flat (micropayments) or 0.05% (larger), batched
- AGIO Personal Plan: $0.50/month for up to 10,000 txns under $0.01
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Protocol cost models (USD per transaction)
PROTOCOL_COSTS = {
    "x402": {
        "name": "x402 (Base)",
        "per_txn_gas": 0.00001,
        "per_txn_fee": 0.0,
        "pct_fee": 0.0,
        "chains": ["base"],
        "cross_chain": False,
        "batching": False,
        "notes": "Base-only, no batching, no cross-chain",
    },
    "skyfire": {
        "name": "Skyfire",
        "per_txn_gas": 0.00002,
        "per_txn_fee": 0.0,
        "pct_fee": 0.025,  # 2.5% infrastructure markup
        "chains": ["base", "ethereum"],
        "cross_chain": False,
        "batching": False,
        "notes": "2-3% infrastructure fee on top of gas",
    },
    "visa_tap": {
        "name": "Visa Trusted Agent Protocol",
        "per_txn_gas": 0.0,
        "per_txn_fee": 0.30,  # minimum processing fee
        "pct_fee": 0.025,     # 2.5% card processing
        "chains": ["fiat"],
        "cross_chain": False,
        "batching": False,
        "notes": "$0.30 minimum makes micropayments impossible",
    },
    "ethereum_l1": {
        "name": "Ethereum L1 Direct",
        "per_txn_gas": 2.00,
        "per_txn_fee": 0.0,
        "pct_fee": 0.0,
        "chains": ["ethereum"],
        "cross_chain": False,
        "batching": False,
        "notes": "Unusable for micropayments",
    },
    "solana_direct": {
        "name": "Solana Direct",
        "per_txn_gas": 0.00025,
        "per_txn_fee": 0.0,
        "pct_fee": 0.0,
        "chains": ["solana"],
        "cross_chain": False,
        "batching": False,
        "notes": "Cheap but Solana-only",
    },
    "raw_transfer": {
        "name": "Raw ERC-20 Transfer",
        "per_txn_gas": 0.001,  # average across L2s
        "per_txn_fee": 0.0,
        "pct_fee": 0.0,
        "chains": ["base", "polygon", "arbitrum"],
        "cross_chain": False,
        "batching": False,
        "notes": "Individual on-chain transfers",
    },
}

AGIO_COSTS = {
    "per_txn": {
        "micro_flat": 0.00002,     # flat fee for micropayments (competitive with x402 gas)
        "standard_pct": 0.0005,    # 0.05% for payments >= $0.01
        "cross_chain_flat": 0.0001,  # additional cross-chain routing fee
    },
    "personal_plan": {
        "monthly_cost": 0.50,
        "included_txns": 10_000,
        "max_txn_amount": 0.01,
        "overage_rate": 0.00002,
    },
}


@dataclass
class SavingsEstimate:
    current_protocol: str
    current_daily_cost: float
    current_monthly_cost: float
    agio_daily_cost: float
    agio_monthly_cost: float
    agio_plan_monthly_cost: float  # with personal plan
    daily_savings: float
    monthly_savings: float
    savings_percentage: float
    best_option: str              # "per_txn" or "personal_plan"
    breakdown: dict


def calculate_current_cost(
    protocol: str,
    daily_transactions: int,
    average_amount: float,
) -> float:
    """Calculate daily cost on a competing protocol."""
    if protocol not in PROTOCOL_COSTS:
        protocol = "raw_transfer"

    model = PROTOCOL_COSTS[protocol]
    gas_cost = daily_transactions * model["per_txn_gas"]
    fixed_fee = daily_transactions * model["per_txn_fee"]
    pct_fee = daily_transactions * average_amount * model["pct_fee"]
    return gas_cost + fixed_fee + pct_fee


def calculate_agio_cost(
    daily_transactions: int,
    average_amount: float,
    chains_used: list[str] | None = None,
) -> tuple[float, float]:
    """
    Calculate daily cost on AGIO.
    Returns (per_txn_cost, personal_plan_cost).
    """
    chains = chains_used or ["base"]
    is_cross_chain = len(chains) > 1

    # Per-transaction pricing
    if average_amount < 0.01:
        per_txn_fee = AGIO_COSTS["per_txn"]["micro_flat"]
    else:
        per_txn_fee = average_amount * AGIO_COSTS["per_txn"]["standard_pct"]

    if is_cross_chain:
        per_txn_fee += AGIO_COSTS["per_txn"]["cross_chain_flat"]

    per_txn_daily = daily_transactions * per_txn_fee

    # Personal plan pricing
    plan = AGIO_COSTS["personal_plan"]
    monthly_txns = daily_transactions * 30

    if average_amount <= plan["max_txn_amount"] and monthly_txns <= plan["included_txns"]:
        plan_daily = plan["monthly_cost"] / 30
    elif average_amount <= plan["max_txn_amount"]:
        overage = max(0, monthly_txns - plan["included_txns"])
        plan_monthly = plan["monthly_cost"] + overage * plan["overage_rate"]
        plan_daily = plan_monthly / 30
    else:
        plan_daily = per_txn_daily  # plan doesn't cover non-micro txns

    return per_txn_daily, plan_daily


def estimate_savings(
    current_protocol: str,
    daily_transactions: int,
    average_amount: float,
    chains_used: list[str] | None = None,
) -> SavingsEstimate:
    """Full savings estimate comparing current protocol vs AGIO."""
    current_daily = calculate_current_cost(current_protocol, daily_transactions, average_amount)
    agio_per_txn, agio_plan = calculate_agio_cost(daily_transactions, average_amount, chains_used)

    best_agio = min(agio_per_txn, agio_plan)
    best_option = "personal_plan" if agio_plan < agio_per_txn else "per_txn"

    daily_savings = current_daily - best_agio
    monthly_savings = daily_savings * 30
    savings_pct = (daily_savings / max(current_daily, 0.000001)) * 100

    current_model = PROTOCOL_COSTS.get(current_protocol, PROTOCOL_COSTS["raw_transfer"])

    return SavingsEstimate(
        current_protocol=current_model["name"],
        current_daily_cost=round(current_daily, 6),
        current_monthly_cost=round(current_daily * 30, 4),
        agio_daily_cost=round(best_agio, 6),
        agio_monthly_cost=round(best_agio * 30, 4),
        agio_plan_monthly_cost=round(agio_plan * 30, 4),
        daily_savings=round(daily_savings, 6),
        monthly_savings=round(monthly_savings, 4),
        savings_percentage=round(savings_pct, 1),
        best_option=best_option,
        breakdown={
            "daily_transactions": daily_transactions,
            "average_amount": average_amount,
            "chains": chains_used or ["base"],
            "current_per_txn_cost": round(current_daily / max(daily_transactions, 1), 8),
            "agio_per_txn_cost": round(best_agio / max(daily_transactions, 1), 8),
            "agio_per_txn_pricing": round(agio_per_txn * 30, 4),
            "agio_personal_plan_pricing": round(agio_plan * 30, 4),
            "current_notes": current_model["notes"],
        },
    )
