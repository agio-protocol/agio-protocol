#!/usr/bin/env bash
set -eo pipefail

# ============================================================
# AGIO Protocol — Base Mainnet Deployment
# ============================================================
# Keys are read from macOS Keychain (never from files).
# Run setup first: python3 scripts/setup_keys.py
#
# Usage:
#   ./scripts/deploy_mainnet.sh              # dry run
#   ./scripts/deploy_mainnet.sh --broadcast  # LIVE deployment
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONTRACTS_DIR="$ROOT_DIR/contracts"
ENV_FILE="$SCRIPT_DIR/.env.mainnet"

echo "========================================"
echo "  AGIO MAINNET DEPLOYMENT"
echo "========================================"

# Load non-secret config from .env.mainnet
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

# Read secrets from macOS Keychain
echo "Reading keys from macOS Keychain..."

DEPLOYER_PRIVATE_KEY=$(python3 -c "
import keyring
val = keyring.get_password('agio-protocol', 'DEPLOYER_PRIVATE_KEY')
if not val:
    raise SystemExit('DEPLOYER_PRIVATE_KEY not in Keychain. Run: python3 scripts/setup_keys.py')
# Forge requires 0x prefix
if not val.startswith('0x'):
    val = '0x' + val
print(val)
")

BATCH_SIGNER_ADDRESS=$(python3 -c "
import keyring
val = keyring.get_password('agio-protocol', 'BATCH_SIGNER_ADDRESS')
if not val:
    raise SystemExit('BATCH_SIGNER_ADDRESS not in Keychain. Run: python3 scripts/setup_keys.py')
print(val)
")

FEE_COLLECTOR_ADDRESS=$(python3 -c "
import keyring
val = keyring.get_password('agio-protocol', 'FEE_COLLECTOR_ADDRESS')
print(val or '')
")

DEPLOYER_ADDRESS=$(python3 -c "
import keyring
val = keyring.get_password('agio-protocol', 'DEPLOYER_ADDRESS')
if not val:
    raise SystemExit('DEPLOYER_ADDRESS not in Keychain. Run: python3 scripts/setup_keys.py')
print(val)
")

echo "Deployer:       $DEPLOYER_ADDRESS"
echo "Batch signer:   $BATCH_SIGNER_ADDRESS"
echo "Fee collector:  $FEE_COLLECTOR_ADDRESS"
echo "RPC:            $BASE_RPC_URL"
echo "Chain ID:       $BASE_CHAIN_ID"
echo ""

# Check if --broadcast flag is passed
BROADCAST_FLAG=""
if [[ "${1:-}" == "--broadcast" ]]; then
    echo "========================================"
    echo "  WARNING: LIVE MAINNET DEPLOYMENT"
    echo "  This will deploy real contracts and"
    echo "  spend real ETH on Base mainnet."
    echo "========================================"
    echo ""
    read -p "Type 'DEPLOY' to confirm: " CONFIRM
    if [ "$CONFIRM" != "DEPLOY" ]; then
        echo "Aborted."
        exit 0
    fi
    BROADCAST_ARGS=(--broadcast --verify --etherscan-api-key "${BASESCAN_API_KEY}")
else
    echo "DRY RUN — add --broadcast for live deployment"
    echo ""
    BROADCAST_ARGS=()
fi

# Run pre-deployment checks
echo "Running pre-deployment checks..."
python3 "$SCRIPT_DIR/pre_deploy_check.py" --network mainnet
PRE_CHECK_EXIT=$?
if [ $PRE_CHECK_EXIT -ne 0 ]; then
    echo ""
    echo "ERROR: Pre-deployment checks failed."
    exit 1
fi

echo ""
echo "Deploying contracts..."
echo ""

cd "$CONTRACTS_DIR"

# Build the forge command explicitly
FORGE_CMD="PRIVATE_KEY=$DEPLOYER_PRIVATE_KEY $HOME/.foundry/bin/forge script script/DeployAll.s.sol --rpc-url $BASE_RPC_URL --chain-id $BASE_CHAIN_ID -vvv"

if [[ "${1:-}" == "--broadcast" ]]; then
    FORGE_CMD="$FORGE_CMD --broadcast --verify --etherscan-api-key $BASESCAN_API_KEY"
fi

eval "$FORGE_CMD" 2>&1 | tee /tmp/agio_deploy_output.txt

# Clear the key from the environment immediately
unset DEPLOYER_PRIVATE_KEY

# Extract deployed addresses from output
if [[ "${1:-}" == "--broadcast" ]]; then
    echo ""
    echo "Extracting deployed addresses..."

    VAULT=$(grep "DEPLOYED_VAULT=" /tmp/agio_deploy_output.txt | awk -F'= ' '{print $2}' | tr -d ' ')
    BATCH=$(grep "DEPLOYED_BATCH=" /tmp/agio_deploy_output.txt | awk -F'= ' '{print $2}' | tr -d ' ')
    REGISTRY=$(grep "DEPLOYED_REGISTRY=" /tmp/agio_deploy_output.txt | awk -F'= ' '{print $2}' | tr -d ' ')
    SWAP_ROUTER=$(grep "DEPLOYED_SWAP_ROUTER=" /tmp/agio_deploy_output.txt | awk -F'= ' '{print $2}' | tr -d ' ')

    # Write deployed addresses JSON (no secrets)
    cat > "$SCRIPT_DIR/deployed_addresses.json" << EOFADDR
{
    "network": "base-mainnet",
    "chain_id": 8453,
    "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "contracts": {
        "vault": "$VAULT",
        "batch_settlement": "$BATCH",
        "registry": "$REGISTRY",
        "swap_router": "$SWAP_ROUTER"
    },
    "tokens": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
        "WETH": "0x4200000000000000000000000000000000000006",
        "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"
    },
    "dex_router": "0x2626664c2603336E57B271c5C0b26F421741e481"
}
EOFADDR

    # Clean up forge output (may contain key in logs)
    rm -f /tmp/agio_deploy_output.txt

    echo ""
    echo "========================================"
    echo "  DEPLOYMENT COMPLETE"
    echo "========================================"
    echo "  Addresses: scripts/deployed_addresses.json"
    echo ""
    echo "  Next:"
    echo "  1. python3 scripts/post_deploy_setup.py"
    echo "  2. python3 scripts/monitor.py"
    echo "========================================"
fi
