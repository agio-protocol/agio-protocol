# Self-Hosted x402 Facilitator Plan

## What a Facilitator Does

The facilitator is the settlement layer of the x402 protocol. It sits between the resource server (your API) and the blockchain, handling two critical functions:

1. **Verify** (`POST /verify`) — Validates that a payment signature is authentic, the payer has sufficient balance, the amount matches the requirement, and the authorization hasn't expired. This happens off-chain and is fast.

2. **Settle** (`POST /settle`) — Submits the actual on-chain transaction to transfer funds from the payer to the payee. Waits for blockchain confirmation before returning a response with the transaction hash.

3. **Supported** (`GET /supported`) — Reports which payment schemes and networks this facilitator can handle (e.g., `eip155:8453` for Base mainnet, `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` for Solana mainnet).

The facilitator signs and submits settlement transactions using its own wallet. It is NOT a custodian — it executes pre-authorized transfers that the payer already signed.

---

## Architecture

```
Agent (payer) --> Your API (resource server) --> Facilitator --> Blockchain
                                                    |
                                              /verify (off-chain check)
                                              /settle (on-chain tx submission)
```

The flow:
1. Agent hits your API endpoint
2. Your API returns `402 Payment Required` with payment requirements
3. Agent signs a payment authorization and resends the request with `X-PAYMENT` header
4. Your API forwards the payment payload to the facilitator's `/verify` endpoint
5. If valid, your API serves the response
6. Your API calls the facilitator's `/settle` endpoint to execute the on-chain transfer
7. Facilitator submits the transaction and returns the tx hash

---

## How to Deploy a Self-Hosted Facilitator

### Reference Implementation

The official example is at: `https://github.com/coinbase/x402/tree/main/examples/typescript/facilitator/basic`

It's a Node.js (TypeScript) Express server using these packages:
- `@x402/core` — Core facilitator class (`x402Facilitator`)
- `@x402/evm` — EVM chain support (Base, Polygon, Arbitrum, etc.)
- `@x402/svm` — Solana chain support
- `viem` — EVM wallet/RPC client
- `@solana/kit` — Solana wallet/RPC client
- `express` — HTTP server

### Environment Variables

```bash
PORT=4022                    # Server port (default 4022)
EVM_PRIVATE_KEY=0x...        # Facilitator wallet private key (EVM)
SVM_PRIVATE_KEY=...          # Facilitator wallet private key (Solana, base58)
```

**Security note:** The facilitator private key is used ONLY to submit settlement transactions. It needs gas funds (ETH on Base, SOL on Solana) but does NOT hold user funds. Keep it separate from your payTo wallet.

### Setup Steps

```bash
# 1. Clone the x402 repo
git clone https://github.com/coinbase/x402.git
cd x402/examples/typescript

# 2. Install dependencies
pnpm install && pnpm build

# 3. Configure environment
cd facilitator/basic
cp .env-local .env
# Edit .env with your private keys

# 4. Modify index.ts for Base mainnet (see below)

# 5. Run
pnpm dev    # development
pnpm start  # production
```

### Configuring for Base Mainnet

The example ships with Base Sepolia (`eip155:84532`). To run on Base mainnet, modify `index.ts`:

```typescript
import { base } from "viem/chains";  // Change from baseSepolia

// Change the viem client chain
const viemClient = createWalletClient({
  account: evmAccount,
  chain: base,                        // Base mainnet
  transport: http(),                  // Uses default public RPC; set a private RPC for production
}).extend(publicActions);

// Register Base mainnet instead of Sepolia
facilitator.register(
  "eip155:8453",                      // Base mainnet CAIP-2 identifier
  new ExactEvmScheme(evmSigner, { deployERC4337WithEIP6492: true }),
);
facilitator.register("eip155:8453", new UptoEvmScheme(evmSigner));

// Optionally also register Solana mainnet
facilitator.register(
  "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",  // Solana mainnet
  new ExactSvmScheme(svmSigner),
);
```

### Configuring Your API to Use Your Facilitator

In your FastAPI x402 middleware, point verification and settlement to your facilitator instead of `x402.org`:

```python
FACILITATOR_URL = "https://your-facilitator.yourdomain.com"  # Your self-hosted facilitator

# In the middleware, verify payments against your facilitator
async def verify_payment(payment_payload, payment_requirements):
    response = await httpx.post(
        f"{FACILITATOR_URL}/verify",
        json={
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
    )
    return response.json()

async def settle_payment(payment_payload, payment_requirements):
    response = await httpx.post(
        f"{FACILITATOR_URL}/settle",
        json={
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
    )
    return response.json()
```

---

## Infrastructure Requirements

### Minimum Setup
- **Server:** Any VPS or cloud instance (1 vCPU, 512MB RAM is sufficient — it's a lightweight Node.js service)
- **RPC endpoint:** Base mainnet RPC (default public RPC works but is rate-limited; use Alchemy, Infura, or QuickNode for production)
- **Facilitator wallet:** Funded with ETH on Base for gas fees (~$0.001-0.01 per settlement tx on Base L2)
- **Domain + TLS:** HTTPS endpoint required for production use
- **Solana RPC:** If supporting Solana, a Solana mainnet RPC (Helius, Triton, or public endpoint)

### Recommended Production Setup
- **Hosting:** Railway, Render, Fly.io, or a small EC2/GCP instance ($5-20/month)
- **RPC:** Alchemy or QuickNode free tier (Base mainnet) — 300M compute units/month free on Alchemy
- **Monitoring:** Basic uptime monitoring (UptimeRobot free tier)
- **Wallet funding:** Keep $5-10 in ETH on Base in the facilitator wallet for gas (~5,000-10,000 settlements worth)

---

## Estimated Monthly Cost

| Item | Cost |
|------|------|
| VPS / hosting | $5 - $20 |
| RPC endpoint (Base) | $0 (free tier) - $49 (growth) |
| RPC endpoint (Solana) | $0 (free tier) - $50 |
| Gas for settlements | ~$0.001/tx on Base L2 |
| Domain / TLS | $0 (use existing) |
| **Total** | **$5 - $120/month** |

At current Base L2 gas costs, settling 1,000 transactions costs roughly $1-10 in gas. The main cost is hosting and RPC access.

---

## Pros vs Cons: Self-Hosted vs Waiting for Coinbase

### Self-Hosted Facilitator

**Pros:**
- Full control over supported networks and chains
- No dependency on Coinbase/CDP for mainnet support
- Can customize verification logic (hooks for rate limiting, fraud detection, etc.)
- No per-transaction fees to CDP (they charge $0.001/tx after 1,000 free/month)
- Can support any EVM chain or Solana network immediately
- Demonstrates technical depth to the x402 ecosystem

**Cons:**
- You are responsible for uptime, security, and wallet management
- Must keep the facilitator wallet funded with gas
- Must manage private key security (if compromised, attacker can submit settlement txs)
- Not auto-indexed by agentic.market (CDP's hosted facilitator may be required for auto-listing)
- You maintain the code — must stay current with x402 protocol updates
- If the x402 SDK has bugs in settlement logic, you're on your own to debug

### Waiting for Coinbase CDP Hosted Facilitator

**Pros:**
- Zero infrastructure to manage
- CDP already supports Base, Polygon, Arbitrum, World, and Solana
- 1,000 free transactions/month, then $0.001/tx
- Likely required for auto-indexing on agentic.market
- Coinbase handles security, uptime, and protocol updates

**Cons:**
- You depend on their timeline and priorities
- Base mainnet may already be supported (needs verification — their docs list "Base" without specifying testnet vs mainnet)
- Less control over custom logic
- Per-transaction fees at scale

---

## Recommendation

**Do both in parallel:**

1. **Immediately:** Test whether CDP's hosted facilitator already supports Base mainnet (`eip155:8453`). Their docs list "Base" as a supported network — it may already work. Sign up for a CDP API key and test against their facilitator endpoint.

2. **This week:** Set up a self-hosted facilitator on a cheap VPS as a fallback. Use the official example code. This takes about 2 hours and costs $5/month. It gives you full control and lets you test your x402 middleware end-to-end without waiting on anyone.

3. **Outreach:** Contact the x402 team (see coinbase_x402_outreach.md) to confirm Base mainnet support and discuss agentic.market listing. If auto-indexing requires their hosted facilitator, you'll need their support regardless.

4. **Long term:** If Agiotage grows, the self-hosted facilitator saves money (no $0.001/tx fee) and gives you flexibility. If volume stays low, CDP's hosted facilitator is simpler to maintain.

---

## CAIP-2 Network Identifiers (Reference)

| Network | CAIP-2 ID |
|---------|-----------|
| Base Sepolia (testnet) | `eip155:84532` |
| Base Mainnet | `eip155:8453` |
| Ethereum Mainnet | `eip155:1` |
| Polygon | `eip155:137` |
| Arbitrum | `eip155:42161` |
| Solana Devnet | `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` |
| Solana Mainnet | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` |
