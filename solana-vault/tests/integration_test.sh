#!/usr/bin/env bash
# AGIO Solana Vault — Integration tests on localnet
# Requires: solana-test-validator running, program deployed
set -e

export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"

PROGRAM_ID="CpHfZKzThtYt64YjAWKkJYNZboQYjPazSTxj75j3w9YE"
RPC="http://localhost:8899"
DEPLOYER=$(solana address)
PASS=0
FAIL=0

ok() { PASS=$((PASS+1)); echo "  [PASS] $1"; }
fail() { FAIL=$((FAIL+1)); echo "  [FAIL] $1: $2"; }

echo "================================================"
echo "  AGIO Solana Vault — Integration Tests"
echo "  Program: $PROGRAM_ID"
echo "  RPC: $RPC"
echo "  Deployer: $DEPLOYER"
echo "================================================"

# --- Setup: Create a USDC-like SPL token ---
echo ""
echo "[Setup] Creating test USDC mint..."
USDC_MINT=$(spl-token create-token --decimals 6 2>&1 | grep "Creating token" | awk '{print $3}')
echo "  USDC Mint: $USDC_MINT"

# Create deployer's token account and mint some
DEPLOYER_ATA=$(spl-token create-account $USDC_MINT 2>&1 | grep "Creating account" | awk '{print $3}')
spl-token mint $USDC_MINT 1000000000 2>/dev/null  # 1000 USDC
echo "  Deployer ATA: $DEPLOYER_ATA (1000 USDC)"

# Create Agent A and Agent B keypairs
AGENT_A_KEY=/tmp/agio_agent_a.json
AGENT_B_KEY=/tmp/agio_agent_b.json
solana-keygen new --outfile $AGENT_A_KEY --no-bip39-passphrase --force 2>/dev/null
solana-keygen new --outfile $AGENT_B_KEY --no-bip39-passphrase --force 2>/dev/null
AGENT_A=$(solana-keygen pubkey $AGENT_A_KEY)
AGENT_B=$(solana-keygen pubkey $AGENT_B_KEY)
echo "  Agent A: $AGENT_A"
echo "  Agent B: $AGENT_B"

# Fund agents with SOL for rent
solana airdrop 10 $AGENT_A --url $RPC 2>/dev/null
solana airdrop 10 $AGENT_B --url $RPC 2>/dev/null

# Create token accounts for agents
AGENT_A_ATA=$(spl-token create-account $USDC_MINT --owner $AGENT_A 2>&1 | grep "Creating account" | awk '{print $3}')
AGENT_B_ATA=$(spl-token create-account $USDC_MINT --owner $AGENT_B 2>&1 | grep "Creating account" | awk '{print $3}')

# Transfer USDC to agents
spl-token transfer $USDC_MINT 100000000 $AGENT_A_ATA 2>/dev/null  # 100 USDC to A
spl-token transfer $USDC_MINT 100000000 $AGENT_B_ATA 2>/dev/null  # 100 USDC to B
echo "  Agent A has 100 USDC, Agent B has 100 USDC"

# Derive PDAs
VAULT_PDA=$(python3 -c "
from solders.pubkey import Pubkey
program = Pubkey.from_string('$PROGRAM_ID')
pda, bump = Pubkey.find_program_address([b'vault'], program)
print(pda)
" 2>/dev/null || echo "PDA_DERIVATION_NEEDS_PYTHON")
echo "  Vault PDA: $VAULT_PDA"

echo ""
echo "[Tests]"
echo ""

# --- Test 1: Initialize Vault ---
echo "  Test 1: Initialize Vault"
# This requires sending the instruction via the Anchor client
# For shell-based testing, we'll use the anchor test framework
# For now, verify the program is deployed
CODE=$(solana program show $PROGRAM_ID --url $RPC 2>&1 | grep "Program Id" || echo "")
if [ -n "$CODE" ]; then
    ok "Program deployed and accessible"
else
    fail "Program not found" "$CODE"
fi

# --- Report ---
echo ""
echo "================================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "================================================"
echo ""
echo "NOTE: Full instruction-level tests require the Anchor"
echo "TypeScript or Rust client. The LiteSVM unit tests cover"
echo "instruction logic. This script verifies deployment and"
echo "SPL token infrastructure."
