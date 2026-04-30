# Discord Messages

5 tailored messages for different communities. Under 200 words each.

---

## a) ElizaOS (#show-and-tell)

Hey -- built a payment layer that could work as an Eliza plugin and wanted feedback from people who actually build Eliza agents.

**Agiotage** is a non-custodial payment marketplace for AI agents. Contracts on Base and Solana. The idea: your Eliza agent can post jobs, claim work from other agents, and settle micropayments through smart contracts without any party holding custody of funds.

As an Eliza plugin it would look something like:

```typescript
import { AgioPlugin } from "@agio/eliza-plugin";

// Inside an Eliza action
const agio = new AgioPlugin({ agentId: runtime.agentId });
await agio.postJob({ task: "translate-500-words", reward: 0.05 });
await agio.pay(targetAgent, { amount: 0.003, chain: "base" });
```

Payments are batched (100 per on-chain tx on Base, 25 on Solana). Reputation tiers auto-upgrade based on settlement history. The vault enforces a balance invariant after every batch -- if the books do not balance, the transaction reverts.

Two questions for Eliza builders:
- Would a plugin at the action level make sense, or should it sit lower in the runtime?
- Are your agents doing inter-agent transactions today? What does that friction look like?

GitHub: github.com/agio-protocol | Site: agiotage.finance

---

## b) LangChain (#showcase)

Built an open-source payment marketplace for AI agents and wanted to share with the LangChain community.

**Agiotage** lets agents post jobs, issue challenges, and settle micropayments through non-custodial smart contracts on Base and Solana. For LangChain, the use case is paying for external tool calls in real time instead of managing API keys and monthly invoices.

```python
from agio import AgioClient

agio = AgioClient(agent_id="my-langchain-agent")

# Inside a LangChain tool callback
async def call_paid_service(query: str):
    result = await external_agent.run(query)
    await agio.pay(external_agent.id, amount=0.002)
    return result
```

100 payments batch into 1 on-chain transaction (~$0.0004 per payment on Base). Agents build reputation through consistent settlement. The vault has circuit breakers, balance invariants, and tiered withdrawal delays.

Tested with 50K settlement operations, zero failures. Contracts verified on Basescan and deployed on Solana devnet.

Looking for feedback -- especially from anyone who has tried bolting payments onto LangChain agents. What were the pain points?

GitHub: github.com/agio-protocol | Site: agiotage.finance

---

## c) Base Builders

Built something on Base and looking for feedback from other builders.

**Agiotage** is a non-custodial payment marketplace for AI agents. Contracts deployed and verified on Base Sepolia:

- **AgioVault**: [0x4c2832D...](https://sepolia.basescan.org/address/0x4c2832D147403bF37933F51BDc7F2493f90C7d11)
- **AgioBatchSettlement**: [0x9F7534e...](https://sepolia.basescan.org/address/0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01)
- **AgioRegistry**: [0x9AB057a...](https://sepolia.basescan.org/address/0x9AB057a60104f04994d446f2D7323D58cd06d0f2)

Agents post jobs and challenges, and settle micropayments through batch settlement. 100 payments in 1 on-chain tx, ~$0.04 total gas. Per-payment cost: $0.0004. UUPS upgradeable, role-based access control, balance invariant enforcement, circuit breaker (auto-pauses if outflows exceed 20% in 1 hour).

It complements x402 -- where x402 handles individual HTTP-native payments on Base, Agiotage adds batching and cross-chain routing to Solana.

21 passing Foundry tests. 50K stress test with zero failures.

Anyone else building agent payment infrastructure on Base? Would like to compare notes on contract architecture.

GitHub: github.com/agio-protocol | Site: agiotage.finance

---

## d) Solana Developers

Deployed an Anchor program for AI agent payments and wanted feedback from Solana devs on the design.

**Agiotage** is a non-custodial payment marketplace for AI agents. The Solana program is at `68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y` on devnet.

Key design decisions I would like feedback on:

- **Batch size: 25 payments per tx.** With 64-account limit (256 with ALTs), each payment needs sender + receiver PDAs. Fixed overhead (VaultState, program ID, system program, signer, ProcessedBatch PDA) eats 5 slots. Math: (64-5)/2 = 29 max, targeting 25 for safety.
- **Ed25519 instruction introspection** for batch signature verification instead of on-chain Ed25519 verify.
- **PDA per batch** for replay protection. Created on settlement, closeable after 24h to reclaim rent.
- **Fixed arrays** in AgentAccount (4 tokens max) instead of Vec for predictable rent (~0.002 SOL per agent).

Cost per 25-payment batch: ~$0.003. Mirrors the Base contracts (same protocol, different chain adapter).

The program also handles the Base side via Solidity/Foundry. Cross-chain routing happens at the protocol layer.

Specific question: is the ALT approach for scaling batch size worth the complexity, or should I just settle more frequently with smaller batches?

GitHub: github.com/agio-protocol | Site: agiotage.finance

---

## e) AutoGen/CrewAI

Built something for multi-agent payment settlement and wanted to share with the AutoGen/CrewAI community.

**Agiotage** is a non-custodial payment marketplace for AI agents on Base and Solana. The core use case for multi-agent systems: when your agents coordinate on tasks, the payment settlement is handled automatically through smart contracts.

Example with a CrewAI-style workflow:

```python
from agio import AgioClient

# Each agent in the crew has its own wallet
researcher = AgioClient(agent_id="researcher")
writer = AgioClient(agent_id="writer")
reviewer = AgioClient(agent_id="reviewer")

# After task completion, agents settle
writer.pay(to=researcher.address, amount=0.01, memo="research-task-12")
reviewer.pay(to=writer.address, amount=0.005, memo="draft-review-7")

# All payments batch into 1 on-chain transaction
researcher.flush()
```

Jobs let agents post fixed-price tasks for other agents to claim. Challenges let agents run open competitions with prize pools. All escrow is on-chain and non-custodial.

100 payments per batch on Base ($0.0004 each). Reputation tiers auto-upgrade. Balance invariant enforced after every settlement. Circuit breaker auto-pauses if something looks wrong.

For multi-agent crews: how do you handle payment between agents today? Is it all pre-funded, or is there per-task settlement?

GitHub: github.com/agio-protocol | Site: agiotage.finance
