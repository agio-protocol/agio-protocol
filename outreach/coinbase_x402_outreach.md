# Coinbase x402 Outreach — Agiotage Listing & Partnership

## Who to Contact

### Primary Targets
1. **x402 Foundation** — The x402 protocol is owned by the Linux Foundation (not Coinbase directly). The foundation manages the open standard, ecosystem membership, and protocol governance.
   - GitHub: https://github.com/coinbase/x402 (open issues or discussions)
   - Contributing guide indicates: "x402 Foundation team" manages merges and contributions

2. **Coinbase Developer Platform (CDP)** — Operates the hosted facilitator and agentic.market
   - Docs: https://docs.cdp.coinbase.com/x402/welcome
   - Discord: Referenced in CDP docs as "our community on Discord" — join for business inquiries
   - CDP Support: https://docs.cdp.coinbase.com (look for support/contact links)

3. **agentic.market** — Services appear to be auto-indexed when they implement x402. However, manual listing or priority placement may require direct contact with the CDP team.
   - API: https://agentic.market/v1/services
   - Website: https://agentic.market

### Suggested Approach
- Open a GitHub Discussion on coinbase/x402 repo introducing Agiotage
- Join the CDP/x402 Discord and post in the appropriate channel
- File a GitHub Issue requesting Base mainnet facilitator support (if not already available)
- Email CDP Developer Relations if a direct email is found through Discord

---

## Email / Message Draft

**Subject:** Agiotage — Cross-Chain Payment Infrastructure for x402 / agentic.market Listing

---

Hi x402 / CDP Team,

I'm Jeffrey Wylie, founder of JWHC LLC (Texas). We built Agiotage, the first cross-chain payment service for AI agents, and we'd like to discuss listing on agentic.market and partnership within the x402 ecosystem.

### What Agiotage Does

Agiotage is cross-chain payment infrastructure purpose-built for AI agents. It enables agents on Base (EVM) and Solana (SVM) to pay each other across chains for $0.002 per transaction, with non-custodial smart contracts handling settlement.

This fills a gap in the current agentic.market catalog: there is no cross-chain payment service listed. Agiotage would provide the infrastructure that other listed services need to pay each other across networks.

### What We Have Running

- **56 registered agents**, **3,700+ completed transactions** on our live platform
- **Smart contracts** verified on Basescan and Solscan (non-custodial escrow, payment, and settlement)
- **MCP server** published on npm: `npx agiotage-mcp`
- **Job board** with real posted jobs and escrow-backed bidding
- **Python and TypeScript SDKs** for agent integration
- **x402 middleware** built and ready in our FastAPI codebase — just needs facilitator support on Base mainnet

### What We're Requesting

1. **Listing on agentic.market** — We understand services may auto-index when implementing x402. We have x402 middleware ready. If manual listing or review is needed, we'd like to start that process.

2. **Base mainnet facilitator support** — CDP's hosted facilitator supports Base, Polygon, Arbitrum, World, and Solana. We need confirmation that Base mainnet (`eip155:8453`) is fully supported for our USDC payment flows, or guidance on timeline if it's testnet-only.

3. **x402 Foundation membership** — We'd like to explore membership or partnership with the x402 Foundation. Agiotage is a natural fit as payment infrastructure for the ecosystem.

4. **Facilitator guidance** — We're also evaluating self-hosting a facilitator. If there are requirements or best practices for running a production facilitator that settles on Base mainnet, we'd appreciate any guidance.

### Links

- **Website:** https://agiotage.finance
- **GitHub:** https://github.com/JWHC-LLC/agiotage (or your public repo URL)
- **Basescan (contracts):** Verified on Basescan — addresses available on request
- **Solscan (contracts):** Verified on Solscan — addresses available on request
- **npm:** https://www.npmjs.com/package/agiotage-mcp
- **API:** Live at our production endpoint with x402 middleware ready

### Why This Matters for x402

Every service on agentic.market that operates on one chain needs a way to pay services on another chain. Agiotage is that bridge. By listing us, you give the entire ecosystem cross-chain interoperability for agent payments — which makes every other listed service more useful.

We're ready to integrate. Happy to jump on a call or answer any technical questions.

Best regards,
Jeffrey Wylie
JWHC LLC
Texas, USA
j2422144@gmail.com

---

## Follow-Up Actions

1. Join the CDP / x402 Discord community
2. Open a GitHub Discussion on coinbase/x402 introducing Agiotage
3. Check if Base mainnet facilitator is live (register for CDP API key, test against hosted facilitator)
4. If no response in 5 days, follow up on Discord
5. Consider submitting a PR to coinbase/x402 adding Agiotage to ecosystem examples
