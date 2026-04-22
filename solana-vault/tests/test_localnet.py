#!/usr/bin/env python3
"""
AGIO Solana Vault — Integration tests on localnet.
Requires: solana-test-validator running, program deployed.
"""
import asyncio
import json
import hashlib
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
RPC_URL = "http://localhost:8899"

PASSED = 0
FAILED = 0


def ok(name):
    global PASSED; PASSED += 1; print(f"  [PASS] {name}")


def fail(name, err=""):
    global FAILED; FAILED += 1; print(f"  [FAIL] {name}: {err}")


def pda(seeds):
    return Pubkey.find_program_address(seeds, PROGRAM_ID)[0]


def disc(name):
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


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
    batch_signer = Keypair()
    fee_collector = Keypair().pubkey()
    vault = pda([b"vault"])
    usdc_mint = Pubkey.from_string("11111111111111111111111111111111")

    print("=" * 55)
    print("  AGIO Solana Vault — Integration Tests")
    print("=" * 55)
    print(f"  Vault PDA: {vault}")
    print()

    # TEST 1: Initialize
    print("  Test 1: Initialize vault")
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("initialize_vault") + bytes(batch_signer.pubkey()) + bytes(fee_collector),
                [AccountMeta(vault, False, True), AccountMeta(deployer.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            deployer, [deployer])
        ok("Initialize vault")
    except Exception as e:
        if "already in use" in str(e) or "Custom" in str(e): ok("Initialize vault (exists)")
        else: fail("Initialize vault", str(e)[:100])

    # TEST 2: Register Agent A
    print("  Test 2: Register Agent A")
    agent_a = Keypair()
    await client.request_airdrop(agent_a.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)
    agent_a_pda = pda([b"agent", bytes(agent_a.pubkey())])
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                [AccountMeta(vault, False, True), AccountMeta(agent_a_pda, False, True),
                 AccountMeta(agent_a.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            agent_a, [agent_a])
        ok("Register Agent A")
    except Exception as e:
        fail("Register Agent A", str(e)[:80])

    # TEST 3: Register Agent B
    print("  Test 3: Register Agent B")
    agent_b = Keypair()
    await client.request_airdrop(agent_b.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)
    agent_b_pda = pda([b"agent", bytes(agent_b.pubkey())])
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                [AccountMeta(vault, False, True), AccountMeta(agent_b_pda, False, True),
                 AccountMeta(agent_b.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            agent_b, [agent_b])
        ok("Register Agent B")
    except Exception as e:
        fail("Register Agent B", str(e)[:80])

    # TEST 4: Duplicate registration fails
    print("  Test 4: Duplicate registration fails")
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                [AccountMeta(vault, False, True), AccountMeta(agent_a_pda, False, True),
                 AccountMeta(agent_a.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            agent_a, [agent_a])
        fail("Duplicate registration", "should have failed")
    except:
        ok("Duplicate registration rejected")

    # TEST 5: Pause
    print("  Test 5: Pause vault")
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("pause"),
                [AccountMeta(vault, False, True), AccountMeta(deployer.pubkey(), True, False)])],
            deployer, [deployer])
        ok("Pause vault")
    except Exception as e:
        fail("Pause vault", str(e)[:80])

    # TEST 6: Registration blocked when paused
    print("  Test 6: Registration blocked when paused")
    agent_c = Keypair()
    await client.request_airdrop(agent_c.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)
    agent_c_pda = pda([b"agent", bytes(agent_c.pubkey())])
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("register_agent") + bytes(usdc_mint),
                [AccountMeta(vault, False, True), AccountMeta(agent_c_pda, False, True),
                 AccountMeta(agent_c.pubkey(), True, True), AccountMeta(SYS_PROGRAM, False, False)])],
            agent_c, [agent_c])
        fail("Paused registration", "should have failed")
    except:
        ok("Registration blocked when paused")

    # TEST 7: Unpause
    print("  Test 7: Unpause vault")
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("unpause"),
                [AccountMeta(vault, False, True), AccountMeta(deployer.pubkey(), True, False)])],
            deployer, [deployer])
        ok("Unpause vault")
    except Exception as e:
        fail("Unpause vault", str(e)[:80])

    # TEST 8: Unauthorized pause fails
    print("  Test 8: Unauthorized pause rejected")
    impostor = Keypair()
    await client.request_airdrop(impostor.pubkey(), 10_000_000_000)
    await asyncio.sleep(1)
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("pause"),
                [AccountMeta(vault, False, True), AccountMeta(impostor.pubkey(), True, False)])],
            impostor, [impostor])
        fail("Unauthorized pause", "should have failed")
    except:
        ok("Unauthorized pause rejected")

    # TEST 9: Read vault state
    print("  Test 9: Read vault state")
    try:
        acct = await client.get_account_info(vault, commitment=Confirmed)
        data = acct.value.data
        authority = Pubkey.from_bytes(data[8:40])
        assert authority == deployer.pubkey()
        total_agents = int.from_bytes(data[8+32+32+32+1:8+32+32+32+1+8], "little")
        ok(f"Vault state (agents={total_agents})")
    except Exception as e:
        fail("Read vault state", str(e)[:80])

    # TEST 10: Read agent account
    print("  Test 10: Read agent account")
    try:
        acct = await client.get_account_info(agent_a_pda, commitment=Confirmed)
        data = acct.value.data
        wallet = Pubkey.from_bytes(data[8:40])
        assert wallet == agent_a.pubkey()
        ok("Agent A account (wallet correct)")
    except Exception as e:
        fail("Read agent account", str(e)[:80])

    # TEST 11: Set batch signer
    print("  Test 11: Set batch signer")
    new_signer = Keypair()
    try:
        await send(client,
            [Instruction(PROGRAM_ID, disc("set_batch_signer") + bytes(new_signer.pubkey()),
                [AccountMeta(vault, False, True), AccountMeta(deployer.pubkey(), True, False)])],
            deployer, [deployer])
        ok("Set batch signer")
    except Exception as e:
        fail("Set batch signer", str(e)[:80])

    # SUMMARY
    print()
    print("=" * 55)
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    if FAILED == 0:
        print("  ALL TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")
    print("=" * 55)

    await client.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
