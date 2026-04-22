# AGIO Solana Program — Architecture Plan

## Executive Summary

The Solana program mirrors the Base AgioVault but is designed around 
Solana's account model instead of EVM's storage model. The key 
differences affect batch size, account structure, and cost.

---

## 1. Account Model Differences (Solana vs EVM)

### EVM (Base — what we have)
```
AgioVault contract:
  mapping(agent => mapping(token => balance))    # one storage slot per agent per token
  mapping(agent => mapping(token => locked))     # another slot
  mapping(token => totalTracked)                 # per-token total
```
All data lives in one contract. Any function can read/write any agent.

### Solana (what we're building)
```
Programs don't have persistent storage. Data lives in ACCOUNTS:

VaultState (PDA)         — 1 account, ~200 bytes, created once
AgentAccount (PDA)       — 1 per agent, ~300 bytes each
ProcessedBatch (PDA)     — 1 per batch, ~48 bytes each (replay protection)
Token Accounts (ATA)     — 1 per token held by vault PDA
```
Every account that a transaction touches must be passed as an argument.
This is the primary constraint on batch size.

---

## 2. Batch Size Constraint (CRITICAL)

### The account limit problem

Solana transactions have a hard limit: **64 unique accounts per transaction**.

Every payment in a batch needs:
- Sender AgentAccount PDA (read/write)
- Receiver AgentAccount PDA (read/write)

Plus fixed overhead per transaction:
- VaultState PDA (1)
- Program ID (1)
- System Program (1)
- Signer/payer (1)
- ProcessedBatch PDA for replay protection (1)

**Calculation:**
- Fixed accounts: 5
- Per-payment accounts: 2 (sender + receiver)
- With 64 account limit: (64 - 5) / 2 = **29 payments max**

BUT: if payments share agents (A→B, A→C, B→D), accounts are deduplicated.
- Worst case (all unique agents): 29 payments
- Best case (hub-and-spoke, one sender): 59 payments
- Realistic average: **20-30 payments per transaction**

### Address Lookup Tables (ALTs)

Versioned transactions (v0) with ALTs allow **256 accounts**.
- With ALT: (256 - 5) / 2 = **125 payments max**
- Requires pre-creating the ALT and populating it with known agent addresses
- **Recommendation: use ALTs from day one.** They're standard on mainnet.

### Compute Units

Each payment requires ~50,000 compute units (debit + credit + verify).
Default CU budget: 200,000. Max requestable: 1,400,000.

- At 50K CU/payment: 1,400,000 / 50,000 = **28 payments per CU budget**
- This aligns with the account limit — both cap around 25-30.

**Decision: target 25 payments per batch on Solana.**
Base handles 500. This means Solana batches settle more frequently.

---

## 3. Account Structures

### VaultState (created once at deployment)
```rust
#[account]
pub struct VaultState {
    pub authority: Pubkey,        // 32 bytes — admin
    pub batch_signer: Pubkey,     // 32 — authorized batch submitter
    pub fee_collector: Pubkey,    // 32 — receives fees
    pub is_paused: bool,          // 1
    pub total_agents: u64,        // 8
    pub total_batches: u64,       // 8
    pub total_payments: u64,      // 8
    pub bump: u8,                 // 1
    // Fixed array for tracked balances (max 8 tokens)
    pub tracked_balances: [TrackedToken; 8],  // 8 * 40 = 320
}
// Total: ~442 bytes + discriminator
```

### AgentAccount (one per agent, PDA seeded by wallet)
```rust
#[account]
pub struct AgentAccount {
    pub wallet: Pubkey,           // 32 — agent's main wallet
    pub registered_at: i64,       // 8
    pub total_payments: u64,      // 8
    pub total_volume: u64,        // 8
    pub preferred_token: Pubkey,  // 32 — preferred receive token mint
    pub tier: u8,                 // 1
    pub bump: u8,                 // 1
    // Fixed array for balances (max 4 tokens per agent)
    pub balances: [TokenBalance; 4],  // 4 * 24 = 96
}
// Total: ~186 bytes + discriminator
```

Why fixed arrays instead of Vec:
- Vec requires dynamic allocation, makes account size unpredictable
- Solana charges rent proportional to account size
- Fixed arrays mean predictable rent (~0.002 SOL per agent, paid once)
- 4 tokens per agent is enough (USDC, USDT, SOL, and 1 spare)
- 8 tracked tokens per vault covers all supported mints

### TokenBalance / TrackedToken
```rust
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Default)]
pub struct TokenBalance {
    pub mint: Pubkey,   // 32
    pub available: u64, // 8
    pub locked: u64,    // 8
}
// 48 bytes each — but aligned to 40 with packing

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Default)]
pub struct TrackedToken {
    pub mint: Pubkey,   // 32
    pub total: u64,     // 8
}
// 40 bytes each
```

---

## 4. Instruction Design

### initialize_vault
- Creates VaultState PDA (seed: "vault")
- Sets authority, batch_signer, fee_collector
- Called once at deployment

### register_agent
- Creates AgentAccount PDA (seed: "agent" + wallet_pubkey)
- Agent pays rent (~0.002 SOL)
- Sets wallet, preferred_token, initial tier

### deposit
- Agent transfers SPL tokens from their wallet to vault's token account
- Increments agent's balance in AgentAccount
- Increments tracked_balances in VaultState
- Uses CPI to SPL Token program

### withdraw
- Checks tiered delay (instant < $1K, 1h < $10K, 24h > $10K)
- Transfers SPL tokens from vault to agent wallet
- Decrements agent balance and tracked_balances
- Circuit breaker check (20% outflow threshold)

### settle_batch
- Takes array of BatchPayment structs
- Verifies Ed25519 signature from batch_signer (Solana uses Ed25519, not ECDSA)
- For each payment: debit sender, credit receiver, collect fee
- Creates ProcessedBatch PDA for replay protection
- Atomic — all succeed or all revert

### check_invariant
- Reads vault's token account balance
- Compares to tracked_balances sum
- Returns bool (anyone can call)

---

## 5. Signature Verification

Base uses ECDSA (secp256k1). Solana uses **Ed25519**.

The batch signer will have a Solana keypair (Ed25519).
Verification options:
1. **Ed25519 native program** — Solana has a built-in Ed25519 sig verify
   program at `Ed25519Program.programId`. Cheapest option.
2. **Instruction introspection** — Check that a prior instruction in the 
   same transaction was an Ed25519 verify instruction. This is the 
   standard Anchor pattern.

**Decision: Use Ed25519 instruction introspection.** The API server signs
the batch hash with the Solana batch signer key, includes the verify
instruction before the settle_batch instruction, and the program checks
that the verify passed.

---

## 6. Replay Protection

No nonces on Solana. Two approaches:

1. **PDA per batch** — Create a ProcessedBatch PDA (seed: "batch" + batch_id).
   If the PDA already exists, the transaction fails. Simple, reliable.
   Cost: ~0.001 SOL rent per batch (~$0.15). Accounts can be closed 
   after 24 hours to reclaim rent.

2. **Bitmap** — Store a bitmap of processed payment IDs in a large account.
   More space-efficient but complex to implement.

**Decision: PDA per batch.** Simple, auditable, automatic collision 
detection. Close after 24h to reclaim rent.

---

## 7. Cost Analysis

### Deployment
- Program deploy: ~3 SOL (~$450 at $150/SOL)
  - Actually much less with Anchor — program is ~50KB, costs ~0.5 SOL
- VaultState init: 0.003 SOL
- Total deploy cost: **~$75-100**

### Per-transaction costs
- Base transaction fee: 5,000 lamports (0.000005 SOL, ~$0.00075)
- Priority fee (recommended): 10,000-50,000 lamports
- Compute units: included in base fee up to 200K CU
- Additional CU (if needed): ~0.000001 SOL per 1000 CU

### Per-batch costs (25 payments)
- Base fee: $0.00075
- Priority fee: ~$0.002
- ProcessedBatch rent: $0.15 (reclaimable)
- **Net cost per batch: ~$0.003 (matches Base!)**
- **Cost per payment: ~$0.00012**

### vs Base comparison
| | Base | Solana |
|---|---|---|
| Max batch size | 500 | 25 (with ALT) |
| Cost per batch | ~$0.004 | ~$0.003 |
| Cost per payment | ~$0.000008 | ~$0.00012 |
| Settlement time | ~2 sec | ~0.4 sec |

Solana is slightly more expensive per payment but settles faster.
At $0.002 cross-chain fee, both chains are highly profitable for 
cross-chain payments.

---

## 8. Token Support

Solana mainnet tokens to whitelist:
- **USDC-SPL**: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (6 decimals)
- **USDT-SPL**: `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` (6 decimals)
- **SOL** (wrapped): `So11111111111111111111111111111111111111112` (9 decimals)

Jupiter aggregator for on-chain swaps (SOL ↔ USDC).

---

## 9. Security (matching Base)

| Feature | Base | Solana |
|---|---|---|
| Non-custodial | UUPS proxy | PDA-controlled vault |
| Batch signature | ECDSA (secp256k1) | Ed25519 |
| Replay protection | mapping(bytes32 => bool) | PDA per batch |
| Atomic batches | EVM revert | Solana tx failure |
| Pause | AccessControl role | authority check |
| Invariant | checkInvariant() | check_invariant() |
| Circuit breaker | 20% outflow/hour | same |
| Withdrawal delay | tiered | same |
| Upgradeable | UUPS proxy | Anchor upgradeable |

---

## 10. Build Plan

### Phase 1: Toolchain (30 min)
- Install Rust, Solana CLI, Anchor
- Create project structure
- Verify build works

### Phase 2: Core Program (4-6 hours)
- VaultState, AgentAccount structs
- initialize_vault, register_agent
- deposit, withdraw (with SPL token CPI)
- settle_batch (with Ed25519 verification)
- pause/unpause
- check_invariant

### Phase 3: Tests (2-3 hours)
- 22 tests listed in the spec
- Compute unit benchmarks for batch sizes 1-25

### Phase 4: Devnet Deploy (1 hour)
- Deploy program
- Create token accounts
- Run full test suite against devnet

### Phase 5: Solana Batch Worker (2-3 hours)
- Python worker using solders/solana-py
- Same Redis queue, filtered by chain
- Ed25519 signing
- Transaction construction

### Phase 6: Integration (1-2 hours)
- Cross-chain tests: Base ↔ Solana
- Reserve management
- Dashboard updates

### Phase 7: Mainnet (1 hour)
- Deploy program
- Fund with $50 USDC-SPL
- Verify with test payment

**Total estimated time: 12-16 hours across 2-3 sessions**

---

## 11. Key Decisions Summary

| Decision | Choice | Reason |
|---|---|---|
| Max batch size | 25 | Account limit + CU budget |
| Account structure | Fixed arrays | Predictable rent, simpler code |
| Replay protection | PDA per batch | Simple, auditable |
| Signature scheme | Ed25519 introspection | Native Solana, cheapest |
| Token limit per agent | 4 | Covers USDC, USDT, SOL + 1 spare |
| Upgradeable | Yes (Anchor default) | Matches Base UUPS pattern |
| Address Lookup Tables | Yes, from day one | 2-3x more payments per batch |
