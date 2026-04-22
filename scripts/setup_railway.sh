#!/usr/bin/env bash
set -eo pipefail

# ============================================================
# AGIO Railway Setup — One-time production deployment
# ============================================================
# Prerequisites:
#   1. Install Railway CLI: npm install -g @railway/cli
#   2. Login: railway login
#   3. Have your private keys ready (from macOS Keychain)
# ============================================================

echo "========================================"
echo "  AGIO — Railway Production Setup"
echo "========================================"

# Check Railway CLI
if ! command -v railway &> /dev/null; then
    echo "Railway CLI not installed. Run: npm install -g @railway/cli"
    exit 1
fi

# Check login
railway whoami 2>/dev/null || {
    echo "Not logged in to Railway. Run: railway login"
    exit 1
}

echo ""
echo "This will create:"
echo "  1. Railway project: agio-protocol"
echo "  2. PostgreSQL database"
echo "  3. Redis instance"
echo "  4. API service (from Dockerfile)"
echo "  5. Worker services (batch, reconciler, oracle)"
echo ""
read -p "Continue? (y/N): " CONFIRM
if [ "$CONFIRM" != "y" ]; then echo "Aborted."; exit 0; fi

# Step 1: Create project
echo ""
echo "[1/6] Creating Railway project..."
cd /Users/jeffreywylie/agio-protocol/service
railway init --name agio-protocol 2>/dev/null || echo "  (project may already exist)"

# Step 2: Add PostgreSQL
echo "[2/6] Adding PostgreSQL..."
echo "  Go to Railway dashboard → Add service → Database → PostgreSQL"
echo "  Railway will provide DATABASE_URL automatically."
echo ""
read -p "Press Enter after adding PostgreSQL in the Railway dashboard..."

# Step 3: Add Redis
echo "[3/6] Adding Redis..."
echo "  Go to Railway dashboard → Add service → Database → Redis"
echo "  Railway will provide REDIS_URL automatically."
echo ""
read -p "Press Enter after adding Redis in the Railway dashboard..."

# Step 4: Set environment variables (secrets)
echo "[4/6] Setting environment variables..."
echo "  Reading keys from macOS Keychain..."

DEPLOYER_KEY=$(python3 -c "import keyring; v=keyring.get_password('agio-protocol','DEPLOYER_PRIVATE_KEY'); print(v if v and not v.startswith('0x') else (v or ''))")
SIGNER_KEY=$(python3 -c "import keyring; v=keyring.get_password('agio-protocol','BATCH_SIGNER_PRIVATE_KEY'); print(v if v and not v.startswith('0x') else (v or ''))")
SUBMITTER_KEY=$(python3 -c "import keyring; v=keyring.get_password('agio-protocol','BATCH_SUBMITTER_PRIVATE_KEY'); print(v if v and not v.startswith('0x') else (v or ''))")

echo "  Setting non-secret config..."
railway variables set \
    RPC_URL="https://mainnet.base.org" \
    VAULT_ADDRESS="0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc" \
    BATCH_SETTLEMENT_ADDRESS="0x3937a057AE18971657AD12830964511B73D9e7C5" \
    REGISTRY_ADDRESS="0xEfC4166Fc14758bAE879Bf439848Cb26E8f74927" \
    SWAP_ROUTER_ADDRESS="0x3428833a0E578Fb0BF9bE6Db45F36B99476949d8" \
    ALERT_EMAIL="jeffrey_wylie@yahoo.com" \
    API_SECRET_KEY="$(openssl rand -hex 32)" \
    BATCH_INTERVAL_SECONDS="60" \
    MAX_BATCH_SIZE="500" \
    2>/dev/null

echo "  Setting secrets (private keys)..."
railway variables set \
    BATCH_SIGNER_PRIVATE_KEY="$SIGNER_KEY" \
    BATCH_SUBMITTER_PRIVATE_KEY="$SUBMITTER_KEY" \
    2>/dev/null

# Clear from shell
unset DEPLOYER_KEY SIGNER_KEY SUBMITTER_KEY

echo "  Environment variables set."

# Step 5: Deploy
echo ""
echo "[5/6] Deploying to Railway..."
railway up --detach

echo ""
echo "[6/6] Getting deployment URL..."
sleep 10
railway domain 2>/dev/null || echo "  Set custom domain in Railway dashboard: api.agiotage.finance"

echo ""
echo "========================================"
echo "  DEPLOYMENT STARTED"
echo "========================================"
echo ""
echo "  Next steps:"
echo "  1. Check Railway dashboard for deploy status"
echo "  2. Add custom domain: api.agiotage.finance"
echo "  3. Run database migration (tables auto-create on first boot)"
echo "  4. Migrate data: python3 scripts/migrate_to_railway.py"
echo "  5. Update dashboard API URL to Railway URL"
echo "  6. Set up UptimeRobot monitoring"
echo "========================================"
