"""Chain registry model — tracks supported chains, tokens, and reserve balances."""
import uuid
from sqlalchemy import String, Integer, Numeric, Boolean, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class SupportedChain(Base):
    __tablename__ = "supported_chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    chain_name: Mapped[str] = mapped_column(String(50), nullable=False)
    rpc_url: Mapped[str] = mapped_column(Text, nullable=False)
    usdc_address: Mapped[str] = mapped_column(String(42), nullable=False)
    vault_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    swap_router_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    cctp_domain: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserve_balance: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    min_reserve: Mapped[float] = mapped_column(Numeric(20, 6), default=1000)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    gas_price_gwei: Mapped[float] = mapped_column(Numeric(20, 10), default=0)
    avg_block_time_ms: Mapped[int] = mapped_column(Integer, default=2000)


class SupportedToken(Base):
    """Tokens supported on each chain. Maps token symbols to on-chain addresses."""
    __tablename__ = "supported_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    contract_address: Mapped[str] = mapped_column(String(42), nullable=False)
    decimals: Mapped[int] = mapped_column(Integer, default=6)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("idx_supported_tokens_chain_symbol", "chain_id", "symbol", unique=True),
    )


# Base mainnet token addresses (Level 1)
BASE_TOKENS = {
    "USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    "USDT": {"address": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "decimals": 6},
    "DAI":  {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    "WETH": {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    "cbETH": {"address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18},
}

SWAP_FEE_BPS = 30  # 0.3% for cross-token swaps
