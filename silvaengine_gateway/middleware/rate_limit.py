# -*- coding: utf-8 -*-
"""
RateLimitMiddleware — per-IP request rate limiting.

Applied globally to the gateway app. The counting store is pluggable:

- ``InMemoryRateLimitStore`` — per-process sliding window. Fine for a single
  process; with ``workers > 1`` each worker counts independently, so the
  effective limit is ``max_requests * workers``.
- ``DynamoDBRateLimitStore`` — a shared fixed-window atomic counter in DynamoDB,
  so the limit is enforced across all worker processes.

Select via the gateway ``rate_limit_backend`` setting
(``GATEWAY_RATE_LIMIT_BACKEND`` = ``memory`` | ``dynamodb``).
"""

from __future__ import print_function

__author__ = "silvaengine"

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger(__name__)


class RateLimitStore:
    """Counts requests per key and decides whether one more is allowed.

    ``is_blocking`` tells the middleware whether ``hit`` performs network I/O and
    must be offloaded from the event loop.
    """

    is_blocking: bool = False

    def hit(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """Record a request for ``key``; return True if it is within the limit."""
        raise NotImplementedError


class InMemoryRateLimitStore(RateLimitStore):
    """Per-process sliding-window counter."""

    is_blocking = False

    def __init__(self) -> None:
        self._counts: Dict[str, List[float]] = defaultdict(list)

    def hit(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        recent = [t for t in self._counts[key] if now - t < window_seconds]

        if len(recent) >= max_requests:
            self._counts[key] = recent
            return False

        recent.append(now)
        self._counts[key] = recent

        # Drop idle keys so the map does not grow unbounded over time.
        if len(self._counts) > 10000:
            self._prune_idle(now, window_seconds)
        return True

    def _prune_idle(self, now: float, window_seconds: int) -> None:
        stale = [
            k
            for k, hits in self._counts.items()
            if not hits or now - hits[-1] >= window_seconds
        ]
        for k in stale:
            del self._counts[k]


class DynamoDBRateLimitStore(RateLimitStore):
    """Shared fixed-window counter backed by a DynamoDB table.

    The table must have a string hash key named ``rl_key``. Enable DynamoDB TTL
    on the ``expires_at`` attribute so expired window rows are reclaimed.

    A fixed window (rather than a sliding one) keeps the cross-process counter a
    single atomic ``ADD`` per request: the key embeds the window index, so each
    window starts a fresh counter and old windows age out via TTL.
    """

    is_blocking = True

    def __init__(self, table: Any) -> None:
        self._table = table

    def hit(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_index = now // max(window_seconds, 1)
        rl_key = f"{key}:{window_index}"
        expires_at = (window_index + 1) * window_seconds + window_seconds

        try:
            resp = self._table.update_item(
                Key={"rl_key": rl_key},
                UpdateExpression=(
                    "SET expires_at = if_not_exists(expires_at, :exp) ADD #c :one"
                ),
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues={":one": 1, ":exp": expires_at},
                ReturnValues="UPDATED_NEW",
            )
            count = int(resp["Attributes"]["count"])
            return count <= max_requests
        except Exception as e:
            # Fail open: a rate-limit store outage must not take down the API.
            logger.warning("Rate-limit store error for %s — allowing: %s", rl_key, e)
            return True


def make_dynamodb_rate_limit_store(
    table_name: str,
    *,
    region_name: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
) -> "DynamoDBRateLimitStore":
    """Build a DynamoDBRateLimitStore from AWS credentials and a table name."""
    import boto3

    resource_kwargs: Dict[str, Any] = {}
    if region_name:
        resource_kwargs["region_name"] = region_name
    if aws_access_key_id and aws_secret_access_key:
        resource_kwargs["aws_access_key_id"] = aws_access_key_id
        resource_kwargs["aws_secret_access_key"] = aws_secret_access_key

    table = boto3.resource("dynamodb", **resource_kwargs).Table(table_name)
    return DynamoDBRateLimitStore(table)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client-IP rate limiter over a pluggable :class:`RateLimitStore`."""

    def __init__(
        self,
        app,
        max_requests: int = 100,
        window_seconds: int = 60,
        store: Optional[RateLimitStore] = None,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.store = store or InMemoryRateLimitStore()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip rate limiting for health check
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        if self.store.is_blocking:
            # Offload network I/O so the event loop is not blocked.
            import asyncio

            allowed = await asyncio.get_running_loop().run_in_executor(
                None,
                self.store.hit,
                client_ip,
                self.max_requests,
                self.window_seconds,
            )
        else:
            allowed = self.store.hit(
                client_ip, self.max_requests, self.window_seconds
            )

        if not allowed:
            # Return a Response directly. Raising HTTPException inside Starlette
            # middleware bypasses FastAPI's exception handlers and surfaces as a
            # 500 rather than a 429.
            return JSONResponse(
                status_code=429, content={"detail": "Rate limit exceeded"}
            )

        return await call_next(request)
