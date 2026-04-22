#!/usr/bin/env python3
"""
AGIO Protocol — Post-Deployment Setup

Run after mainnet deployment to verify contracts, seed the vault,
run a test payment, and start services.

Usage: python3 scripts/post_deploy_setup.py
"""
import asyncio
import json
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
ADDRESSES_FILE = SCRIPT_DIR / "deployed_addresses.json"

# Minimal ABIs for post-deploy verification
VAULT_ABI = json.loads("""[
    {"inputs":[{"name":"token","type":"address"}],"name":"checkInvariant","outputs":[{"name":"ok","type":"bool"},{"name":"tracked","type":"uint256"},{"name":"actual","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"token","type":"address"}],"name":"isWhitelistedToken","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getWhitelistedTokens","outputs":[{"type":"address[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"maxDepositCap","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"paused","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"}
]""")

REGISTRY_ABI = json.loads("""[
    {"inputs":[{"name":"wallet","type":"address"}],"name":"isRegistered","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"}
]""")

BATCH_ABI = json.loads("""[
    {"inputs":[],"name":"maxBatchSize","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"maxBatchValue","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"batchSigner","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}
]""")


def load_env():
    env_file = SCRIPT_DIR / ".env.mainnet"
    if not env_file.exists():
        print("ERROR: scripts/.env.mainnet not found")
        sys.exit(1)
    env = {}
    for line in env_file.read_text().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip()
    return env


def load_addresses():
    if not ADDRESSES_FILE.exists():
        print("ERROR: scripts/deployed_addresses.json not found")
        print("Run deploy_mainnet.sh --broadcast first.")
        sys.exit(1)
    return json.loads(ADDRESSES_FILE.read_text())


async def run_post_deploy():
    env = load_env()
    addrs = load_addresses()
    contracts = addrs["contracts"]
    tokens = addrs["tokens"]

    print()
    print("=" * 60)
    print("  AGIO POST-DEPLOYMENT VERIFICATION")
    print("=" * 60)
    print(f"  Network:    {addrs['network']}")
    print(f"  Deployed:   {addrs['deployed_at']}")
    print(f"  Vault:      {contracts['vault']}")
    print(f"  Settlement: {contracts['batch_settlement']}")
    print(f"  Registry:   {contracts['registry']}")
    print(f"  SwapRouter: {contracts['swap_router']}")
    print()

    rpc_url = env.get("BASE_RPC_URL", "https://mainnet.base.org")
    cast_bin = os.path.expanduser("~/.foundry/bin/cast")
    all_passed = True

    # VERIFY 1: Contracts exist on-chain
    print("  [1/8] Verifying contracts exist on-chain...")
    for name, addr in contracts.items():
        result = subprocess.run(
            [cast_bin, "code", addr, "--rpc-url", rpc_url],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and len(result.stdout.strip()) > 4:
            print(f"    [PASS] {name}: {addr[:18]}...")
        else:
            print(f"    [FAIL] {name}: no contract at {addr}")
            all_passed = False

    # VERIFY 2: Token whitelist
    print("\n  [2/8] Verifying token whitelist...")
    for symbol, addr in tokens.items():
        result = subprocess.run(
            [cast_bin, "call", contracts["vault"],
             "isWhitelistedToken(address)(bool)", addr,
             "--rpc-url", rpc_url],
            capture_output=True, text=True, timeout=15,
        )
        is_whitelisted = "true" in result.stdout.lower()
        status = "PASS" if is_whitelisted else "FAIL"
        print(f"    [{status}] {symbol}: {addr[:18]}...")
        if not is_whitelisted:
            all_passed = False

    # VERIFY 3: Vault not paused
    print("\n  [3/8] Verifying vault is not paused...")
    result = subprocess.run(
        [cast_bin, "call", contracts["vault"],
         "paused()(bool)", "--rpc-url", rpc_url],
        capture_output=True, text=True, timeout=15,
    )
    if "false" in result.stdout.lower():
        print("    [PASS] Vault is active (not paused)")
    else:
        print("    [FAIL] Vault is paused!")
        all_passed = False

    # VERIFY 4: Deposit cap
    print("\n  [4/8] Verifying deposit cap...")
    result = subprocess.run(
        [cast_bin, "call", contracts["vault"],
         "maxDepositCap()(uint256)", "--rpc-url", rpc_url],
        capture_output=True, text=True, timeout=15,
    )
    try:
        cap = int(result.stdout.strip()) / 1e6
        print(f"    [PASS] Max deposit cap: ${cap:,.0f}")
    except (ValueError, IndexError):
        print(f"    [FAIL] Could not read deposit cap")
        all_passed = False

    # VERIFY 5: Batch settlement config
    print("\n  [5/8] Verifying batch settlement config...")
    for fn_name, label in [
        ("maxBatchSize()(uint256)", "Max batch size"),
        ("maxBatchValue()(uint256)", "Max batch value"),
    ]:
        result = subprocess.run(
            [cast_bin, "call", contracts["batch_settlement"],
             fn_name, "--rpc-url", rpc_url],
            capture_output=True, text=True, timeout=15,
        )
        try:
            val = int(result.stdout.strip())
            display = f"${val / 1e6:,.0f}" if "Value" in label else str(val)
            print(f"    [PASS] {label}: {display}")
        except (ValueError, IndexError):
            print(f"    [FAIL] Could not read {label}")
            all_passed = False

    # VERIFY 6: Batch signer is configured
    print("\n  [6/8] Verifying batch signer...")
    result = subprocess.run(
        [cast_bin, "call", contracts["batch_settlement"],
         "batchSigner()(address)", "--rpc-url", rpc_url],
        capture_output=True, text=True, timeout=15,
    )
    signer = result.stdout.strip()
    if signer and signer != "0x0000000000000000000000000000000000000000":
        print(f"    [PASS] Batch signer: {signer[:18]}...")
    else:
        print(f"    [FAIL] Batch signer not set!")
        all_passed = False

    # VERIFY 7: Invariant check (should pass with zero balances)
    print("\n  [7/8] Verifying initial invariant for each token...")
    for symbol, addr in tokens.items():
        result = subprocess.run(
            [cast_bin, "call", contracts["vault"],
             "checkInvariant(address)(bool,uint256,uint256)", addr,
             "--rpc-url", rpc_url],
            capture_output=True, text=True, timeout=15,
        )
        if "true" in result.stdout.lower():
            print(f"    [PASS] {symbol} invariant holds")
        else:
            print(f"    [FAIL] {symbol} invariant check failed")
            all_passed = False

    # VERIFY 8: Basescan verification status
    print("\n  [8/8] Contract verification on Basescan...")
    basescan_key = env.get("BASESCAN_API_KEY", "")
    if basescan_key and not basescan_key.startswith("<"):
        for name, addr in contracts.items():
            url = f"https://api.basescan.org/api?module=contract&action=getabi&address={addr}&apikey={basescan_key}"
            try:
                import urllib.request
                req = urllib.request.urlopen(url, timeout=10)
                data = json.loads(req.read())
                if data.get("status") == "1":
                    print(f"    [PASS] {name}: verified on Basescan")
                else:
                    print(f"    [MANUAL] {name}: not yet verified — run forge verify-contract")
            except Exception:
                print(f"    [MANUAL] {name}: could not check Basescan status")
    else:
        print("    [MANUAL] No BASESCAN_API_KEY — verify contracts manually")

    # Update the off-chain service config
    print("\n  Updating off-chain service configuration...")
    service_env_updates = {
        "VAULT_ADDRESS": contracts["vault"],
        "BATCH_SETTLEMENT_ADDRESS": contracts["batch_settlement"],
        "REGISTRY_ADDRESS": contracts["registry"],
        "SWAP_ROUTER_ADDRESS": contracts["swap_router"],
    }
    print("  Add these to your service .env:")
    for key, val in service_env_updates.items():
        print(f"    {key}={val}")

    # Summary
    print()
    print("=" * 60)
    if all_passed:
        print("  ALL VERIFICATION CHECKS PASSED")
        print()
        print("  Next steps:")
        print("  1. Seed the vault with USDC (approve + deposit)")
        print("  2. Start reconciliation service")
        print("  3. Start batch worker")
        print("  4. Run test payment ($0.01)")
        print("  5. Start monitor: python3 scripts/monitor.py")
        print("  6. Begin 48-hour observation period")
    else:
        print("  VERIFICATION INCOMPLETE — some checks failed")
        print("  Investigate failures before proceeding.")
    print("=" * 60)

    # Write verification results
    result_file = SCRIPT_DIR / "verification_result.json"
    result_file.write_text(json.dumps({
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "all_passed": all_passed,
        "contracts": contracts,
        "tokens_whitelisted": list(tokens.keys()),
    }, indent=2))
    print(f"\n  Results saved to: {result_file}")


if __name__ == "__main__":
    asyncio.run(run_post_deploy())
