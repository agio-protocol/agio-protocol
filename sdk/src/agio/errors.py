"""Custom exceptions with helpful messages."""


class AgioError(Exception):
    pass


class InsufficientBalance(AgioError):
    def __init__(self, available: float, requested: float):
        super().__init__(
            f"Insufficient balance: you have {available} USDC but tried to send {requested}. "
            f"Deposit more USDC using client.deposit({requested - available})"
        )
        self.available = available
        self.requested = requested


class AgentNotRegistered(AgioError):
    def __init__(self):
        super().__init__(
            "This agent is not registered with AGIO. "
            "Call client.register() first."
        )


class BatchSettlementFailed(AgioError):
    def __init__(self, batch_id: str, reason: str):
        super().__init__(f"Batch {batch_id} failed: {reason}")
