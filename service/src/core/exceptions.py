# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""Custom exceptions with HTTP status codes."""
from fastapi import HTTPException


class InsufficientBalance(HTTPException):
    def __init__(self, available: float, requested: float):
        super().__init__(
            status_code=400,
            detail=f"Insufficient balance: available {available}, requested {requested}",
        )


class AgentNotFound(HTTPException):
    def __init__(self, identifier: str):
        super().__init__(status_code=404, detail=f"Agent not found: {identifier}")


class DuplicateAgent(HTTPException):
    def __init__(self):
        super().__init__(status_code=409, detail="Agent already registered")
