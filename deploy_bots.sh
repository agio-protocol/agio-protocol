#!/bin/bash
# Deploy copy trader and sniper as separate Railway services
# Run this ONCE on hotspot - minimal API calls

set -e

echo "=== VERIFYING RAILWAY AUTH ==="
railway whoami || { echo "NOT LOGGED IN - run: railway login"; exit 1; }

echo ""
echo "=== STEP 1: CREATE COPYTRADER SERVICE ==="
railway add --service copytrader
echo "Created copytrader"

echo ""
echo "=== STEP 2: SET COPYTRADER VARS ==="
railway service link copytrader
railway variables set \
  WORKER_CMD=copytrader \
  DATABASE_URL="postgresql://postgres:KuiiwrwZBsHLdvwqYKMdNaMQiHNxZujj@postgres.railway.internal:5432/railway" \
  GMGN_API_KEY="gmgn_9b11437b14ab87a06ffdf9bf654291f1" \
  HELIUS_API_KEY="bd6f8a27-49dc-4399-9777-c0a3350bc0a5" \
  COPY_TRADER_PRIVATE_KEY="wRTcCcH7Mz7vsXBaGuMM4njcD7dVymUHPxbuJ8frJB8aUp7vGsuapfNpfw7C1Xf5R5VDkdCy64QBm4iqXyeHxud" \
  TELEGRAM_BOT_TOKEN="8642733389:AAGVoCkGHhjaf1woCwwiGWvmwe8F5" \
  TELEGRAM_CHAT_ID="7722605903"
echo "Copytrader vars set"

echo ""
echo "=== STEP 3: DEPLOY COPYTRADER ==="
cd /Users/jeffreywylie/agio-protocol/service
railway up --detach
echo "Copytrader deploying"

echo ""
echo "=== STEP 4: CREATE SNIPER SERVICE ==="
cd /Users/jeffreywylie/agio-protocol
railway add --service sniper
echo "Created sniper"

echo ""
echo "=== STEP 5: SET SNIPER VARS ==="
railway service link sniper
railway variables set \
  WORKER_CMD=sniper \
  DATABASE_URL="postgresql://postgres:KuiiwrwZBsHLdvwqYKMdNaMQiHNxZujj@postgres.railway.internal:5432/railway" \
  GMGN_API_KEY="gmgn_9b11437b14ab87a06ffdf9bf654291f1" \
  TELEGRAM_BOT_TOKEN="8642733389:AAGVoCkGHhjaf1woCwwiGWvmwe8F5" \
  TELEGRAM_CHAT_ID="7722605903"
echo "Sniper vars set"

echo ""
echo "=== STEP 6: DEPLOY SNIPER ==="
cd /Users/jeffreywylie/agio-protocol/service
railway up --detach
echo "Sniper deploying"

echo ""
echo "=== STEP 7: VERIFY MAIN SERVICE NOT TOUCHED ==="
railway service link agio-protocol
railway variables 2>&1 | grep WORKER_CMD && echo "WARNING: WORKER_CMD on main service!" || echo "CLEAN - no WORKER_CMD on main service"

echo ""
echo "=== DONE - Switch back to WiFi ==="
echo "Both services are building. Check Railway dashboard for status."
