# Show HN Post

**Title:** Show HN: AGIO -- Sub-cent micropayments for AI agents across any blockchain

**URL:** https://agiotage.finance

---

AGIO is a settlement protocol for AI agent micropayments. The core problem: agents trading services need to transact sub-cent amounts, but per-transaction fees on any chain make that uneconomical at scale.

**Architecture (3 layers):**

1. **Intent layer** — Agents submit signed payment intents off-chain. No on-chain cost at this stage.
2. **Netting layer** — Intents are accumulated over a settlement window, then netted. If A owes B and B owes A, only the difference moves.
3. **Settlement layer** — Netted amounts are batched into a single on-chain transaction. Contracts on Base Sepolia.

**Numbers from testing:**

- 3 payments per batch transaction, 0.27% overhead
- 50,000 settlement operations, 0 failures
- 5 test agents autonomously negotiating and settling

**Technical decisions worth discussing:**

We went with off-chain intent accumulation rather than payment channels (like Lightning) because agent-to-agent payment graphs are sparse and dynamic. Channels assume repeated bilateral transactions. Agents are more likely to pay many different agents small amounts infrequently.

Cross-chain routing is handled at the protocol level. An agent on Base paying an agent on another chain submits the same payment intent. The settlement layer handles routing.

The reputation system uses 5 tiers. New agents are restricted; consistent settlement behavior unlocks higher limits and better terms. No identity required — reputation is tied to agent address.

**Research:** We analyzed 474 data points across existing micropayment approaches before settling on this architecture.

**Stack:** Solidity contracts on Base Sepolia, Python SDK, off-chain settlement engine.

Code: https://github.com/agio-protocol

Looking for feedback on the netting approach and the reputation model in particular. Both feel like they have edge cases I haven't considered.
