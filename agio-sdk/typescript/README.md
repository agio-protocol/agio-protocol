# AGIO SDK

> Instant, sub-cent payments between AI agents across any blockchain.

[![npm](https://img.shields.io/badge/npm-coming%20soon-yellow)](https://www.npmjs.com/package/@agio/sdk)
[![PyPI](https://img.shields.io/badge/pypi-coming%20soon-yellow)](https://pypi.org/project/agio-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AGIO is a cross-chain micropayment routing protocol for AI agents. Send $0.001 across chains in under 1 second. 3 lines of code.

## Install

```bash
pip install agio-sdk
```

```bash
npm install @agio/sdk
```

## Quick Start

```python
from agio import Agent

# Initialize your agent
agent = Agent("your-api-key")

# Pay another agent — any chain, any amount
receipt = agent.pay(
    to="agent_0x7a3f...base",
    amount="0.005",    # $0.005 USDC
    memo="API call: sentiment analysis"
)

# Check balance across all chains
balance = agent.balance()
# {"base": 12.50, "solana": 3.20, "polygon": 0.75}

# Request payment from another agent
invoice = agent.request(
    from_agent="agent_0x9b2c...solana",
    amount="0.002",
    service="data-enrichment"
)
```

## Supported Chains

| Chain | Status | Gas Cost | Settlement |
|---|---|---|---|
| **Base** | Live | ~$0.00001 | <1 second |
| **Solana** | Live | ~$0.00025 | <1 second |
| **Polygon** | Coming Q1 2027 | ~$0.001 | <2 seconds |
| **Arbitrum** | Coming Q1 2027 | ~$0.001 | <2 seconds |
| **Ethereum** | Via batching | ~$0.01 (batched) | <5 minutes |

## Features

- **Sub-cent payments**: Send $0.001 economically. Gas costs are negligible on L2s.
- **Cross-chain routing**: Pay an agent on Solana from your Base wallet. AGIO handles the routing.
- **Batch settlement**: Micropayments are batched to amortize gas costs across transactions.
- **Agent identity**: On-chain reputation that builds with every successful transaction.
- **x402 compatible**: Works alongside Coinbase's HTTP 402 payment standard.
- **No KYC required**: Designed for autonomous agents, not humans.

## Fee Structure

| Transaction Size | AGIO Fee | Total Cost (Base) |
|---|---|---|
| < $0.01 (micropayment) | $0.0001 flat | ~$0.00011 |
| $0.01 - $1.00 | 0.05% | ~$0.0005 - $0.0006 |
| > $1.00 | 0.05% | $0.0005 + 0.05% of amount |

## Documentation

Full docs: [docs.agiotage.finance](https://docs.agiotage.finance)

## Links

- Website: [agiotage.finance](https://agiotage.finance)
- GitHub: [github.com/agio-protocol](https://github.com/agio-protocol)
- Twitter: [@agiofinance](https://twitter.com/agiofinance)
- Discord: [discord.gg/agio](https://discord.gg/agio)

## License

MIT
