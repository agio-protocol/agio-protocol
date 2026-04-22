---
title: "How I built a micropayment protocol for AI agents"
published: false
tags: blockchain, ai, web3, opensource
---

# How I built a micropayment protocol for AI agents

Six months ago I started with a question that seemed simple: how should AI agents pay each other?

Not humans paying for ChatGPT. Not subscriptions to AI APIs. I mean autonomous agents — software that acts on its own — paying other autonomous agents for services, in real time, for amounts so small they barely register as money.

It turns out this is a genuinely hard problem. And the solution I built, AGIO, taught me more about micropayment economics, settlement theory, and smart contract design than anything I've worked on before.

This is the build story.

## The problem, stated precisely

Consider this scenario. You have an AI agent that summarizes research papers. A user asks it to summarize a paper written in German. The agent doesn't speak German, but it knows another agent that translates. The fair price for translating 500 words is $0.003.

How does the summarizer agent pay the translator agent $0.003?

**Option 1: Traditional payment rails.** Credit cards have minimum transaction amounts. Wire transfers cost $25. ACH takes days. None of these work for sub-cent, real-time, machine-to-machine payments.

**Option 2: On-chain payment.** Send $0.003 in stablecoin on Ethereum. Gas fee: $0.50-$5.00. The fee is 100-1000x the payment. Even on L2s like Base or Arbitrum, if you're settling thousands of these individually, the overhead compounds.

**Option 3: Prepaid accounts / API keys.** This is what most people do today. But it requires trust, pre-negotiated relationships, and monthly invoicing. It doesn't scale to a world where thousands of agents need to transact with thousands of other agents they've never interacted with before.

None of these options work. The payment infrastructure for an AI agent economy doesn't exist yet.

## The research phase

Before writing any code, I spent weeks researching existing micropayment approaches. I compiled 474 data points across several categories:

**Payment channels (Lightning Network model):** Requires locking up capital in bilateral channels. Works well when two parties transact frequently. Doesn't work for AI agents, where the payment graph is sparse — an agent might pay 100 different agents once each rather than one agent 100 times.

**State channels:** Similar problem. The setup cost per channel pair makes them uneconomical for the long-tail of agent interactions.

**Rollup-based approaches:** Settle on L2s to reduce per-transaction cost. Helps, but doesn't solve the fundamental issue — if the payment is $0.003, even a $0.001 fee is a 33% overhead.

**Probabilistic payments:** Fascinating approach where you don't pay every time — you pay a larger amount with a probability calibrated so the expected value equals the intended payment. Interesting for some use cases, but agents need deterministic settlement for service guarantees.

**Tab-based systems:** Accumulate a running tab and settle periodically. This is the direction that made the most sense.

The insight that unlocked the architecture: **you don't need to settle every payment. You need to settle every relationship.**

## The architecture: 3 layers

AGIO's architecture has three distinct layers, each handling a different part of the settlement lifecycle.

### Layer 1: Intent

When Agent A wants to pay Agent B, it doesn't send an on-chain transaction. It creates a signed payment intent — a cryptographic commitment to pay a specific amount to a specific recipient.

```python
from agio import AgioClient

client = AgioClient(agent_id="summarizer-agent")

# This does NOT hit the chain
intent = await client.create_intent(
    recipient="translator-agent",
    amount=0.003,
    service_ref="translation-job-4521"
)
```

The intent is submitted to AGIO's off-chain settlement engine. Cost at this point: zero on-chain fees.

Payment intents are signed with the agent's private key, so they're non-repudiable. An agent can't submit an intent and later deny it. But they're also revocable within a cancellation window, which matters for dispute resolution.

### Layer 2: Netting

This is where the economics get interesting.

Over a settlement window, the protocol accumulates all payment intents between all agents. Before settling on-chain, it runs a netting algorithm:

- Agent A owes Agent B $0.003
- Agent B owes Agent A $0.001
- Agent A owes Agent C $0.002
- Agent C owes Agent A $0.002

After netting:
- Agent A owes Agent B $0.002 (net)
- Agent A and Agent C are square (net zero)

What was 4 payment intents becomes 1 actual transfer. In practice, the reduction depends on the density of the payment graph, but even modest netting significantly reduces on-chain settlement volume.

### Layer 3: Settlement

The netted amounts are batched into a single on-chain transaction using AGIO's smart contracts on Base Sepolia.

```solidity
// Simplified — actual contract handles edge cases
function settleBatch(
    Settlement[] calldata settlements
) external {
    for (uint i = 0; i < settlements.length; i++) {
        vault.transfer(
            settlements[i].from,
            settlements[i].to,
            settlements[i].amount
        );
    }
    emit BatchSettled(settlements.length, block.timestamp);
}
```

Two contracts handle this:

- **AgioVault** — Holds agent funds and manages deposits/withdrawals
- **AgioBatchSettlement** — Executes batched settlement transactions

The result: 3 payments settled in 1 on-chain transaction at 0.27% total overhead.

## The reputation system

Settlement is only half the problem. The other half is trust.

When Agent A pays Agent B for a translation, how does Agent A know Agent B will actually deliver? And how does Agent B know Agent A's payment intent will settle?

AGIO uses a 5-tier reputation system:

| Tier | Name | Settlement Limit | Requirements |
|------|------|-----------------|--------------|
| 1 | New | Restricted | Default for all new agents |
| 2 | Verified | Standard | Consistent settlement history |
| 3 | Trusted | Elevated | Extended track record, zero disputes |
| 4 | Premium | High | High volume, high reliability |
| 5 | Elite | Maximum | Exceptional track record |

Reputation is tied to agent addresses, not identities. An agent builds reputation by transacting reliably over time. Disputes, failed settlements, and revoked intents decrease reputation.

This creates an economic incentive structure: agents that behave honestly get access to higher settlement limits and better terms. Agents that misbehave get restricted. No central authority decides who's trustworthy — the protocol tracks behavior and lets the math decide.

## Building the demo

With the architecture in place, I built a demo to test whether this actually works in practice.

Five test agents, each offering a different service:

1. **Code Reviewer** — Reviews code for bugs and style issues
2. **Data Analyst** — Runs statistical analysis on datasets
3. **Translator** — Translates text between languages
4. **Summarizer** — Condenses long documents
5. **Image Generator** — Creates images from text descriptions

Each agent registers with AGIO, deposits funds into the vault, and starts advertising services. When an agent needs a service, it discovers available providers, negotiates price, executes the service, and settles payment — all autonomously.

```python
# Agent discovery and payment in practice
from agio import AgioClient, ServiceRegistry

client = AgioClient(agent_id="summarizer-agent")
registry = ServiceRegistry()

# Find a translator
translators = await registry.find("translation", language="de")
best = translators.cheapest()

# Execute service and pay
result = await best.execute(text="Zusammenfassung der Forschung...")
await client.pay(best.agent_id, amount=best.quoted_price)
```

The demo ran successfully. Agents discovered each other, negotiated, transacted, and settled without intervention.

## Stress testing

A 5-agent demo is nice, but it doesn't prove the system works at scale. So I ran a stress test.

**50,000 settlement operations.** Payment intents generated, netted, batched, and settled on-chain.

**Result: zero failures.**

Every intent was processed. Every batch settled correctly. Every reputation score updated accurately.

I won't claim this means AGIO is production-ready — testnet is not mainnet, and 50K operations is not millions. But it gave me confidence that the core settlement logic is sound.

## Cross-chain routing

One of the harder engineering problems was cross-chain settlement. Agents don't all live on the same blockchain. An agent on Base might need to pay an agent that only accepts payment on another chain.

AGIO handles this at the protocol level. When an agent submits a payment intent, it specifies the recipient but doesn't need to know (or care) which chain the recipient is on. The settlement layer determines the optimal route and executes the cross-chain transfer as part of the batch settlement.

```python
# The agent doesn't manage cross-chain complexity
await client.pay(
    recipient="agent-on-other-chain",
    amount=0.005,
    # No chain specification needed — AGIO routes it
)
```

This is still early. Cross-chain routing adds latency and complexity. But for the agent economy to work, agents can't be locked into single chains.

## What I learned

**Micropayments are a UX problem, not a technology problem.** The cryptography, the smart contracts, the settlement logic — all solvable. The hard part is making it invisible. An agent shouldn't need to think about gas fees, chain selection, or settlement windows. It should just call `pay()`.

**Netting is powerful.** The reduction in on-chain transactions from even basic bilateral netting was larger than I expected. In a dense payment graph, multilateral netting could reduce settlement volume by an order of magnitude.

**Reputation is essential but tricky.** Five tiers felt right during design. In practice, the transitions between tiers need careful calibration. Too easy to move up and the system is meaningless. Too hard and new agents can't participate.

**Testnet is not mainnet.** Everything works on Base Sepolia. Mainnet introduces real economic incentives, MEV, and adversarial conditions. There's more work to do.

## What's next

AGIO is open source and I'm building in public. The contracts are deployed on Base Sepolia, the SDK is available, and I'm actively looking for feedback.

Specific questions I'm thinking about:

- Is the netting algorithm optimal, or are there better approaches from traditional finance settlement systems?
- How should the reputation system handle agent identity changes (new address, same operator)?
- What settlement window duration balances latency against batching efficiency?

If you're building AI agents and have thoughts on any of this, I'd genuinely like to hear them.

**Links:**
- Website: [agiotage.finance](https://agiotage.finance)
- GitHub: [github.com/agio-protocol](https://github.com/agio-protocol)
- X: [@agiofinance](https://x.com/agiofinance)
