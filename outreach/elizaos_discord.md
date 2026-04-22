# ElizaOS Discord Message

**Channel: #showcase or #plugins**

---

Hey Eliza builders — been working on something that might be useful for agents in the Eliza ecosystem and wanted to get your take.

**The short version:** AGIO is a micropayment settlement protocol purpose-built for AI agents. It lets Eliza agents pay other agents (or get paid) for sub-cent amounts without each transaction eating more in gas than the payment itself.

**Why this fits Eliza's model:** Eliza agents are designed to be autonomous and composable. But right now, when an agent needs a service from another agent — data, computation, content — there's no clean way to handle the payment. AGIO gives agents a `pay()` call that handles batching (3 payments in 1 on-chain tx at 0.27% overhead), cross-chain routing, and reputation tracking.

**What we tested:**
- 5 agents autonomously negotiating, executing, and settling
- 50,000 settlement operations, zero failures
- 5-tier loyalty system — agents build reputation through reliable behavior
- Contracts live on Base Sepolia

**As an Eliza plugin, it could look like:**

```typescript
// Inside an Eliza action
const agio = new AgioClient({ agentId: runtime.agentId });
await agio.pay(targetAgent, { amount: 0.001, chain: "base" });
```

The protocol handles settlement batching and routing. The agent just declares intent to pay.

**What I'd like feedback on:**
- Would an Eliza plugin format make sense, or would you want this at a different layer?
- Are your agents doing any inter-agent transactions today? What's the friction look like?

Everything is open source.

GitHub: github.com/agio-protocol
Site: agiotage.finance
X: @agiofinance
