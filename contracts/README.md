# AGIO Protocol — Smart Contracts

> Instant, sub-cent payments between AI agents across any blockchain.

AGIO is a cross-chain micropayment routing protocol for AI agents. These contracts form the settlement layer — the on-chain foundation that holds funds, processes batched payments, and tracks agent identity.

## Deployed Contracts (Base Sepolia)

| Contract | Address | Explorer |
|---|---|---|
| **AgioVault** | `0x4c2832D147403bF37933F51BDc7F2493f90C7d11` | [View](https://sepolia.basescan.org/address/0x4c2832D147403bF37933F51BDc7F2493f90C7d11) |
| **AgioBatchSettlement** | `0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01` | [View](https://sepolia.basescan.org/address/0x9F7534ef8a023c3f4b8F40B43F1F9a1A09815A01) |
| **AgioRegistry** | `0x9AB057a60104f04994d446f2D7323D58cd06d0f2` | [View](https://sepolia.basescan.org/address/0x9AB057a60104f04994d446f2D7323D58cd06d0f2) |
| **MockUSDC** | `0xfE410eDE48Ca12EBBebDd9427265e8008b04979A` | [View](https://sepolia.basescan.org/address/0xfE410eDE48Ca12EBBebDd9427265e8008b04979A) |

Chain: Base Sepolia (Chain ID 84532) · RPC: `https://sepolia.base.org`

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Intelligence (future)                             │
│  Agent reputation, credit scoring, payment negotiation      │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: Router (off-chain)                                │
│  Payment queue → Batch assembly → Signed submission         │
│  FastAPI + PostgreSQL + Redis                               │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: Settlement (on-chain) ← THIS REPO                │
│                                                             │
│  ┌──────────────┐  ┌───────────────────┐  ┌──────────────┐ │
│  │  AgioVault   │  │ AgioBatchSettlement│  │ AgioRegistry │ │
│  │              │  │                   │  │              │ │
│  │ • Deposit    │  │ • Atomic batches  │  │ • Agent ID   │ │
│  │ • Withdraw   │  │ • ECDSA signed    │  │ • Auto-tier  │ │
│  │ • Debit/     │  │ • Replay protect  │  │ • Stats      │ │
│  │   Credit     │  │ • Max value cap   │  │ • Reputation │ │
│  │ • Invariant  │  │ • Rate limiting   │  │              │ │
│  │ • Circuit    │  │ • Invariant       │  │              │ │
│  │   breaker    │  │   enforcement     │  │              │ │
│  └──────────────┘  └───────────────────┘  └──────────────┘ │
│                                                             │
│  Base (Sepolia) · USDC · UUPS Upgradeable                  │
└─────────────────────────────────────────────────────────────┘
```

## Contracts

### AgioVault

The vault where agents deposit and withdraw USDC. The batch settlement contract debits and credits balances internally — no token transfers needed for agent-to-agent payments within AGIO.

- USDC deposits and withdrawals
- Tiered withdrawal delays (instant < $1K, 1hr < $10K, 24hr > $10K)
- Circuit breaker (auto-pauses if outflows exceed 20% of vault in 1 hour)
- Balance invariant enforcement (`totalTrackedBalance == USDC.balanceOf(vault)`)
- Configurable deposit cap per agent

### AgioBatchSettlement

The core innovation — processes hundreds of agent payments in a single on-chain transaction.

- Atomic batch processing (all-or-nothing)
- ECDSA batch hash verification (API signs batch, contract verifies signature)
- Replay protection (each payment ID can only be used once)
- Maximum batch value cap ($50K default)
- Per-submitter rate limiting (60 batches/hour)
- Calls `enforceInvariant()` after every batch

### AgioRegistry

On-chain agent identity with automatic tier upgrades based on payment history.

- Agent registration with unique AGIO ID
- Auto-tier: NEW → ACTIVE (100+ payments) → VERIFIED (1,000+) → TRUSTED (10,000+)
- Payment stats updated by batch settlement contract
- Anti-spam registration fee (configurable, 0 for testnet)

### MockUSDC

Testnet-only ERC-20 with 6 decimals and public mint. Not for mainnet use.

## Security Features

| Feature | Contract | Description |
|---|---|---|
| UUPS Upgradeable | All | Proxy pattern with `UPGRADER_ROLE` protection |
| Reentrancy Guard | Vault, Batch | Prevents reentrancy attacks |
| Pausable | All | Emergency stop capability |
| Access Control | All | Role-based permissions (admin, pauser, submitter, settlement) |
| CEI Pattern | Vault | State updated before external calls |
| Batch Signatures | Batch | ECDSA verification prevents payment tampering |
| Balance Invariant | Vault + Batch | Books must balance after every settlement |
| Circuit Breaker | Vault | Auto-pauses on abnormal outflows |
| Withdrawal Delays | Vault | Tiered delays for large withdrawals |
| Rate Limiting | Batch | Max batches per hour per submitter |
| Replay Protection | Batch | Payment IDs can only settle once |
| Spam Protection | Registry | Configurable registration fee |

## Development

```bash
# Install Foundry
curl -L https://foundry.paradigm.xyz | bash
foundryup

# Clone and build
git clone https://github.com/agio-protocol/agio-contracts.git
cd agio-contracts
forge install
forge build

# Run tests (21 tests)
forge test

# Run tests with gas reporting
forge test -vvv

# Deploy to local Anvil fork
anvil --fork-url https://sepolia.base.org --chain-id 84532 --port 8545
PRIVATE_KEY=0x... forge script script/DeployAll.s.sol --rpc-url http://localhost:8545 --broadcast
```

## Test Coverage

| Test Suite | Tests | Status |
|---|---|---|
| AgioVault | 10 | All passing |
| AgioBatchSettlement | 11 | All passing |
| **Total** | **21** | **All passing** |

Key tests:
- Single and 100-payment batch settlement
- Batch hash signature verification (valid, invalid, and tampered)
- Balance invariant holds after batch settlement
- Insufficient balance reverts entire batch
- Duplicate payment ID rejection
- Unauthorized submitter rejection
- Gas benchmark: 100 payments ≈ 4M gas (~$0.04 on Base)

## Gas Benchmarks

| Operation | Gas | Approx. Cost (Base) |
|---|---|---|
| Single payment batch | ~410K | $0.004 |
| 100 payment batch | ~4M | $0.04 |
| Per-payment cost (in 100-batch) | ~40K | **$0.0004** |

## Links

- Website: [agiotage.finance](https://agiotage.finance)
- SDK: [github.com/agio-protocol/agio-sdk](https://github.com/agio-protocol/agio-sdk)
- Docs: [github.com/agio-protocol/agio-docs](https://github.com/agio-protocol/agio-docs)
- X: [@agiofinance](https://twitter.com/agiofinance)

## License

MIT — see [LICENSE](LICENSE)

---

Built by AGIO Contributors.
