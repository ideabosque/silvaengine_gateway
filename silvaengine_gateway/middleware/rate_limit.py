# -*- coding: utf-8 -*-
"""
RateLimitMiddleware — per-IP request rate limiting.

Applied globally to the gateway app. Configurable via GatewayConfig.
"""

from __future__ import print_function

__author__ = "silvaengine"

import time
from collections import defaultdict
from typing import Dict, List

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = __import__("logging").getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client-IP sliding-window rate limiter."""

    def __init__(
        self,
        app,
        max_requests: int = 100,
        window_seconds: int = 60,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._counts: Dict[str, List[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip rate limiting for health check
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Prune old entries
        self._counts[client_ip] = [
            t for t in self._counts[client_ip] if now - t < self.window_seconds
        ]

        if len(self._counts[client_ip]) >= self.max_requests:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        self._counts[client_ip].append(now)
        return await call_next(request)