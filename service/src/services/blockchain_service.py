# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""Blockchain interaction layer — submits multi-token batches to Base."""
import json
import logging
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from ..core.config import settings

logger = logging.getLogger(__name__)

BATCH_SETTLEMENT_ABI = json.loads("""[
    {
        "inputs": [
            {"components": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"},
                {"name": "token", "type": "address"},
                {"name": "paymentId", "type": "bytes32"}
            ], "name": "payments", "type": "tuple[]"},
            {"name": "batchId", "type": "bytes32"},
            {"name": "signature", "type": "bytes"}
        ],
        "name": "submitBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]""")


def get_web3() -> Web3:
    return Web3(Web3.HTTPProvider(settings.rpc_url))


def compute_batch_hash(payments: list[dict], batch_id: bytes) -> bytes:
    """Mirror the contract's computeBatchHash function (now includes token)."""
    payload_hash = Web3.solidity_keccak(["bytes32"], [batch_id])

    for p in payments:
        payload_hash = Web3.solidity_keccak(
            ["bytes32", "address", "address", "uint256", "address", "bytes32"],
            [payload_hash, p["from"], p["to"], p["amount"], p["token"], p["paymentId"]],
        )
    return payload_hash


def sign_batch(payments: list[dict], batch_id: bytes) -> bytes:
    """Sign the batch hash with the API signer key (Keychain or env)."""
    signer_key = settings.get_batch_signer_key()
    if not signer_key:
        raise ValueError("BATCH_SIGNER_PRIVATE_KEY not configured (check Keychain or env)")

    batch_hash = compute_batch_hash(payments, batch_id)
    account = Account.from_key(signer_key)
    message = encode_defunct(batch_hash)
    signed = account.sign_message(message)
    return signed.signature


async def submit_batch_to_chain(
    payments: list[dict],
    batch_id: bytes,
) -> str:
    """Submit a signed batch to the AgioBatchSettlement contract on Base."""
    if not settings.batch_settlement_address:
        logger.warning("No BATCH_SETTLEMENT_ADDRESS — skipping on-chain submission")
        return "0x" + "0" * 64

    submitter_key = settings.get_batch_submitter_key()
    if not submitter_key:
        logger.warning("No BATCH_SUBMITTER_PRIVATE_KEY — skipping on-chain submission")
        return "0x" + "0" * 64

    w3 = get_web3()
    account = Account.from_key(submitter_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(settings.batch_settlement_address),
        abi=BATCH_SETTLEMENT_ABI,
    )

    payment_tuples = [
        (
            Web3.to_checksum_address(p["from"]),
            Web3.to_checksum_address(p["to"]),
            p["amount"],
            Web3.to_checksum_address(p["token"]),
            p["paymentId"],
        )
        for p in payments
    ]

    signature = sign_batch(payments, batch_id)

    nonce = w3.eth.get_transaction_count(account.address)
    tx = contract.functions.submitBatch(
        payment_tuples, batch_id, signature
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 5_000_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
    })

    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

    logger.info(f"Batch submitted: tx={tx_hash.hex()}")
    return tx_hash.hex()


async def wait_for_receipt(tx_hash: str, timeout: int = 120) -> dict:
    """Wait for transaction confirmation."""
    w3 = get_web3()
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return {
            "success": receipt["status"] == 1,
            "gas_used": receipt["gasUsed"],
            "block": receipt["blockNumber"],
        }
    except Exception as e:
        logger.error(f"Receipt wait failed: {e}")
        return {"success": False, "gas_used": 0, "block": 0}
