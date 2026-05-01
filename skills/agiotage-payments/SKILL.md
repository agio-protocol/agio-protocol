# Agiotage Cross-Chain Payments

Pay any AI agent instantly across Base and Solana. $0.001 same-chain, $0.002 cross-chain. Non-custodial smart contracts.

## Endpoints

- `POST /v1/pay` — Send payment to another agent ($0.001)
- `GET /v1/balances/{agio_id}` — Check balance (free)
- `POST /v1/register` — Register new agent (free)

## Pricing

| Route | Price |
|-------|-------|
| Same-chain payment | $0.001 |
| Cross-chain (Base↔Solana) | $0.002 |

## Example

```bash
POST https://agio-protocol-production.up.railway.app/v1/pay
Content-Type: application/json

{
  "from_agio_id": "0xYOUR_ID",
  "to_agio_id": "agio:sol:0xRECIPIENT",
  "amount": 0.50,
  "token": "USDC"
}
```

## Features

- Non-custodial smart contracts on Base and Solana
- Batch settlement for gas efficiency
- 5 fee tiers (SPARK to NEXUS) — up to 80% discount
- Verified on Basescan and Solscan

## Links

- Website: https://agiotage.finance
- API Docs: https://agiotage.finance/docs.html
- MCP Server: `npx agiotage-mcp`
- SDK: `pip install agiotage-sdk`
