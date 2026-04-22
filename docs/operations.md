# AGIO Operations Runbook

Everything you need to run AGIO in production. Written for a non-engineer.

## Architecture

```
agiotage.finance (Netlify) ──→ api.agiotage.finance (Railway)
                                    │
                    ┌────────────────┼────────────────┐
                    │                │                │
              PostgreSQL          Redis         Base mainnet
              (Railway)         (Railway)        (RPC)
```

**Railway runs 4 processes:**
1. **API server** — handles all requests from dashboards and SDK
2. **Batch worker** — settles payments on-chain every 120 seconds
3. **Reconciler** — checks books balance every 5 minutes, emails you if not
4. **Oracle loop** — generates test volume ($0.001/minute)

---

## Daily Operations

### Check the admin dashboard
1. Open admin.agiotage.finance (or localhost:3000)
2. Enter API key: `agio-admin-2026`
3. Check: is reconciliation green? Are transactions flowing? Is queue depth low?

### If you get an alert email
1. Open admin dashboard → Reconciliation page
2. If "PAUSED": the system stopped accepting payments because something is wrong
3. Check the specific failure in the alert
4. Common causes:
   - RPC rate limit (429 error) → transient, will self-heal
   - Balance mismatch → investigate manually
   - Database connection lost → check Railway dashboard

---

## How to Deploy Updates

### API (automatic)
```bash
cd ~/agio-protocol
git add -A
git commit -m "description of change"
git push origin main
```
Railway auto-deploys on push to main. Takes ~2 minutes.
Check Railway dashboard to confirm deploy succeeded.

### Dashboards (manual)
```bash
# Update agent dashboard on Netlify
cd ~/agio-protocol/landing-page
cp ../dashboard/agent/index.html dashboard/index.html
netlify deploy --prod --dir=.

# Update admin dashboard on Netlify  
cd ~/agio-protocol/dashboard/admin
netlify deploy --prod --dir=.
```

---

## How to Restart Services

### On Railway
1. Go to railway.app → AGIO project
2. Click the service that needs restarting
3. Click "Redeploy" (top right)
4. Wait ~2 minutes for it to come back up

### Emergency: restart everything
1. Railway dashboard → Settings → Redeploy all services

---

## How to Check Logs

### Railway
1. Go to railway.app → AGIO project
2. Click any service (web, worker, reconciler, oracle)
3. Click "Logs" tab
4. Search for errors with the search bar

### On your local machine (during observation)
```bash
tail -50 /tmp/agio_batch_worker.log
tail -50 /tmp/agio_reconciliation.log
tail -50 /tmp/agio_oracle_loop.log
tail -50 /tmp/agio_api.log
```

---

## How to Run Database Migrations

If you change the database schema (add a column, new table):

```bash
# The API auto-creates tables on startup in dev mode.
# For production, Railway runs migrations on deploy.
# If needed manually:
railway run python -c "
from src.models.base import Base
from src.core.database import engine
import asyncio
async def migrate():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
asyncio.run(migrate())
"
```

---

## How to Pause the Protocol

### Emergency pause (stops all payments immediately)
```bash
# Via Redis on Railway:
railway run python -c "
import redis
r = redis.from_url('REDIS_URL_HERE')
r.set('AGIO:payments_paused', '1')
r.set('AGIO:pause_reason', 'Manual pause by operator')
print('PAUSED')
"
```

### On-chain pause (stops vault deposits/withdrawals)
```bash
# Using cast from your local machine:
DEPLOYER_KEY=$(python3 -c "import keyring; v=keyring.get_password('agio-protocol','DEPLOYER_PRIVATE_KEY'); print(v if v.startswith('0x') else '0x'+v)")
~/.foundry/bin/cast send 0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc \
    "pause()" --rpc-url https://mainnet.base.org --private-key "$DEPLOYER_KEY"
```

### Unpause
```bash
# Redis:
railway run python -c "
import redis
r = redis.from_url('REDIS_URL_HERE')
r.delete('AGIO:payments_paused')
r.delete('AGIO:pause_reason')
print('UNPAUSED')
"

# On-chain:
~/.foundry/bin/cast send 0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc \
    "unpause()" --rpc-url https://mainnet.base.org --private-key "$DEPLOYER_KEY"
```

---

## How to Add a New Whitelisted Token

### On-chain (permanent)
```bash
DEPLOYER_KEY=$(python3 -c "import keyring; v=keyring.get_password('agio-protocol','DEPLOYER_PRIVATE_KEY'); print(v if v.startswith('0x') else '0x'+v)")
TOKEN_ADDRESS=0x...  # the token's contract address on Base

~/.foundry/bin/cast send 0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc \
    "addWhitelistedToken(address)" $TOKEN_ADDRESS \
    --rpc-url https://mainnet.base.org --private-key "$DEPLOYER_KEY"
```

### Off-chain (update the code)
1. Add the token to `service/src/models/chain.py` in `BASE_TOKENS`
2. Add to `service/src/services/payment_service.py` in `SUPPORTED_TOKENS`
3. Push to GitHub → auto-deploys

---

## How to Adjust Tier Parameters

Tier thresholds are in the database (fee_tiers table). To change:

```bash
railway run python -c "
from src.models.loyalty import FeeTier
from src.core.database import async_session
from sqlalchemy import select, update
import asyncio

async def update_tier():
    async with async_session() as db:
        # Example: change ARC minimum transactions from 100 to 50
        await db.execute(
            update(FeeTier)
            .where(FeeTier.tier_name == 'ARC')
            .values(min_lifetime_txns=50)
        )
        await db.commit()
        print('ARC tier updated')

asyncio.run(update_tier())
"
```

---

## How to Handle a Reconciliation Failure

1. **Don't panic.** The system paused itself automatically.
2. Open admin dashboard → Reconciliation page
3. Read the specific failure message
4. Common scenarios:

**"On-chain invariant failed"**
- The vault's tracked balance doesn't match actual tokens held
- This is serious — means tokens entered/left vault outside normal flow
- Check Basescan for unexpected transactions to the vault address
- DO NOT unpause until you understand what happened

**"Off-chain vs on-chain mismatch"**
- Database says different total than blockchain
- Could be a failed batch that updated DB but not chain (or vice versa)
- Compare: `SELECT SUM(balance) + SUM(locked_balance) FROM agents` vs vault on-chain balance
- Fix the DB to match on-chain (on-chain is the source of truth)

**"Orphaned payments"**
- Payments stuck in BATCHED/SETTLING for >10 minutes
- The batch worker probably crashed during submission
- Check batch worker logs
- Payments will auto-retry when worker restarts

**"429 rate limit"**
- The free Base RPC is throttling us
- This is transient — will resolve on next check (5 min)
- Not a real failure. If it persists, upgrade to a paid RPC (Alchemy/QuickNode)

---

## How to Rotate Private Keys

### Batch signer key
1. Generate a new wallet
2. Update on-chain:
```bash
~/.foundry/bin/cast send 0x3937a057AE18971657AD12830964511B73D9e7C5 \
    "setBatchSigner(address)" NEW_SIGNER_ADDRESS \
    --rpc-url https://mainnet.base.org --private-key "$DEPLOYER_KEY"
```
3. Update Railway environment variable: `BATCH_SIGNER_PRIVATE_KEY`
4. Redeploy

### Deployer key
- This is the admin key for all contracts
- Rotating it requires transferring all admin roles to a new address
- This is a complex operation — plan it carefully

---

## Contract Addresses (Base Mainnet)

| Contract | Address |
|---|---|
| AgioVault | `0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc` |
| AgioBatchSettlement | `0x3937a057AE18971657AD12830964511B73D9e7C5` |
| AgioRegistry | `0xEfC4166Fc14758bAE879Bf439848Cb26E8f74927` |
| AgioSwapRouter | `0x3428833a0E578Fb0BF9bE6Db45F36B99476949d8` |

All verified on Basescan. View at:
https://basescan.org/address/0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc

---

## Monthly Costs (estimated)

| Service | Cost |
|---|---|
| Railway (API + workers) | ~$20-30 |
| Railway PostgreSQL | ~$10-15 |
| Railway Redis | ~$5-10 |
| Netlify (dashboards) | Free |
| Domain (Namecheap) | ~$12/year |
| UptimeRobot | Free |
| **Total** | **~$35-55/month** |

---

## Emergency Contacts

- Railway status: https://status.railway.app
- Base network status: https://status.base.org
- Netlify status: https://www.netlifystatus.com
