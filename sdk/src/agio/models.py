"""Pydantic models for SDK requests and responses."""
from pydantic import BaseModel
from typing import Optional


class PaymentReceipt(BaseModel):
    payment_id: str
    status: str
    amount: float
    from_token: str = "USDC"
    to_token: Optional[str] = None
    swap_fee: float = 0.0
    tx_hash: Optional[str] = None
    batch_id: Optional[str] = None


class Balance(BaseModel):
    available: float
    locked: float
    total: float


class TokenBalances(BaseModel):
    """All token balances for an agent."""
    balances: dict[str, Balance]
    preferred_token: str = "USDC"


class AgentInfo(BaseModel):
    agio_id: str
    wallet: str
    tier: str
    balance: Balance
    preferred_token: str = "USDC"
    total_payments: int = 0
    total_volume: float = 0.0
