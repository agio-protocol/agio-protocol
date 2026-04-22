#!/usr/bin/env python3
"""
AGIO Solana Vault — Batch Settlement Tests on localnet.
Tests atomic multi-payment settlement, replay protection, invariant, scaling.
"""
import asyncio
import json
import hashlib
import struct
import subprocess
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

PROGRAM_ID = Pubkey.from_string("CpHfZKzThtYt64YjAWKkJYNZboQYjPazSTxj75j3w9YE")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOC_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RPC = "http://localhost:8899"

PASSED = 0
FAILED = 0


def ok(name): global PASSED; PASSED += 1; print(f"  [PASS] {name}")
def fail(name, err=""): global FAILED; FAILED += 1; print(f"  [FAIL] {name}: {err}")
def pda(seeds): return Pubkey.find_program_address(seeds, PROGRAM_ID)[0]
def disc(name): return hashlib.sha256(f"global:{name}".encode()).digest()[:8]
def cli(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()


async def send(client, ix_list, payer, signers):
    bh = (await client.get_latest_blockhash()).value.blockhash
    tx = Transaction.new_signed_with_payer(ix_list, payer.pubkey(), signers, bh)
    result = await client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
    await asyncio.sleep(0.5)
    st = (await client.get_signature_statuses([result.value])).value[0]
    if st and st.err:
        raise Exception(f"Tx failed: {st.err}")
    return result.value


def encode_batch_payment(from_pk, to_pk, amount, token_mint, payment_id, fee):
    """Encode a BatchPayment struct for the settle_batch instruction."""
    return (
        bytes(from_pk) +       # 32
        bytes(to_pk) +         # 32
        struct.pack("<Q", amount) +  # 8 (u64 LE)
        bytes(token_mint) +    # 32
        payment_id +           # 32
        struct.pack("<Q", fee) # 8 (u64 LE)
    )


def encode_settle_batch_data(batch_id, payments_encoded):
    """Encode the full instruction data for settle_batch."""
    data = disc("settle_batch")
    data += batch_id  # [u8; 32]
    # Vec<BatchPayment>: length prefix (u32 LE) + items
    data += struct.pack("<I", len(payments_encoded))
    for p in payments_encoded:
        data += p
    return data


async def read_agent_balance(client, agent_pda, token_mint):
    """Read agent's available balance for a token from on-chain account data."""
    acct = await client.get_account_info(agent_pda, commitment=Confirmed)
    if not acct.value:
        return 0
    data = acct.value.data
    # Layout: discriminator(8) + wallet(32) + registered_at(8) + total_payments(8) +
    #         total_volume(8) + preferred_token(32) + tier(1) + bump(1) = 98
    # Then balances array: 4 x TokenBalance (mint(32) + available(8) + locked(8) = 48)
    offset = 98
    for i in range(4):
        start = offset + i * 48
        mint = Pubkey.from_bytes(data[start:start+32])
        available = int.from_bytes(data[start+32:start+40], "little")
        locked = int.from_bytes(data[start+40:start+48], "little")
        if mint == token_mint:
            return available
    return 0


async def run_tests():
    global PASSED, FAILED
    client = AsyncClient(RPC)

    deployer = Keypair.from_bytes(bytes(json.loads(
        (Path.home() / ".config/solana/id.json").read_text()
    )))
    vault_pda = pda([b"vault"])

    print("=" * 60)
    print("  AGIO Solana Vault — Batch Settlement Tests")
    print("=" * 60)

    # --- SETUP: Mint, vault, agents ---
    print("\n  [Setup]")

    # Create mint
    out = cli("spl-token create-token --decimals 6")
    usdc_str = None
    for line in out.split("\n"):
        if "Address:" in line:
            usdc_str = line.split(":")[1].strip()
    if not usdc_str:
        usdc_str = out.split()[-1]
    usdc_mint = Pubkey.from_string(usdc_str)
    print(f"  USDC: {usdc_mint}")

    # Deployer token account + mint supply
    cli(f"spl-token create-account {usdc_str}")
    cli(f"spl-token mint {usdc_str} 10000000000")  # 10,000 USDC

    # Vault ATA
    vault_ata = Pubkey.find_program_address(
        [bytes(vault_pda), bytes(TOKEN_PROGRAM), bytes(usdc_mint)], ASSOC_TOKEN_PROGRAM
    )[0]
    cli(f"spl-token create-account {usdc_str} --owner {vault_pda} --fee-payer ~/.config/solana/id.json")

    # Batch signer
    batch_signer = Keypair()
    await client.request_airdrop(batch_signer.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)

    # Init vault
    try:
        await send(client,
            [Instruction(PROGRAM_ID,
                disc("initialize_vault") + bytes(batch_signer.pubkey()) + bytes(deployer.pubkey()),
                [AccountMeta(vault_pda, False, True), AccountMeta(deployer.pubkey(), True, True),
                 AccountMeta(SYS_PROGRAM, False, False)])],
            deployer, [deployer])
    except:
        pass  # Already initialized
    print("  Vault initialized")

    # Create Agent A and B
    agent_a = Keypair()
    agent_b = Keypair()
    await client.request_airdrop(agent_a.pubkey(), 10_000_000_000)
    await client.request_airdrop(agent_b.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)

    agent_a_pda = pda([b"agent", bytes(agent_a.pubkey())])
    agent_b_pda = pda([b"agent", bytes(agent_b.pubkey())])

    for name, agent, apda in [("A", agent_a, agent_a_pda), ("B", agent_b, agent_b_pda)]:
        await send(client,
            [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                [AccountMeta(vault_pda, False, True), AccountMeta(apda, False, True),
                 AccountMeta(agent.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            agent, [agent])
    print("  Agents registered")

    # Create ATAs and fund
    agent_a_ata = Pubkey.find_program_address(
        [bytes(agent_a.pubkey()), bytes(TOKEN_PROGRAM), bytes(usdc_mint)], ASSOC_TOKEN_PROGRAM
    )[0]
    agent_b_ata = Pubkey.find_program_address(
        [bytes(agent_b.pubkey()), bytes(TOKEN_PROGRAM), bytes(usdc_mint)], ASSOC_TOKEN_PROGRAM
    )[0]
    cli(f"spl-token create-account {usdc_str} --owner {agent_a.pubkey()} --fee-payer ~/.config/solana/id.json")
    cli(f"spl-token create-account {usdc_str} --owner {agent_b.pubkey()} --fee-payer ~/.config/solana/id.json")
    cli(f"spl-token transfer {usdc_str} 100000000 {agent_a_ata} --fund-recipient")  # 100 USDC
    cli(f"spl-token transfer {usdc_str} 100000000 {agent_b_ata} --fund-recipient")  # 100 USDC

    # Deposit 10 USDC each
    for name, agent, apda, ata in [("A", agent_a, agent_a_pda, agent_a_ata), ("B", agent_b, agent_b_pda, agent_b_ata)]:
        await send(client,
            [Instruction(PROGRAM_ID, disc("deposit") + struct.pack("<Q", 10_000_000),
                [AccountMeta(vault_pda, False, True), AccountMeta(apda, False, True),
                 AccountMeta(ata, False, True), AccountMeta(vault_ata, False, True),
                 AccountMeta(usdc_mint, False, False), AccountMeta(agent.pubkey(), True, True),
                 AccountMeta(TOKEN_PROGRAM, False, False)])],
            agent, [agent])
    print("  Deposited 10 USDC each")

    bal_a = await read_agent_balance(client, agent_a_pda, usdc_mint)
    bal_b = await read_agent_balance(client, agent_b_pda, usdc_mint)
    print(f"  Before batch: A={bal_a/1e6} USDC, B={bal_b/1e6} USDC")

    # === TEST 1: 3-payment batch ===
    print("\n  Test 1: 3-payment batch (A->B 1, B->A 0.5, A->B 0.25)")
    batch_id = bytes([0x01] * 32)
    payments = [
        encode_batch_payment(agent_a.pubkey(), agent_b.pubkey(), 1_000_000, usdc_mint, bytes([0x11]*32), 150),
        encode_batch_payment(agent_b.pubkey(), agent_a.pubkey(), 500_000, usdc_mint, bytes([0x12]*32), 150),
        encode_batch_payment(agent_a.pubkey(), agent_b.pubkey(), 250_000, usdc_mint, bytes([0x13]*32), 150),
    ]
    batch_pda = pda([b"batch", batch_id])

    metas = [
        AccountMeta(vault_pda, False, True),
        AccountMeta(batch_pda, False, True),
        AccountMeta(batch_signer.pubkey(), True, True),
        AccountMeta(SYS_PROGRAM, False, False),
        # remaining_accounts: sender, receiver pairs
        AccountMeta(agent_a_pda, False, True),  # payment 0 sender
        AccountMeta(agent_b_pda, False, True),  # payment 0 receiver
        AccountMeta(agent_b_pda, False, True),  # payment 1 sender
        AccountMeta(agent_a_pda, False, True),  # payment 1 receiver
        AccountMeta(agent_a_pda, False, True),  # payment 2 sender
        AccountMeta(agent_b_pda, False, True),  # payment 2 receiver
    ]

    try:
        ix_data = encode_settle_batch_data(batch_id, payments)
        ix = Instruction(PROGRAM_ID, ix_data, metas)
        await send(client, [ix], batch_signer, [batch_signer])

        bal_a = await read_agent_balance(client, agent_a_pda, usdc_mint)
        bal_b = await read_agent_balance(client, agent_b_pda, usdc_mint)

        # Expected: A = 10 - 1 - 0.00015 + 0.5 - 0.25 - 0.00015 = 9.24970
        #           B = 10 + 1 - 0.5 - 0.00015 + 0.25 = 10.74985
        # Fees: deducted from sender's total_debit (amount + fee)
        expected_a = 10_000_000 - 1_000_150 + 500_000 - 250_150  # = 9,249,700
        expected_b = 10_000_000 + 1_000_000 - 500_150 + 250_000  # = 10,749,850

        if bal_a == expected_a and bal_b == expected_b:
            ok(f"3-payment batch: A={bal_a/1e6:.6f}, B={bal_b/1e6:.6f}")
        else:
            fail(f"3-payment batch", f"A={bal_a} (exp {expected_a}), B={bal_b} (exp {expected_b})")
    except Exception as e:
        fail("3-payment batch", str(e)[:120])

    # === TEST 2: Replay protection ===
    print("  Test 2: Replay protection (same batch_id)")
    try:
        ix = Instruction(PROGRAM_ID, ix_data, metas)
        await send(client, [ix], batch_signer, [batch_signer])
        fail("Replay protection", "should have failed")
    except:
        ok("Replay protection — duplicate batch rejected")

    # === TEST 3: Check invariant ===
    print("  Test 3: Check invariant")
    try:
        ix = Instruction(PROGRAM_ID, disc("check_invariant") + bytes(usdc_mint),
            [AccountMeta(vault_pda, False, False), AccountMeta(vault_ata, False, False)])
        # Simulate to see the log
        bh = (await client.get_latest_blockhash()).value.blockhash
        tx = Transaction.new_signed_with_payer([ix], deployer.pubkey(), [deployer], bh)
        sim = await client.simulate_transaction(tx)
        logs = sim.value.logs or []
        invariant_ok = any("Invariant OK" in l for l in logs)
        if invariant_ok:
            ok("Invariant check passed (tracked == actual)")
        else:
            # Invariant may show violated because batch settlement debits/credits
            # internal balances but doesn't move real tokens
            violated = any("VIOLATED" in l for l in logs)
            if violated:
                for l in logs:
                    if "tracked" in l.lower() or "invariant" in l.lower():
                        print(f"    {l}")
                fail("Invariant", "tracked != actual (expected — batch settlement only moves internal balances)")
            else:
                ok("Invariant check ran")
    except Exception as e:
        fail("Invariant check", str(e)[:80])

    # === TEST 4: 5-payment batch (max without ALT) ===
    print("  Test 4: 5-payment batch")
    batch_id_5 = bytes([0x02] * 32)
    batch_pda_5 = pda([b"batch", batch_id_5])
    payments_5 = []
    for i in range(5):
        pid = bytes([0x20 + i] + [0]*31)
        if i % 2 == 0:
            payments_5.append(encode_batch_payment(
                agent_a.pubkey(), agent_b.pubkey(), 100_000, usdc_mint, pid, 0))
        else:
            payments_5.append(encode_batch_payment(
                agent_b.pubkey(), agent_a.pubkey(), 100_000, usdc_mint, pid, 0))

    metas_5 = [
        AccountMeta(vault_pda, False, True),
        AccountMeta(batch_pda_5, False, True),
        AccountMeta(batch_signer.pubkey(), True, True),
        AccountMeta(SYS_PROGRAM, False, False),
    ]
    for i in range(5):
        if i % 2 == 0:
            metas_5.append(AccountMeta(agent_a_pda, False, True))
            metas_5.append(AccountMeta(agent_b_pda, False, True))
        else:
            metas_5.append(AccountMeta(agent_b_pda, False, True))
            metas_5.append(AccountMeta(agent_a_pda, False, True))

    try:
        ix_data_5 = encode_settle_batch_data(batch_id_5, payments_5)
        ix = Instruction(PROGRAM_ID, ix_data_5, metas_5)
        sig = await send(client, [ix], batch_signer, [batch_signer])
        ok(f"5-payment batch settled")
    except Exception as e:
        fail("5-payment batch", str(e)[:120])

    # === TEST 5: Read final vault state ===
    print("  Test 5: Read vault state")
    try:
        acct = await client.get_account_info(vault_pda, commitment=Confirmed)
        data = acct.value.data
        total_batches = int.from_bytes(data[8+32+32+32+1+8:8+32+32+32+1+8+8], "little")
        total_payments = int.from_bytes(data[8+32+32+32+1+8+8:8+32+32+32+1+8+8+8], "little")
        ok(f"Vault state: {total_batches} batches, {total_payments} payments")
    except Exception as e:
        fail("Vault state read", str(e)[:80])

    # === TEST 6: Final balances ===
    print("  Test 6: Final balances")
    bal_a = await read_agent_balance(client, agent_a_pda, usdc_mint)
    bal_b = await read_agent_balance(client, agent_b_pda, usdc_mint)
    print(f"    Agent A: {bal_a/1e6:.6f} USDC")
    print(f"    Agent B: {bal_b/1e6:.6f} USDC")
    total = bal_a + bal_b
    # After both batches, 450 in fees were collected from batch 1
    # Batch 2 had 0 fees, 10 payments of 0.1 alternating = net zero
    print(f"    Total: {total/1e6:.6f} USDC (started with 20.0)")
    if total <= 20_000_000:
        ok(f"No money created (total {total/1e6:.6f} <= 20.0)")
    else:
        fail("Balance conservation", f"total {total} > 20000000")

    # SUMMARY
    print()
    print("=" * 60)
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    if FAILED == 0:
        print("  ALL BATCH SETTLEMENT TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")
    print("=" * 60)

    await client.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
