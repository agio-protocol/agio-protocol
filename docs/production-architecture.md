# AGIO Protocol — Production Deployment Architecture

**Target: 99.99% uptime (52 minutes downtime/year maximum)**

---

## System Overview

```
                    ┌─────────────────────────────────┐
                    │          CLOUDFLARE              │
                    │  DDoS protection + SSL + CDN     │
                    │  agiotage.finance                │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │        NGINX LOAD BALANCER       │
                    │  Health checks every 10s          │
                    │  Auto-remove unhealthy instances  │
                    │  Rate limiting (100 req/min/IP)   │
                    └──┬──────────┬──────────┬────────┘
                       │          │          │
              ┌────────▼───┐ ┌───▼────────┐ ┌▼───────────┐
              │  API-1     │ │  API-2     │ │  API-3     │
              │  FastAPI   │ │  FastAPI   │ │  FastAPI   │
              │  :8001     │ │  :8002     │ │  :8003     │
              └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
                    │              │              │
       ┌────────────┴──────────────┴──────────────┴──────┐
       │                                                  │
┌──────▼───────┐                                  ┌───────▼────────┐
│  PgBouncer   │                                  │     REDIS      │
│  Connection  │                                  │   Sentinel     │
│  Pooler      │                                  │   Cluster      │
│  :6432       │                                  │                │
└──────┬───────┘                                  │  ┌──────────┐  │
       │                                          │  │ Queue    │  │
┌──────▼───────────────────────┐                  │  │ (AOF)   │  │
│     PostgreSQL Cluster       │                  │  │ :6379   │  │
│                              │                  │  ├──────────┤  │
│  ┌─────────┐  ┌──────────┐  │                  │  │ Cache   │  │
│  │ Primary │  │ Replica1 │  │                  │  │ :6380   │  │
│  │  (RW)   │──│  (RO)    │  │                  │  ├──────────┤  │
│  │ :5432   │  │ :5433    │  │                  │  │ Session │  │
│  └────┬────┘  └──────────┘  │                  │  │ :6381   │  │
│       │       ┌──────────┐  │                  │  └──────────┘  │
│       │       │ Replica2 │  │                  │                │
│       └───────│  (RO)    │  │                  │  Sentinel×3    │
│               │ :5434    │  │                  │  :26379-26381  │
│               └──────────┘  │                  └────────────────┘
│                              │
│  WAL archiving → S3/GCS     │
│  Patroni for auto-failover  │
│  PITR: 30 days retention    │
└──────────────────────────────┘

       ┌──────────────────────────────────────────────┐
       │              WORKER LAYER                     │
       │                                              │
       │  ┌─────────────────┐  ┌─────────────────┐   │
       │  │  Batch Worker   │  │  Batch Worker   │   │
       │  │  (ACTIVE)       │  │  (PASSIVE)      │   │
       │  │  Holds dist.    │  │  Monitors lock  │   │
       │  │  lock in Redis  │  │  Takes over on  │   │
       │  │                 │  │  3 missed beats │   │
       │  └────────┬────────┘  └─────────────────┘   │
       │           │                                  │
       │  ┌────────▼────────┐                         │
       │  │  Reconciliation │  Runs every 5 min       │
       │  │  Service        │  Compares off-chain     │
       │  │                 │  vs on-chain state      │
       │  │  CRITICAL       │  Auto-pauses on         │
       │  │  SAFETY CHECK   │  discrepancy            │
       │  └─────────────────┘                         │
       │                                              │
       │  ┌─────────────────┐                         │
       │  │  Dead Letter    │  Payments that fail     │
       │  │  Queue (DLQ)    │  3 times go here for    │
       │  │                 │  manual review           │
       │  └─────────────────┘                         │
       └──────────────────────────────────────────────┘

       ┌──────────────────────────────────────────────┐
       │           MONITORING & ALERTING               │
       │                                              │
       │  Prometheus → Grafana dashboards             │
       │  Structured JSON logs → Loki / CloudWatch    │
       │                                              │
       │  ALERTS:                                     │
       │  • Component unhealthy           → PagerDuty │
       │  • Batch settlement > 5 min      → PagerDuty │
       │  • Balance invariant FAIL        → PagerDuty │
       │  • Off-chain/on-chain mismatch   → PagerDuty │
       │  • Reserve below threshold       → Slack     │
       │  • Error rate > 1%               → Slack     │
       │  • Redis queue depth > 10,000    → Slack     │
       └──────────────────────────────────────────────┘

       ┌──────────────────────────────────────────────┐
       │           BASE BLOCKCHAIN                     │
       │                                              │
       │  AgioVault          ← Holds all USDC         │
       │  AgioBatchSettlement← Processes batches      │
       │  AgioRegistry       ← Agent identities       │
       │                                              │
       │  RPC: Alchemy / QuickNode (redundant)        │
       │  Fallback: public Base RPC                   │
       └──────────────────────────────────────────────┘
```

---

## Component Specifications

### 1. PostgreSQL Cluster

**Configuration:**
- Primary + 2 streaming replicas
- Patroni for automatic leader election and failover
- PgBouncer for connection pooling (max 1,000 concurrent)
- WAL archiving to S3 for point-in-time recovery (30 days)

**Failover process:**
1. Patroni detects primary is unhealthy (3 missed health checks, 10s interval)
2. Patroni promotes the most caught-up replica to primary (< 5 seconds)
3. PgBouncer redirects all connections to new primary
4. Old primary restarts as replica when it recovers
5. Alert sent to ops team

**Backup schedule:**
- Continuous WAL streaming to S3
- Full base backup daily at 03:00 UTC
- Point-in-time recovery to any second in last 30 days
- Monthly backup restoration test (automated)

### 2. Redis Cluster

**Three separate instances (isolation prevents cross-contamination):**

| Instance | Port | Persistence | Purpose | Data Loss Impact |
|---|---|---|---|---|
| Queue | 6379 | AOF (fsync/sec) | Payment queue | **Critical** — payments lost |
| Cache | 6380 | None | Rate limiting, hot data | Low — rebuilt in seconds |
| Session | 6381 | RDB (every 5 min) | API sessions | Low — agents re-auth |

**Sentinel configuration:**
- 3 Sentinel nodes monitoring all Redis instances
- Quorum: 2 (majority required to trigger failover)
- Failover time: < 30 seconds
- Queue instance uses AOF with `appendfsync everysec` — max 1 second of data loss

### 3. API Layer

**3 FastAPI instances behind nginx:**

```nginx
upstream agio_api {
    server api-1:8001 max_fails=2 fail_timeout=30s;
    server api-2:8002 max_fails=2 fail_timeout=30s;
    server api-3:8003 max_fails=2 fail_timeout=30s;
}

server {
    location /v1/ {
        proxy_pass http://agio_api;
        proxy_next_upstream error timeout http_502 http_503;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
    }

    location /v1/health {
        proxy_pass http://agio_api;
        access_log off;
    }
}
```

**Health check contract:**
- Endpoint: `GET /v1/health`
- Checks: database connectivity, Redis connectivity, RPC reachability
- nginx polls every 10 seconds
- 2 consecutive failures → instance removed from rotation
- 1 success → instance restored to rotation

**Graceful shutdown:**
- SIGTERM → stop accepting new requests
- Finish all in-flight requests (30 second timeout)
- Close database connections
- Exit

### 4. Batch Workers

**Active-passive with distributed lock:**

```
Worker-1 (Active):
  1. Acquire Redis lock: AGIO:batch_worker_lock (TTL: 30s)
  2. Renew lock every 10s (heartbeat)
  3. Pull payments from queue → assemble batch → submit on-chain
  4. On crash: lock expires after 30s

Worker-2 (Passive):
  1. Try to acquire lock every 5s
  2. If lock acquired (Worker-1 missed 3 heartbeats): become active
  3. Recovery: check last batch on-chain, reconcile DB, resume
```

**Dead letter queue:**
- Payment fails validation → retry with exponential backoff (1s, 5s, 30s)
- After 3 failures → move to DLQ (Redis list: `AGIO:dead_letter_queue`)
- DLQ reviewed by ops team daily
- Alert if DLQ depth > 10

**Recovery after crash:**
1. New active worker starts
2. Checks last known batch_id in PostgreSQL
3. Queries on-chain: was this batch settled?
4. If settled but DB not updated → catch up DB state
5. If not settled → resubmit batch (contract rejects duplicates via payment ID)
6. Resume normal processing

### 5. Monitoring & Alerting

**Metrics (Prometheus):**
- `agio_payments_queued` — current queue depth
- `agio_payments_settled_total` — counter of settled payments
- `agio_batch_settlement_duration_seconds` — histogram
- `agio_balance_invariant_ok` — gauge (1 = ok, 0 = violation)
- `agio_reconciliation_ok` — gauge (1 = ok, 0 = mismatch)
- `agio_api_request_duration_seconds` — histogram by endpoint
- `agio_api_errors_total` — counter by status code
- `agio_vault_total_balance_usd` — gauge
- `agio_active_agents` — gauge

**Alert rules:**

| Alert | Condition | Severity | Channel |
|---|---|---|---|
| Component down | Health check fails 3x | **P1** | PagerDuty |
| Balance invariant | `agio_balance_invariant_ok == 0` | **P1** | PagerDuty |
| Reconciliation mismatch | `agio_reconciliation_ok == 0` | **P1** | PagerDuty |
| Batch stuck | Settlement > 5 minutes | **P2** | PagerDuty |
| High error rate | > 1% of requests are 5xx | **P2** | Slack |
| Queue backlog | Queue depth > 10,000 | **P3** | Slack |
| Reserve low | Vault balance < $1,000 | **P3** | Slack |
| DLQ growing | Dead letter queue > 10 | **P3** | Slack |
| Worker failover | Passive worker took over | **P3** | Slack |

### 6. Reconciliation Service (Critical)

**The ultimate safety check. If off-chain and on-chain disagree, everything stops.**

Runs every 5 minutes. Compares:

| Check | Off-chain (PostgreSQL) | On-chain (Smart Contract) |
|---|---|---|
| Total vault balance | `SUM(balance + locked_balance)` from agents table | `vault.checkInvariant()` |
| Individual agent balances | `agents.balance` for sampled agents | `vault.balanceOf(agent)` |
| Batch settlement status | `batches.status` for recent batches | `batch.getBatchDetails(id)` |
| Payment dedup | `payments.payment_id` | `batch.isPaymentProcessed(id)` |

**If ANY check fails:**
1. Pause all payment processing immediately
2. Log full discrepancy details (off-chain value, on-chain value, delta)
3. Send P1 alert to PagerDuty
4. Post to #incidents Slack channel
5. Do NOT attempt to auto-fix — wait for human investigation

**Why no auto-fix:** If the books don't balance, something unexpected happened (bug, exploit, race condition). Auto-fixing could make it worse. The correct action is always: stop, preserve evidence, investigate.

---

## Deployment Environments

| Environment | Purpose | Infrastructure |
|---|---|---|
| **Local** | Development | Docker compose, single instances |
| **Staging** | Pre-production testing | Same topology as prod, testnet contracts |
| **Production** | Live traffic | Full HA setup, mainnet contracts |

## Deployment Targets

| Component | Recommended Provider | Alternative |
|---|---|---|
| Compute | Railway / Fly.io | AWS ECS / GCP Cloud Run |
| PostgreSQL | Neon / Supabase (managed) | AWS RDS |
| Redis | Upstash (managed) | AWS ElastiCache |
| RPC | Alchemy (primary) + QuickNode (fallback) | Infura |
| Monitoring | Grafana Cloud | Datadog |
| Alerting | PagerDuty + Slack | OpsGenie |
| Secrets | Doppler / 1Password | AWS Secrets Manager |
| CDN/WAF | Cloudflare | AWS CloudFront |

## Capacity Planning

| Metric | Day 1 | 10K agents | 100K agents |
|---|---|---|---|
| Payments/day | 1,000 | 100,000 | 5,000,000 |
| Batches/day | 24 | 2,400 | 100,000 |
| API requests/sec | 1 | 50 | 500 |
| PostgreSQL connections | 20 | 200 | 1,000 |
| Redis queue depth (peak) | 100 | 5,000 | 50,000 |
| Storage (PostgreSQL) | 1 GB | 50 GB | 500 GB |
| Monthly infra cost | $50 | $500 | $5,000 |

---

## Multi-sig Role Assignment (Pre-Mainnet Checklist)

Before deploying to mainnet, transfer contract roles to Gnosis Safe multisigs:

| Role | Current | Production Target | Threshold |
|---|---|---|---|
| `DEFAULT_ADMIN_ROLE` | Deployer EOA | 3-of-5 multisig (founders) | High |
| `UPGRADER_ROLE` | Deployer EOA | 4-of-5 multisig + 48hr timelock | Highest |
| `PAUSER_ROLE` | Deployer EOA | 2-of-3 multisig (ops) | Fast response |
| `BATCH_SUBMITTER_ROLE` | Deployer EOA | Batch worker service account | Automated |
| `SETTLEMENT_ROLE` | BatchSettlement contract | BatchSettlement contract | Immutable |
| `batchSigner` | Deployer EOA | Dedicated signing key (HSM) | Automated |

---

*Document version: 1.0 — AGIO Contributors*
