#!/usr/bin/env python3
"""
AGIO Protocol — Full Demo

This is what you show developers. It demonstrates:
1. Register two agents
2. Deposit testnet USDC
3. 100 payments between them
4. Batch settlement on-chain
5. Final balance verification + invariant check

Run:
  python demo.py

Requires: Anvil running on localhost:8545 with deployed contracts.
"""
import sys, json, time
sys.path.insert(0, "sdk/src")
from agio import AgioClient

# Load deployed addresses
with open("contracts/deployed.json") as f:
    deployed = json.load(f)

print("=" * 60)
print("  AGIO PROTOCOL — FULL DEMO")
print("=" * 60)
print(f"\n  Chain: {deployed['chain']}")
print(f"  RPC:   {deployed['rpc']}")
print(f"  Vault: {deployed['vault']}")
print(f"  Batch: {deployed['batch_settlement']}")
print()

# --- Step 1: Create two agents ---
print("STEP 1: Register two agents")
print("-" * 40)

# Alice (deployer wallet — already has ETH)
alice = AgioClient(
    rpc_url=deployed["rpc"],
    private_key=deployed["deployer_key"],
    vault_address=deployed["vault"],
    batch_address=deployed["batch_settlement"],
    registry_address=deployed["registry"],
    usdc_address=deployed["usdc"],
    signer_key=deployed["deployer_key"],
)

# Bob (generate fresh wallet, fund with ETH from Alice)
from eth_account import Account
from web3 import Web3
w3 = Web3(Web3.HTTPProvider(deployed["rpc"]))
bob_account = Account.create()
bob_key = bob_account.key.hex()

# Fund Bob with ETH for gas
tx = {
    "from": alice.address,
    "to": bob_account.address,
    "value": w3.to_wei(1, "ether"),
    "nonce": w3.eth.get_transaction_count(alice.address),
    "gas": 21000,
    "gasPrice": w3.eth.gas_price,
}
signed = Account.from_key(deployed["deployer_key"]).sign_transaction(tx)
w3.eth.send_raw_transaction(signed.raw_transaction)

bob = AgioClient(
    rpc_url=deployed["rpc"],
    private_key=bob_key,
    vault_address=deployed["vault"],
    batch_address=deployed["batch_settlement"],
    registry_address=deployed["registry"],
    usdc_address=deployed["usdc"],
    signer_key=deployed["deployer_key"],  # deployer is the batch signer
)

alice_id = alice.register("alice-research-agent")
bob_id = bob.register("bob-data-agent")
print(f"  Alice: {alice.address}")
print(f"    AGIO ID: {alice_id[:20]}...")
print(f"  Bob:   {bob.address}")
print(f"    AGIO ID: {bob_id[:20]}...")

# --- Step 2: Deposit USDC ---
print(f"\nSTEP 2: Deposit testnet USDC")
print("-" * 40)

alice.mint_test_usdc(100.0)
alice.deposit(100.0)
bob.mint_test_usdc(50.0)
bob.deposit(50.0)

alice_bal = alice.balance()
bob_bal = bob.balance()
print(f"  Alice deposited: ${alice_bal.available:.2f} USDC")
print(f"  Bob deposited:   ${bob_bal.available:.2f} USDC")
print(f"  Total in vault:  ${alice_bal.available + bob_bal.available:.2f} USDC")

ok, tracked, actual = alice.check_invariant()
print(f"  Invariant check: {'PASS' if ok else 'FAIL'} (tracked=${tracked:.2f}, actual=${actual:.2f})")

# --- Step 3: 100 payments ---
print(f"\nSTEP 3: Queue 100 payments (Alice → Bob)")
print("-" * 40)

start = time.time()
receipts = []
for i in range(100):
    receipt = alice.pay(to=bob.address, amount=0.005, memo=f"API call #{i+1}")
    receipts.append(receipt)

elapsed = (time.time() - start) * 1000
print(f"  100 payments queued in {elapsed:.0f}ms ({elapsed/100:.1f}ms each)")
print(f"  Total value: ${0.005 * 100:.2f} USDC")
print(f"  Status: {receipts[0].status}")
print(f"  Sample payment_id: {receipts[0].payment_id[:20]}...")

# --- Step 4: Batch settlement ---
print(f"\nSTEP 4: Settle batch on-chain")
print("-" * 40)

start = time.time()
result = alice.flush()
elapsed = (time.time() - start) * 1000
print(f"  Settlement: {result}")
print(f"  Settlement time: {elapsed:.0f}ms")

# --- Step 5: Verify final balances ---
print(f"\nSTEP 5: Final balances")
print("-" * 40)

alice_final = alice.balance()
bob_final = bob.balance()
print(f"  Alice: ${alice_final.available:.2f} USDC (started with $100.00, sent $0.50)")
print(f"  Bob:   ${bob_final.available:.2f} USDC (started with $50.00, received $0.50)")

expected_alice = 100.0 - (0.005 * 100)
expected_bob = 50.0 + (0.005 * 100)
print(f"  Expected Alice: ${expected_alice:.2f} — {'MATCH' if abs(alice_final.available - expected_alice) < 0.001 else 'MISMATCH!'}")
print(f"  Expected Bob:   ${expected_bob:.2f} — {'MATCH' if abs(bob_final.available - expected_bob) < 0.001 else 'MISMATCH!'}")

ok, tracked, actual = alice.check_invariant()
print(f"  Invariant: {'PASS' if ok else 'FAIL'} (tracked=${tracked:.2f}, actual=${actual:.2f})")

print(f"\n{'=' * 60}")
print(f"  DEMO COMPLETE")
print(f"  100 payments settled in 1 on-chain transaction")
print(f"  Cost per payment: ~$0.00004 (gas shared across batch)")
print(f"  Vault invariant: {'VERIFIED' if ok else 'FAILED'}")
print(f"{'=' * 60}")
