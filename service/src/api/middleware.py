# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""API middleware — rate limiting, logging, CORS."""
import time
import logging
from collections import defaultdict
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("agio.api")

# Simple in-memory rate limiter (use Redis for production horizontal scaling)
_request_counts: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 100  # requests per minute
RATE_WINDOW = 60  # seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean old entries
        _request_counts[client_ip] = [
            t for t in _request_counts[client_ip] if now - t < RATE_WINDOW
        ]

        if len(_request_counts[client_ip]) >= RATE_LIMIT:
            return Response(
                content='{"detail":"Rate limit exceeded"}',
                status_code=429,
                media_type="application/json",
            )

        _request_counts[client_ip].append(now)

        # Log request
        start = time.time()
        response = await call_next(request)
        elapsed = (time.time() - start) * 1000
        logger.info(f"{request.method} {request.url.path} {response.status_code} {elapsed:.0f}ms")

        return response
