# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Deposit Watcher — Monitors the vault contract for incoming deposits and credits agent balances.

Runs every 30 seconds. Scans for new Deposited events on the vault contract,
matches the depositor's wallet to their agent account, and credits their
off-chain AgentBalance. Every deposit is logged to the deposit_ledger table
for permanent auditability.

Safety guarantees:
  - Idempotent: tracks last processed block, never double-credits
  - Auditable: every credit is logged with tx hash, block, amount, and timestamp
  - Reconcilable: deposit_ledger can be compared against on-chain events
"""
import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, Column, String, Integer, Numeric, DateTime, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session, engine
from ..core.config import settings
from ..models.agent import Agent, AgentBalance
from ..models.base import Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("deposit_watcher")

CHECK_INTERVAL = 30  # seconds
VAULT_ADDRESS = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
BASE_RPC = os.getenv("RPC_URL", "https://mainnet.base.org")

# ERC20 USDC on Base
TOKEN_MAP = {
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": {"symbol": "USDC", "decimals": 6},
    "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2": {"symbol": "USDT", "decimals": 6},
    "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb": {"symbol": "DAI", "decimals": 18},
    "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "decimals": 18},
    "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22": {"symbol": "cbETH", "decimals": 18},
}

# Vault Deposited event: Deposited(address indexed agent, address indexed token, uint256 amount, uint256 timestamp)
DEPOSITED_EVENT_SIGNATURE = "0x" + "0" * 64  # Will compute below

# ABI for the Deposited event
VAULT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agent", "type": "address"},
            {"indexed": True, "name": "token", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
        ],
        "name": "Deposited",
        "type": "event",
    }
]

# Also watch for direct ERC20 transfers TO the vault (in case someone sends without calling deposit())
ERC20_TRANSFER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    }
]


class DepositLedger(Base):
    """Permanent record of every deposit credited. Used for auditing and reconciliation."""
    __tablename__ = "deposit_ledger"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tx_hash = Column(String(66), nullable=False, unique=True)
    block_number = Column(Integer, nullable=False)
    depositor_address = Column(String(42), nullable=False)
    agent_id = Column(UUID(as_uuid=True), nullable=True)
    agio_id = Column(String(66), nullable=True)
    token_address = Column(String(42), nullable=False)
    token_symbol = Column(String(10), nullable=False)
    amount_raw = Column(Numeric(40, 0), nullable=False)
    amount_human = Column(Numeric(20, 6), nullable=False)
    credited = Column(Boolean, default=False)
    credited_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow)


class DepositWatcherState(Base):
    """Tracks the last processed block to avoid re-scanning."""
    __tablename__ = "deposit_watcher_state"

    id = Column(Integer, primary_key=True, default=1)
    last_block = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


def _get_w3():
    from web3 import Web3
    return Web3(Web3.HTTPProvider(BASE_RPC))


async def _ensure_tables():
    """Create deposit_ledger and deposit_watcher_state tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(DepositLedger.__table__.create, checkfirst=True)
        await conn.run_sync(DepositWatcherState.__table__.create, checkfirst=True)


async def _get_last_block(db: AsyncSession) -> int:
    """Get the last processed block number."""
    state = (await db.execute(select(DepositWatcherState))).scalar_one_or_none()
    if not state:
        w3 = _get_w3()
        current = w3.eth.block_number
        state = DepositWatcherState(id=1, last_block=current - 1000)
        db.add(state)
        await db.commit()
        return current - 1000
    return state.last_block


async def _set_last_block(db: AsyncSession, block: int):
    """Update the last processed block number."""
    state = (await db.execute(select(DepositWatcherState))).scalar_one_or_none()
    if state:
        state.last_block = block
        state.updated_at = datetime.utcnow()
    else:
        db.add(DepositWatcherState(id=1, last_block=block))
    await db.commit()


async def _find_agent_by_wallet(db: AsyncSession, wallet: str):
    """Find an agent by their registered wallet address."""
    wallet_lower = wallet.lower()
    agent = (await db.execute(
        select(Agent).where(Agent.wallet_address.ilike(wallet_lower))
    )).scalar_one_or_none()
    return agent


async def _credit_agent(db: AsyncSession, agent: Agent, token_symbol: str, amount: Decimal):
    """Credit an agent's off-chain balance for a detected deposit."""
    bal = (await db.execute(
        select(AgentBalance).where(
            AgentBalance.agent_id == agent.id,
            AgentBalance.token == token_symbol,
        )
    )).scalar_one_or_none()

    if bal:
        bal.balance = Decimal(str(bal.balance)) + amount
    else:
        bal = AgentBalance(
            id=uuid4(),
            agent_id=agent.id,
            token=token_symbol,
            balance=amount,
            locked_balance=Decimal("0"),
        )
        db.add(bal)

    # Also update legacy balance field
    agent.balance = Decimal(str(agent.balance or 0)) + amount


async def _already_processed(db: AsyncSession, tx_hash: str) -> bool:
    """Check if a transaction has already been processed."""
    existing = (await db.execute(
        select(DepositLedger).where(DepositLedger.tx_hash == tx_hash)
    )).scalar_one_or_none()
    return existing is not None


async def scan_deposits():
    """Scan for new vault deposits and ERC20 transfers to vault."""
    w3 = _get_w3()
    from web3 import Web3

    vault_addr = Web3.to_checksum_address(VAULT_ADDRESS)
    vault_contract = w3.eth.contract(address=vault_addr, abi=VAULT_ABI)
    current_block = w3.eth.block_number

    async with async_session() as db:
        from_block = await _get_last_block(db)
        to_block = min(from_block + 2000, current_block)

        if from_block >= current_block:
            return 0

        logger.info(f"Scanning blocks {from_block} → {to_block} ({to_block - from_block} blocks)")

        credits = 0

        # Method 1: Scan for vault Deposited events
        try:
            deposit_filter = vault_contract.events.Deposited.create_filter(
                fromBlock=from_block, toBlock=to_block
            )
            events = deposit_filter.get_all_entries()

            for event in events:
                tx_hash = event.transactionHash.hex()
                if await _already_processed(db, tx_hash):
                    continue

                depositor = event.args.agent
                token_addr = event.args.token
                amount_raw = event.args.amount
                block_num = event.blockNumber

                token_info = TOKEN_MAP.get(Web3.to_checksum_address(token_addr))
                if not token_info:
                    logger.warning(f"Unknown token {token_addr} in deposit tx {tx_hash}")
                    continue

                amount_human = Decimal(str(amount_raw)) / Decimal(10 ** token_info["decimals"])
                agent = await _find_agent_by_wallet(db, depositor)

                ledger = DepositLedger(
                    id=uuid4(),
                    tx_hash=tx_hash,
                    block_number=block_num,
                    depositor_address=depositor.lower(),
                    agent_id=agent.id if agent else None,
                    agio_id=agent.agio_id if agent else None,
                    token_address=token_addr.lower(),
                    token_symbol=token_info["symbol"],
                    amount_raw=amount_raw,
                    amount_human=amount_human,
                    credited=False,
                    detected_at=datetime.utcnow(),
                )

                if agent:
                    await _credit_agent(db, agent, token_info["symbol"], amount_human)
                    ledger.credited = True
                    ledger.credited_at = datetime.utcnow()
                    logger.info(f"CREDITED: {amount_human} {token_info['symbol']} to {agent.agio_id[:20]}... (tx: {tx_hash[:16]}...)")
                    credits += 1
                else:
                    ledger.error = f"No agent found for wallet {depositor}"
                    logger.warning(f"UNCREDITED deposit: {amount_human} {token_info['symbol']} from {depositor} — no matching agent")

                db.add(ledger)

        except Exception as e:
            logger.error(f"Vault event scan error: {e}", exc_info=True)

        # Method 2: Scan for direct ERC20 transfers TO the vault
        for token_addr, token_info in TOKEN_MAP.items():
            try:
                token_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token_addr),
                    abi=ERC20_TRANSFER_ABI,
                )
                transfer_filter = token_contract.events.Transfer.create_filter(
                    fromBlock=from_block,
                    toBlock=to_block,
                    argument_filters={"to": vault_addr},
                )
                transfers = transfer_filter.get_all_entries()

                for event in transfers:
                    tx_hash = event.transactionHash.hex()
                    if await _already_processed(db, tx_hash):
                        continue

                    sender = event.args["from"]
                    amount_raw = event.args.value
                    block_num = event.blockNumber
                    amount_human = Decimal(str(amount_raw)) / Decimal(10 ** token_info["decimals"])

                    if amount_human < Decimal("0.001"):
                        continue

                    agent = await _find_agent_by_wallet(db, sender)

                    ledger = DepositLedger(
                        id=uuid4(),
                        tx_hash=tx_hash,
                        block_number=block_num,
                        depositor_address=sender.lower(),
                        agent_id=agent.id if agent else None,
                        agio_id=agent.agio_id if agent else None,
                        token_address=token_addr.lower(),
                        token_symbol=token_info["symbol"],
                        amount_raw=amount_raw,
                        amount_human=amount_human,
                        credited=False,
                        detected_at=datetime.utcnow(),
                    )

                    if agent:
                        await _credit_agent(db, agent, token_info["symbol"], amount_human)
                        ledger.credited = True
                        ledger.credited_at = datetime.utcnow()
                        logger.info(f"CREDITED (transfer): {amount_human} {token_info['symbol']} to {agent.agio_id[:20]}... (tx: {tx_hash[:16]}...)")
                        credits += 1
                    else:
                        ledger.error = f"No agent found for wallet {sender}"
                        logger.warning(f"UNCREDITED transfer: {amount_human} {token_info['symbol']} from {sender}")

                    db.add(ledger)

            except Exception as e:
                logger.error(f"Transfer scan error for {token_info['symbol']}: {e}", exc_info=True)

        await _set_last_block(db, to_block)
        await db.commit()

        if credits:
            logger.info(f"Credited {credits} deposits in blocks {from_block}–{to_block}")

        return credits


async def run_watcher():
    """Main loop — scan for deposits every 30 seconds."""
    logger.info(f"Deposit watcher started. Vault: {VAULT_ADDRESS}. Interval: {CHECK_INTERVAL}s")
    await _ensure_tables()

    while True:
        try:
            await scan_deposits()
        except Exception as e:
            logger.error(f"Deposit watcher error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_watcher())
