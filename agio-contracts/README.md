# AGIO Smart Contracts

> Payment channels, batch settlement, and identity registry for the AGIO protocol.

## Architecture

```
┌─────────────────────────────────────────────┐
│              AGIO Protocol Layer             │
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Payment  │  │  Batch   │  │ Identity │  │
│  │ Channels │  │ Settler  │  │ Registry │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       │              │              │        │
├───────┼──────────────┼──────────────┼────────┤
│       ▼              ▼              ▼        │
│  ┌─────────────────────────────────────┐     │
│  │         Chain Adapters              │     │
│  │  Base │ Solana │ Polygon │ Arb      │     │
│  └─────────────────────────────────────┘     │
└─────────────────────────────────────────────┘
```

### Payment Channels
Agents open payment channels for repeated transactions. Micropayments are tracked off-chain and settled periodically.

### Batch Settler
Aggregates hundreds of micropayments into a single on-chain settlement transaction. Reduces gas cost per payment by 10-100x.

### Identity Registry
On-chain record of agent identities, payment history, and reputation scores. Portable across chains.

## Contracts

| Contract | Chain | Address | Status |
|---|---|---|---|
| PaymentChannel | Base Sepolia | `TBD` | Testnet deployment coming soon |
| BatchSettler | Base Sepolia | `TBD` | Testnet deployment coming soon |
| IdentityRegistry | Base Sepolia | `TBD` | Testnet deployment coming soon |

## Development

```bash
# Install dependencies
forge install

# Compile
forge build

# Test
forge test

# Deploy (testnet)
forge script script/Deploy.s.sol --rpc-url base-sepolia --broadcast
```

## Audit Status

**Pre-audit — do not use in production.** Formal audit planned for Q4 2026.

## License

MIT
