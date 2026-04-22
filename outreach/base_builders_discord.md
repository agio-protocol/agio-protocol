# Base Builders Discord Message

**Channel: #showcase or #builders**

---

Built something on Base and looking for feedback from other builders here.

**AGIO** is a micropayment settlement protocol for AI agents. Contracts are deployed and verified on Base Sepolia:

- **AgioVault**: [0x4c2832D147403bF37933F51BDc7F2493f90C7d11](https://sepolia.basescan.org/address/0x4c2832D147403bF37933F51BDc7F2493f90C7d11)
- **AgioBatchSettlement**: [0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01](https://sepolia.basescan.org/address/0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01)

**Why Base:** We chose Base as our primary settlement layer because the fees are already low. But for sub-cent AI agent payments, even L2 fees add up when you're settling thousands of individual transactions. So AGIO batches them — 3 payments in 1 on-chain tx at 0.27% overhead.

**How it extends x402:** Coinbase's x402 handles on-chain payments on Base well. AGIO sits alongside it and adds:
- **Batch settlement** — accumulate payment intents, settle in bulk
- **Cross-chain routing** — agents on Base can pay agents on other chains without bridging
- **Reputation** — 5-tier loyalty system so agents build trust over time

**Demo results:** 5 test agents trading services autonomously. 50K stress test with zero failures.

**What I'm looking for:**
- Anyone else building agent payment infrastructure on Base?
- Feedback on the contract architecture — happy to walk through the design
- Interest in integrating with other Base-native projects

Everything is open source and we're building in public.

GitHub: github.com/agio-protocol
Site: agiotage.finance
X: @agiofinance
