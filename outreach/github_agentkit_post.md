# Cross-chain micropayment settlement for AgentKit agents

**Posted to: Coinbase AgentKit GitHub Discussions**

---

Hey all,

I've been building with AgentKit and want to share something I've been working on — looking for feedback from folks who've dealt with agent-to-agent payments.

## The problem

x402 is excellent for on-chain payments on Base. If your agents live entirely within the Base ecosystem, it handles the job well. But I kept running into a wall: my agents needed to pay agents on other chains, and individual sub-cent transactions were eating fees that exceeded the payment itself.

## What AGIO does differently

AGIO is a micropayment settlement protocol designed specifically for AI agents. It sits alongside x402 and extends the payment surface cross-chain. Three core ideas:

1. **Batch settlement** — Instead of settling every micropayment individually, AGIO accumulates payment intents and settles them in batches. In testing, we batch 3 payments into 1 on-chain transaction at 0.27% overhead. That's the difference between micropayments being viable and not.

2. **Cross-chain routing** — Agents on Base can pay agents on other chains without either side managing bridges. The protocol handles route selection and settlement.

3. **Reputation scoring** — A 5-tier loyalty system tracks agent reliability. Trusted agents get better settlement terms. New agents start restricted and earn trust through consistent behavior.

## Demo results

We ran a live demo with 5 test agents trading services — code review, data analysis, translation, summarization, and image generation. The agents negotiated prices, executed services, and settled payments autonomously.

Then we stress-tested: 50,000 settlement operations with zero failures.

## Integration with AgentKit

```python
from agio import AgioClient

client = AgioClient(agent_id="your-agent")
await client.pay("target-agent", amount=0.003, chain="base")
```

The client handles batching, routing, and settlement automatically. Your agent just calls `pay()`.

## Contracts on Base Sepolia

The settlement contracts are live on Base Sepolia testnet. Everything is open source.

## What I'm looking for

- Has anyone else run into the cross-chain payment problem with AgentKit agents?
- Does the batching approach make sense for your use cases, or are your payments large enough that individual settlement works fine?
- Any thoughts on the reputation model? We went with 5 tiers but I'm not sure that's the right granularity.

Happy to walk through the architecture in more detail if anyone's interested.

Links:
- Website: [agiotage.finance](https://agiotage.finance)
- GitHub: [github.com/agio-protocol](https://github.com/agio-protocol)
- X: [@agiofinance](https://x.com/agiofinance)
