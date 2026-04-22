"""
BaseAgent — Foundation for all AGIO agents.

Handles registration, payment, logging, and status reporting.
All 5 test agents inherit from this.
"""
from __future__ import annotations

import os
import sys
import time
import asyncio
from datetime import datetime

# Add SDK to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "src"))

# Add service to path for direct DB access in demo mode
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))


class BaseAgent:
    """Base class all AGIO agents inherit from."""

    def __init__(self, agent_name: str, service_type: str | None = None, price: float = 0):
        self.agent_name = agent_name
        self.service_type = service_type
        self.price = price
        self.agio_id: str = ""
        self.payments_received: list[dict] = []
        self.payments_sent: list[dict] = []
        self.total_earned: float = 0
        self.total_spent: float = 0
        self._db_session = None
        self._db_agent = None

    async def setup(self, db_session, initial_balance: float = 0):
        """Register agent with AGIO via direct DB (demo mode)."""
        from decimal import Decimal
        from src.models.agent import Agent
        from src.services.registry_service import register_agent

        self._db_session = db_session

        result = await register_agent(db_session, f"0x{hash(self.agent_name) & 0xFFFFFFFFFFFFFFFF:040x}", self.agent_name)
        self.agio_id = result["agio_id"]

        if initial_balance > 0:
            from sqlalchemy import select, update
            await db_session.execute(
                update(Agent).where(Agent.agio_id == self.agio_id).values(
                    balance=Decimal(str(initial_balance))
                )
            )
            await db_session.commit()

        self.log(f"Registered: {self.agio_id[:16]}... | Balance: ${initial_balance}")

    async def pay(self, to_agent: "BaseAgent", amount: float, memo: str = "") -> dict:
        """Pay another agent through AGIO."""
        from src.services.payment_service import create_payment

        result = await create_payment(
            self._db_session, self.agio_id, to_agent.agio_id, amount, memo
        )
        self.total_spent += amount
        self.payments_sent.append(result)
        return result

    async def get_balance(self) -> float:
        from sqlalchemy import select
        from src.models.agent import Agent
        agent = (await self._db_session.execute(
            select(Agent).where(Agent.agio_id == self.agio_id)
        )).scalar_one()
        return float(agent.balance)

    async def get_tier(self) -> str:
        from sqlalchemy import select
        from src.models.agent import Agent
        agent = (await self._db_session.execute(
            select(Agent).where(Agent.agio_id == self.agio_id)
        )).scalar_one()
        return agent.tier or "SPARK"

    async def get_total_payments(self) -> int:
        from sqlalchemy import select
        from src.models.agent import Agent
        agent = (await self._db_session.execute(
            select(Agent).where(Agent.agio_id == self.agio_id)
        )).scalar_one()
        return agent.total_payments

    def log(self, message: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        print(f"  [{ts}] [{self.agent_name}] {message}")

    async def report(self) -> dict:
        bal = await self.get_balance()
        tier = await self.get_tier()
        return {
            "agent": self.agent_name,
            "agio_id": self.agio_id[:16] + "...",
            "balance": f"${bal:.6f}",
            "tier": tier,
            "earned": f"${self.total_earned:.6f}",
            "spent": f"${self.total_spent:.6f}",
            "payments_sent": len(self.payments_sent),
            "payments_received": len(self.payments_received),
        }
