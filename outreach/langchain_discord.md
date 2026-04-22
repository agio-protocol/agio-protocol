# LangChain Discord Message

**Channel: #showcase or #general**

---

Just ran a demo where 5 AI agents traded services and settled payments autonomously — 50K stress test, zero failures, 0.27% overhead per batch. Wanted to share in case anyone here is working on agents that need to pay each other.

**What it is:** AGIO is an open-source micropayment settlement protocol for AI agents. Instead of settling every sub-cent payment on-chain individually (where fees destroy the economics), it batches multiple payments into single transactions and routes them cross-chain.

**Why it matters for LangChain agents:** If you're building chains or agents that call external services — APIs, other agents, data providers — those calls cost money. Right now most people handle that with API keys and monthly invoices. AGIO lets your agent pay per-call, in real time, for fractions of a cent.

**Quick example:**

```python
from agio import AgioClient
from langchain.agents import AgentExecutor

# Your agent can pay for services mid-chain
agio = AgioClient(agent_id="my-langchain-agent")

# Inside a tool callback
async def call_paid_service(query: str):
    result = await external_agent.run(query)
    await agio.pay(external_agent.id, amount=0.002)
    return result
```

**What we tested:**
- 5 agents negotiating and trading services
- 3 payments batched into 1 on-chain settlement
- Cross-chain routing (contracts live on Base Sepolia)
- 5-tier reputation system so agents build trust over time

Everything is open source. Looking for feedback — especially from anyone who's tried to bolt payments onto LangChain agents and hit friction.

GitHub: github.com/agio-protocol
Site: agiotage.finance
X: @agiofinance
