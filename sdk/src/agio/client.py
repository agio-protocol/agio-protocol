"""
AgioClient — Multi-token developer interface for AGIO.

For the demo, this talks directly to contracts (no API server needed).
In production, it would call the AGIO API which handles batching.
"""
from __future__ import annotations

import json
import hashlib
from typing import Optional
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from .models import PaymentReceipt, Balance, TokenBalances, AgentInfo
from .errors import InsufficientBalance, AgentNotRegistered

VAULT_ABI = json.loads("""[
    {"inputs":[{"name":"token","type":"address"},{"name":"amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"token","type":"address"},{"name":"amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"agent","type":"address"},{"name":"token","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"agent","type":"address"},{"name":"token","type":"address"}],"name":"lockedBalanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"token","type":"address"}],"name":"checkInvariant","outputs":[{"name":"ok","type":"bool"},{"name":"tracked","type":"uint256"},{"name":"actual","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"token","type":"address"}],"name":"isWhitelistedToken","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getWhitelistedTokens","outputs":[{"type":"address[]"}],"stateMutability":"view","type":"function"}
]""")

BATCH_ABI = json.loads("""[
    {"inputs":[
        {"components":[
            {"name":"from","type":"address"},{"name":"to","type":"address"},
            {"name":"amount","type":"uint256"},{"name":"token","type":"address"},
            {"name":"paymentId","type":"bytes32"}
        ],"name":"payments","type":"tuple[]"},
        {"name":"batchId","type":"bytes32"},
        {"name":"signature","type":"bytes"}
    ],"name":"submitBatch","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"batchId","type":"bytes32"}],"name":"getBatchDetails","outputs":[
        {"components":[
            {"name":"batchId","type":"bytes32"},{"name":"timestamp","type":"uint64"},
            {"name":"totalPayments","type":"uint32"},{"name":"totalVolume","type":"uint256"},
            {"name":"submitter","type":"address"},{"name":"status","type":"uint8"}
        ],"type":"tuple"}
    ],"stateMutability":"view","type":"function"}
]""")

SWAP_ROUTER_ABI = json.loads("""[
    {"inputs":[{"name":"token","type":"address"}],"name":"setPreferredToken","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"agent","type":"address"}],"name":"preferredToken","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"receiver","type":"address"},{"name":"senderToken","type":"address"}],"name":"needsSwap","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"amount","type":"uint256"}],"name":"calculateSwapFee","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}
]""")

REGISTRY_ABI = json.loads("""[
    {"inputs":[{"name":"agentId","type":"bytes32"},{"name":"metadata","type":"string"}],"name":"registerAgent","outputs":[],"stateMutability":"payable","type":"function"},
    {"inputs":[{"name":"wallet","type":"address"}],"name":"getAgent","outputs":[
        {"components":[
            {"name":"agentId","type":"bytes32"},{"name":"wallet","type":"address"},
            {"name":"registeredAt","type":"uint64"},{"name":"totalPayments","type":"uint64"},
            {"name":"totalVolume","type":"uint256"},{"name":"metadata","type":"string"},
            {"name":"tier","type":"uint8"}
        ],"type":"tuple"}
    ],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"wallet","type":"address"}],"name":"isRegistered","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"}
]""")

ERC20_ABI = json.loads("""[
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"mint","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}
]""")


class AgioClient:
    """AGIO SDK client — multi-token: pay(), balance(), deposit(), withdraw()."""

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        vault_address: str,
        batch_address: str,
        registry_address: str,
        token_addresses: dict[str, str] | None = None,
        swap_router_address: str | None = None,
        signer_key: Optional[str] = None,
    ):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self._private_key = private_key
        self._signer_key = signer_key or private_key

        self.vault = self.w3.eth.contract(address=Web3.to_checksum_address(vault_address), abi=VAULT_ABI)
        self.batch_contract = self.w3.eth.contract(address=Web3.to_checksum_address(batch_address), abi=BATCH_ABI)
        self.registry = self.w3.eth.contract(address=Web3.to_checksum_address(registry_address), abi=REGISTRY_ABI)

        self._tokens: dict[str, dict] = {}
        if token_addresses:
            for symbol, addr in token_addresses.items():
                checksum = Web3.to_checksum_address(addr)
                contract = self.w3.eth.contract(address=checksum, abi=ERC20_ABI)
                self._tokens[symbol] = {"address": checksum, "contract": contract}

        self.swap_router = None
        if swap_router_address:
            self.swap_router = self.w3.eth.contract(
                address=Web3.to_checksum_address(swap_router_address), abi=SWAP_ROUTER_ABI
            )

        self._pending_payments: list[dict] = []

    def _get_token(self, symbol: str) -> dict:
        if symbol not in self._tokens:
            raise ValueError(f"Token '{symbol}' not configured. Available: {list(self._tokens.keys())}")
        return self._tokens[symbol]

    def _send_tx(self, tx_func, value=0):
        tx = tx_func.build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 500_000,
            "gasPrice": self.w3.eth.gas_price,
            "value": value,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        return receipt

    def register(self, name: str = "agent") -> str:
        if self.registry.functions.isRegistered(self.address).call():
            info = self.registry.functions.getAgent(self.address).call()
            return "0x" + info[0].hex()

        agent_id = Web3.solidity_keccak(
            ["address", "string"], [self.address, name]
        )
        metadata = json.dumps({"name": name})
        self._send_tx(self.registry.functions.registerAgent(agent_id, metadata))
        return "0x" + agent_id.hex()

    def deposit(self, amount: float, token: str = "USDC"):
        """Deposit any whitelisted token into the AGIO vault."""
        tok = self._get_token(token)
        decimals = tok["contract"].functions.decimals().call()
        amount_base = int(amount * (10 ** decimals))
        self._send_tx(tok["contract"].functions.approve(self.vault.address, amount_base))
        self._send_tx(self.vault.functions.deposit(tok["address"], amount_base))

    def withdraw(self, amount: float, token: str = "USDC"):
        """Withdraw any token from the AGIO vault."""
        tok = self._get_token(token)
        decimals = tok["contract"].functions.decimals().call()
        amount_base = int(amount * (10 ** decimals))
        self._send_tx(self.vault.functions.withdraw(tok["address"], amount_base))

    def pay(self, to: str, amount: float, token: str = "USDC", memo: str = "") -> PaymentReceipt:
        """Pay another agent. Queues for next batch. Call flush() to settle."""
        tok = self._get_token(token)
        bal = self.balance(token)
        if bal.available < amount:
            raise InsufficientBalance(bal.available, amount)

        decimals = tok["contract"].functions.decimals().call()
        amount_base = int(amount * (10 ** decimals))

        payment_id = Web3.solidity_keccak(
            ["address", "address", "uint256", "string"],
            [self.address, Web3.to_checksum_address(to), amount_base, str(len(self._pending_payments))]
        )

        self._pending_payments.append({
            "from": self.address,
            "to": Web3.to_checksum_address(to),
            "amount": amount_base,
            "token": tok["address"],
            "paymentId": payment_id,
            "amount_human": amount,
            "symbol": token,
        })

        return PaymentReceipt(
            payment_id="0x" + payment_id.hex(),
            status="QUEUED",
            amount=amount,
            from_token=token,
        )

    def balance(self, token: str = "USDC") -> Balance:
        """Check this agent's balance for a specific token."""
        tok = self._get_token(token)
        decimals = tok["contract"].functions.decimals().call()
        available = self.vault.functions.balanceOf(self.address, tok["address"]).call() / (10 ** decimals)
        locked = self.vault.functions.lockedBalanceOf(self.address, tok["address"]).call() / (10 ** decimals)
        return Balance(available=available, locked=locked, total=available + locked)

    def all_balances(self) -> dict[str, Balance]:
        """Check balances across all configured tokens."""
        result = {}
        for symbol in self._tokens:
            result[symbol] = self.balance(symbol)
        return result

    def set_preferred_token(self, token: str):
        """Set your preferred receive token (used for cross-token payments)."""
        tok = self._get_token(token)
        if self.swap_router is None:
            raise ValueError("Swap router not configured")
        self._send_tx(self.swap_router.functions.setPreferredToken(tok["address"]))

    def flush(self) -> str:
        """Settle all pending payments in a single on-chain batch."""
        if not self._pending_payments:
            return "no_payments"

        payment_tuples = [
            (p["from"], p["to"], p["amount"], p["token"], p["paymentId"])
            for p in self._pending_payments
        ]

        batch_id = Web3.solidity_keccak(
            ["string"], [json.dumps([p["paymentId"].hex() for p in self._pending_payments])]
        )

        payload_hash = Web3.solidity_keccak(["bytes32"], [batch_id])
        for p in self._pending_payments:
            payload_hash = Web3.solidity_keccak(
                ["bytes32", "address", "address", "uint256", "address", "bytes32"],
                [payload_hash, p["from"], p["to"], p["amount"], p["token"], p["paymentId"]],
            )

        signer = Account.from_key(self._signer_key)
        message = encode_defunct(payload_hash)
        signature = signer.sign_message(message).signature

        tx = self.batch_contract.functions.submitBatch(
            payment_tuples, batch_id, signature
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 5_000_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        count = len(self._pending_payments)
        self._pending_payments.clear()

        return f"0x{tx_hash.hex()} ({count} payments, gas={receipt['gasUsed']})"

    def mint_test_token(self, amount: float, token: str = "USDC"):
        """Mint testnet tokens (only works with MockUSDC on testnet)."""
        tok = self._get_token(token)
        decimals = tok["contract"].functions.decimals().call()
        self._send_tx(tok["contract"].functions.mint(self.address, int(amount * (10 ** decimals))))

    def check_invariant(self, token: str = "USDC") -> tuple[bool, float, float]:
        """Verify the vault's books balance for a specific token."""
        tok = self._get_token(token)
        decimals = tok["contract"].functions.decimals().call()
        ok, tracked, actual = self.vault.functions.checkInvariant(tok["address"]).call()
        return ok, tracked / (10 ** decimals), actual / (10 ** decimals)
