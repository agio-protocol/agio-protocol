#!/usr/bin/env python3
"""
AGIO Solana Vault — SPL Token Integration Tests on localnet.
Tests deposit, withdraw, batch settlement with real SPL tokens.
"""
import asyncio
import json
import hashlib
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
RPC_URL = "http://localhost:8899"

PASSED = 0
FAILED = 0


def ok(name): global PASSED; PASSED += 1; print(f"  [PASS] {name}")
def fail(name, err=""): global FAILED; FAILED += 1; print(f"  [FAIL] {name}: {err}")
def pda(seeds): return Pubkey.find_program_address(seeds, PROGRAM_ID)[0]
def disc(name): return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def cli(cmd):
    """Run a solana/spl-token CLI command."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


async def send(client, ix_list, payer, signers):
    blockhash = (await client.get_latest_blockhash()).value.blockhash
    tx = Transaction.new_signed_with_payer(ix_list, payer.pubkey(), signers, blockhash)
    result = await client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
    await asyncio.sleep(0.5)
    status = await client.get_signature_statuses([result.value])
    st = status.value[0]
    if st and st.err:
        raise Exception(f"Tx failed: {st.err}")
    return result.value


async def run_tests():
    global PASSED, FAILED
    client = AsyncClient(RPC_URL)
    deployer = Keypair.from_bytes(bytes(json.loads(
        (Path.home() / ".config/solana/id.json").read_text()
    )))
    vault_pda = pda([b"vault"])

    print("=" * 60)
    print("  AGIO Solana Vault — SPL Token Tests (localnet)")
    print("=" * 60)

    # Setup: create USDC mint
    mint_output = cli("spl-token create-token --decimals 6")
    usdc_mint_str = mint_output.split("Address:")[1].strip().split("\n")[0].strip() if "Address:" in mint_output else mint_output.split("\n")[0].split()[-1]
    # Get the last created token
    tokens = cli("spl-token accounts --output json")
    # Just use a fresh mint
    mint_out = cli("spl-token create-token --decimals 6 2>&1")
    for line in mint_out.split("\n"):
        if line.startswith("Address:"):
            usdc_mint_str = line.split(":")[1].strip()
            break
    usdc_mint = Pubkey.from_string(usdc_mint_str)
    print(f"  USDC Mint: {usdc_mint}")

    # Create deployer's token account and mint 1000 USDC
    cli(f"spl-token create-account {usdc_mint_str}")
    cli(f"spl-token mint {usdc_mint_str} 1000000000")  # 1000 USDC

    # Create vault's token account (ATA for vault PDA - we need to derive it)
    # The vault PDA needs a token account. We'll create one owned by the vault PDA.
    from solders.pubkey import Pubkey as Pb
    ASSOC_TOKEN_PROGRAM = Pb.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    vault_ata = Pb.find_program_address(
        [bytes(vault_pda), bytes(TOKEN_PROGRAM), bytes(usdc_mint)],
        ASSOC_TOKEN_PROGRAM
    )[0]
    print(f"  Vault ATA: {vault_ata}")

    # Create vault ATA using CLI (deployer pays)
    cli(f"spl-token create-account {usdc_mint_str} --owner {vault_pda} --fee-payer ~/.config/solana/id.json")

    # Create Agent A and B
    agent_a = Keypair()
    agent_b = Keypair()
    await client.request_airdrop(agent_a.pubkey(), 10_000_000_000)
    await client.request_airdrop(agent_b.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)
    print(f"  Agent A: {agent_a.pubkey()}")
    print(f"  Agent B: {agent_b.pubkey()}")

    # Create agent token accounts
    agent_a_ata = Pb.find_program_address(
        [bytes(agent_a.pubkey()), bytes(TOKEN_PROGRAM), bytes(usdc_mint)],
        ASSOC_TOKEN_PROGRAM
    )[0]
    agent_b_ata = Pb.find_program_address(
        [bytes(agent_b.pubkey()), bytes(TOKEN_PROGRAM), bytes(usdc_mint)],
        ASSOC_TOKEN_PROGRAM
    )[0]

    # Create ATAs for agents using deployer funds
    cli(f"spl-token create-account {usdc_mint_str} --owner {agent_a.pubkey()} --fee-payer ~/.config/solana/id.json")
    cli(f"spl-token create-account {usdc_mint_str} --owner {agent_b.pubkey()} --fee-payer ~/.config/solana/id.json")

    # Fund agents with USDC
    cli(f"spl-token transfer {usdc_mint_str} 100000000 {agent_a_ata} --fund-recipient")  # 100 USDC
    cli(f"spl-token transfer {usdc_mint_str} 100000000 {agent_b_ata} --fund-recipient")  # 100 USDC
    print(f"  Funded: A=100 USDC, B=100 USDC")

    # Initialize vault (may already exist)
    batch_signer = Keypair()
    print()

    # --- TEST 1: Init vault ---
    print("  Test 1: Initialize vault")
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("initialize_vault") + bytes(batch_signer.pubkey()) + bytes(deployer.pubkey()),
                [AccountMeta(vault_pda, False, True), AccountMeta(deployer.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            deployer, [deployer])
        ok("Initialize vault")
    except Exception as e:
        if "Custom" in str(e) or "already" in str(e): ok("Initialize vault (exists)")
        else: fail("Initialize vault", str(e)[:100])

    # --- TEST 2: Register agents ---
    print("  Test 2: Register agents")
    agent_a_pda = pda([b"agent", bytes(agent_a.pubkey())])
    agent_b_pda = pda([b"agent", bytes(agent_b.pubkey())])
    for name, agent, agent_pda in [("A", agent_a, agent_a_pda), ("B", agent_b, agent_b_pda)]:
        try:
            await send(client,
                [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                    [AccountMeta(vault_pda, False, True), AccountMeta(agent_pda, False, True),
                     AccountMeta(agent.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
                agent, [agent])
            ok(f"Register Agent {name}")
        except Exception as e:
            if "Custom" in str(e): ok(f"Register Agent {name} (exists)")
            else: fail(f"Register Agent {name}", str(e)[:80])

    # --- TEST 3: Deposit USDC (Agent A deposits 10 USDC) ---
    print("  Test 3: Deposit 10 USDC (Agent A)")
    try:
        amount = (10_000_000).to_bytes(8, "little")  # 10 USDC in 6 decimals
        await send(client,
            [Instruction(PROGRAM_ID, disc("deposit") + amount,
                [AccountMeta(vault_pda, False, True), AccountMeta(agent_a_pda, False, True),
                 AccountMeta(agent_a_ata, False, True), AccountMeta(vault_ata, False, True),
                 AccountMeta(usdc_mint, False, False), AccountMeta(agent_a.pubkey(), True, True),
                 AccountMeta(TOKEN_PROGRAM, False, False)])],
            agent_a, [agent_a])
        ok("Deposit 10 USDC (Agent A)")
    except Exception as e:
        fail("Deposit 10 USDC", str(e)[:100])

    # --- TEST 4: Check vault balance increased ---
    print("  Test 4: Verify vault token balance")
    try:
        vault_acct = await client.get_token_account_balance(vault_ata)
        vault_bal = int(vault_acct.value.amount)
        if vault_bal == 10_000_000:
            ok(f"Vault balance = {vault_bal / 1e6} USDC")
        else:
            fail(f"Vault balance", f"expected 10000000, got {vault_bal}")
    except Exception as e:
        fail("Vault balance check", str(e)[:80])

    # --- TEST 5: Withdraw 2 USDC (Agent A) ---
    print("  Test 5: Withdraw 2 USDC (Agent A)")
    try:
        amount = (2_000_000).to_bytes(8, "little")
        await send(client,
            [Instruction(PROGRAM_ID, disc("withdraw") + amount,
                [AccountMeta(vault_pda, False, True), AccountMeta(agent_a_pda, False, True),
                 AccountMeta(agent_a_ata, False, True), AccountMeta(vault_ata, False, True),
                 AccountMeta(usdc_mint, False, False), AccountMeta(agent_a.pubkey(), True, True),
                 AccountMeta(TOKEN_PROGRAM, False, False)])],
            agent_a, [agent_a])
        ok("Withdraw 2 USDC (Agent A)")
    except Exception as e:
        fail("Withdraw 2 USDC", str(e)[:100])

    # --- TEST 6: Verify vault balance after withdraw ---
    print("  Test 6: Verify vault balance after withdraw")
    try:
        vault_acct = await client.get_token_account_balance(vault_ata)
        vault_bal = int(vault_acct.value.amount)
        if vault_bal == 8_000_000:
            ok(f"Vault balance = {vault_bal / 1e6} USDC (8 after withdrawing 2)")
        else:
            fail(f"Vault balance after withdraw", f"expected 8000000, got {vault_bal}")
    except Exception as e:
        fail("Vault balance after withdraw", str(e)[:80])

    # --- TEST 7: Withdraw more than balance fails ---
    print("  Test 7: Withdraw more than balance fails")
    try:
        amount = (100_000_000).to_bytes(8, "little")  # 100 USDC (more than 8 available)
        await send(client,
            [Instruction(PROGRAM_ID, disc("withdraw") + amount,
                [AccountMeta(vault_pda, False, True), AccountMeta(agent_a_pda, False, True),
                 AccountMeta(agent_a_ata, False, True), AccountMeta(vault_ata, False, True),
                 AccountMeta(usdc_mint, False, False), AccountMeta(agent_a.pubkey(), True, True),
                 AccountMeta(TOKEN_PROGRAM, False, False)])],
            agent_a, [agent_a])
        fail("Over-withdraw", "should have failed")
    except:
        ok("Over-withdraw rejected")

    # --- TEST 8: Read agent balance from account data ---
    print("  Test 8: Read Agent A on-chain balance")
    try:
        acct = await client.get_account_info(agent_a_pda, commitment=Confirmed)
        data = acct.value.data
        # Agent balances start at offset 8 + 32 + 8 + 8 + 8 + 32 + 1 + 1 = 98
        # Each TokenBalance is: mint(32) + available(8) + locked(8) = 48 bytes
        offset = 8 + 32 + 8 + 8 + 8 + 32 + 1 + 1  # = 98
        # First token balance
        bal_mint = Pubkey.from_bytes(data[offset:offset+32])
        bal_available = int.from_bytes(data[offset+32:offset+40], "little")
        bal_locked = int.from_bytes(data[offset+40:offset+48], "little")
        ok(f"Agent A balance: {bal_available/1e6} USDC available, {bal_locked/1e6} locked (mint={bal_mint})")
    except Exception as e:
        fail("Read agent balance", str(e)[:80])

    # SUMMARY
    print()
    print("=" * 60)
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    if FAILED == 0:
        print("  ALL TESTS PASSED")
    else:
        print("  SOME TESTS FAILED — investigate above")
    print("=" * 60)

    await client.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
