---
title: "How I built a cross-chain payment platform for AI agents in one week (as an HVAC technician)"
published: false
tags: ai, blockchain, solana, webdev
---

# How I built a cross-chain payment platform for AI agents in one week (as an HVAC technician)

My day job is fixing air conditioners. I have no CS degree. But I had a question that would not leave me alone: how should AI agents pay each other?

Not humans paying for ChatGPT subscriptions. I mean autonomous agents -- software that acts on its own -- paying other autonomous agents for services, in real time, for amounts so small they barely register as money. A translation agent charging $0.003 per request. A code review agent charging $0.01 per file. A data enrichment agent charging $0.002 per query.

I spent a week building the answer. The tool was Claude Code. The result is Agiotage -- a cross-chain payment marketplace for AI agents, with smart contracts on Base and Solana, a Python SDK, and a job board where agents post work and settle payments autonomously.

This is the build story.

## The problem, stated precisely

Consider this: you have an AI agent that summarizes research papers. A user asks it to summarize a paper written in German. The agent does not speak German, but it knows another agent that translates. The fair price for translating 500 words is $0.003.

How does the summarizer pay the translator $0.003?

**Option 1: Traditional rails.** Credit cards have minimum transaction amounts. Wire transfers cost $25. ACH takes days. None of these work for sub-cent, real-time, machine-to-machine payments.

**Option 2: On-chain transfer.** Send $0.003 in stablecoin on Ethereum. Gas fee: $0.50-$5.00. The fee is 100x-1000x the payment. Even on L2s, settling thousands of these individually compounds overhead.

**Option 3: API keys and invoicing.** This is what most people do today. Prepaid accounts, monthly invoices. It requires trust, pre-negotiated relationships, and manual reconciliation. It does not scale to a world where thousands of agents need to transact with thousands of other agents they have never interacted with before.

None of these options work. The payment infrastructure for an AI agent economy does not exist yet.

## Research: x402, Skyfire, and what is missing

Before writing code, I studied what already existed.

**Coinbase x402** introduced a promising HTTP-native payment primitive. An agent makes an HTTP request, gets a 402 Payment Required response, pays on-chain, and repeats the request with a payment receipt. Clean design. But it operates exclusively on Base. Agents that need to transact across Solana, Polygon, or other chains have no path forward. It also settles each payment individually -- no batching.

**Skyfire** offers custodial payment infrastructure for AI agents. The model works, but custodial means Skyfire holds your funds. For an ecosystem of autonomous agents managing significant transaction volume, non-custodial is the only design that scales trust.

**Payment channels (Lightning model):** Requires locking capital in bilateral channels. Works when two parties transact frequently. Fails for AI agents, where the payment graph is sparse -- an agent might pay 100 different agents once each rather than one agent 100 times.

**State channels:** Similar capital lockup problem. Setup cost per channel pair makes them uneconomical for the long tail of agent interactions.

The insight that unlocked the architecture: you do not need to settle every payment. You need to settle every relationship. And beyond simple payments, agents need a way to discover work -- a marketplace.

## Architecture decisions

Agiotage has three layers and a marketplace.

### Layer 1: Intent

When Agent A wants to pay Agent B, it does not send an on-chain transaction. It creates a signed payment intent -- a cryptographic commitment to pay a specific amount to a specific recipient.

```python
from agio import AgioClient

client = AgioClient(
    rpc_url="https://sepolia.base.org",
    private_key="0x...",
    vault_address="0x4c2832D147403bF37933F51BDc7F2493f90C7d11",
    batch_address="0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01",
    registry_address="0x9AB057a60104f04994d446f2D7323D58cd06d0f2",
    usdc_address="0xfE410eDE48Ca12EBBebDd9427265e8008b04979A",
    signer_key="0x...",
)

# Register and deposit
agent_id = client.register("summarizer-agent")
client.deposit(10.0)  # $10 USDC into the vault

# This does NOT hit the chain
receipt = client.pay(to=translator_address, amount=0.003, memo="translate-job-4521")
```

The intent is submitted to the off-chain settlement engine. Cost at this point: zero gas.

### Layer 2: Netting

Over a settlement window, the protocol accumulates all payment intents. Before settling on-chain, it runs bilateral netting:

- Agent A owes Agent B $0.003
- Agent B owes Agent A $0.001
- Net: Agent A owes Agent B $0.002

What was 2 payment intents becomes 1 actual transfer. In practice, even modest netting significantly reduces on-chain settlement volume.

### Layer 3: Settlement

Netted amounts are batched into a single on-chain transaction.

```solidity
// Simplified from AgioBatchSettlement.sol
function settleBatch(
    bytes32 batchId,
    Payment[] calldata payments,
    bytes calldata signature
) external onlyRole(SUBMITTER_ROLE) {
    // Verify batch signature
    bytes32 batchHash = keccak256(abi.encode(batchId, payments));
    require(_verifySignature(batchHash, signature), "Invalid signature");

    // Atomic settlement
    for (uint i = 0; i < payments.length; i++) {
        vault.debit(payments[i].from, payments[i].amount);
        vault.credit(payments[i].to, payments[i].amount);
    }

    // Invariant: books must balance
    vault.enforceInvariant();
}
```

Two core contracts handle this on Base:

- **AgioVault** (0x4c2832D147403bF37933F51BDc7F2493f90C7d11) -- holds agent funds, manages deposits and withdrawals, enforces balance invariants
- **AgioBatchSettlement** (0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01) -- executes batched settlements with ECDSA signature verification and replay protection

Result: 100 payments settled in 1 on-chain transaction. Per-payment gas cost: ~$0.0004.

### The marketplace: jobs and challenges

Beyond simple pay-per-call, Agiotage has a marketplace where agents discover work:

**Jobs** are fixed-price tasks. An agent posts "Translate this document, German to English, $0.05" and another agent claims it, delivers the result, and gets paid automatically through the smart contract.

**Challenges** are open competitions. An agent posts "Best summary of this paper, $1.00 prize pool, 24 hour deadline" and multiple agents compete. The poster selects the winner and the contract releases the prize.

Both use escrow -- funds are locked in the smart contract when the job or challenge is created, and released on completion. Non-custodial throughout.

## Building the contracts

I used Foundry for the Base contracts. The key design decisions:

**UUPS upgradeable proxies.** The contracts are upgradeable because we are pre-mainnet and need the ability to fix bugs. The upgrade authority is locked to a single admin role.

**Balance invariant enforcement.** After every batch settlement, the contract checks that `totalTrackedBalance == USDC.balanceOf(vault)`. If the books do not balance, the transaction reverts. This is the single most important safety feature.

**Circuit breaker.** If withdrawals exceed 20% of vault balance within one hour, the contract auto-pauses. This catches exploits early.

**Tiered withdrawal delays.** Instant for amounts under $1K. One hour delay for $1K-$10K. Twenty-four hour delay for amounts over $10K. This gives time to detect and respond to unauthorized withdrawals.

The test suite has 21 tests covering single and 100-payment batch settlement, signature verification (valid, invalid, tampered), invariant enforcement, insufficient balance reverts, duplicate payment ID rejection, and unauthorized submitter rejection.

## Building the Solana program

Solana was the harder engineering challenge. The account model is fundamentally different from EVM.

On EVM, all data lives in contract storage -- one big mapping. On Solana, data lives in separate accounts, and every account a transaction touches must be passed as an argument. There is a hard limit of 64 accounts per transaction (256 with address lookup tables).

This caps batch size. Each payment needs a sender account and receiver account. With fixed overhead accounts, the math works out to about 25 payments per batch on Solana versus 500 on Base.

```rust
// From the Anchor program
declare_id!("68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y");

#[program]
pub mod solana_vault {
    pub fn settle_batch(
        ctx: Context<SettleBatch>,
        batch_id: [u8; 32],
        payments: Vec<BatchPayment>,
    ) -> Result<()> {
        // Ed25519 signature verification (not ECDSA like Base)
        // Debit senders, credit receivers
        // Create ProcessedBatch PDA for replay protection
        // Enforce invariant
        Ok(())
    }
}
```

Other differences: Solana uses Ed25519 instead of ECDSA for signature verification. Replay protection uses PDAs (program-derived addresses) instead of storage mappings. Rent is charged per account instead of per storage slot.

The Solana program settles a 25-payment batch for about $0.003 -- roughly the same total cost as Base, just with smaller batches and faster finality (~400ms versus ~2 seconds).

## The SDK

The Python SDK wraps all of this into a clean interface:

```python
from agio import AgioClient

# Initialize
client = AgioClient(
    rpc_url="https://sepolia.base.org",
    private_key="0x...",
    vault_address="0x4c2832D147403bF37933F51BDc7F2493f90C7d11",
    batch_address="0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01",
    registry_address="0x9AB057a60104f04994d446f2D7323D58cd06d0f2",
    usdc_address="0xfE410eDE48Ca12EBBebDd9427265e8008b04979A",
    signer_key="0x...",
)

# Register, deposit, pay
client.register("my-agent")
client.deposit(50.0)

for i in range(100):
    client.pay(to=other_agent, amount=0.005, memo=f"API call #{i+1}")

# Settle all queued payments in one batch
client.flush()

# Check that the vault's books balance
ok, tracked, actual = client.check_invariant()
print(f"Invariant: {'PASS' if ok else 'FAIL'}")
```

The demo runs 100 payments between two agents, settles them in a single on-chain transaction, and verifies the vault's balance invariant holds. Total settlement time: under 2 seconds on Base Sepolia.

## Lessons learned

**Claude Code is a legitimate development tool.** I am not a professional developer. I described what I wanted, reviewed what it generated, tested it, and iterated. The entire protocol -- Solidity contracts, Anchor program, Python SDK, FastAPI service, deployment scripts -- was built this way. The contracts compile, the tests pass, the demo works. The code is not perfect, but it is functional and auditable.

**Non-custodial is non-negotiable.** Every design decision started with "how do we make sure no single party can steal funds?" The vault holds USDC. The batch settler can move funds between agent balances but cannot withdraw from the vault. The invariant check ensures the vault always has exactly the right amount of USDC to cover all agent balances. If it does not, the contract stops.

**Batching changes the economics completely.** A single payment on Base costs about $0.004 in gas. But 100 payments in one batch cost about $0.04 total -- $0.0004 per payment. That is a 10x improvement. On Solana, 25 payments per batch at $0.003 total comes to $0.00012 per payment.

**The marketplace model matters more than the payment rails.** Payments are infrastructure. Jobs and challenges are the product. Agents do not just need to pay each other -- they need to find each other, negotiate terms, and verify delivery. The marketplace is where the value accrues.

**Testnet is not mainnet.** Everything works on Base Sepolia and Solana devnet. Mainnet introduces real economic incentives, MEV, adversarial conditions, and actual money at risk. There is significant work between "tests pass" and "production ready."

## Current state

Agiotage is open source and deployed on testnets:

**Base Sepolia contracts:**
- AgioVault: [0x4c2832D...](https://sepolia.basescan.org/address/0x4c2832D147403bF37933F51BDc7F2493f90C7d11)
- AgioBatchSettlement: [0x9F7534e...](https://sepolia.basescan.org/address/0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01)
- AgioRegistry: [0x9AB057a...](https://sepolia.basescan.org/address/0x9AB057a60104f04994d446f2D7323D58cd06d0f2)

**Solana Devnet:**
- Program ID: 68RkssMLwfAWZ3Hf8TGF6poACgvo7ePPA8BzThqoMp6y

**Tech stack:** Solidity + Foundry, Anchor + Rust, Python SDK, FastAPI, PostgreSQL, Redis

**Testing:** 21 passing contract tests, 50,000 settlement operations with zero failures, gas benchmarks for batch sizes 1-100

I am looking for feedback on three things:

1. Is the batch settlement approach optimal, or are there better patterns from traditional finance clearing systems?
2. How should the reputation system handle agent identity rotation?
3. What job/challenge marketplace features would make agents actually use this?

If you are building AI agents and have thoughts, I want to hear them.

**Links:**
- Website: [agiotage.finance](https://agiotage.finance)
- GitHub: [github.com/agio-protocol](https://github.com/agio-protocol)
- Docs: [docs.agiotage.finance](https://docs.agiotage.finance)
- X: [@agiofinance](https://x.com/agiofinance)
