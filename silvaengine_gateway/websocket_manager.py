# -*- coding: utf-8 -*-
"""
Thread-safe WebSocket connection manager for the SilvaEngine Gateway.

Tracks active WebSocket connections by ``connection_id`` and provides a
sync-safe ``send_to_connection()`` API so that core-engine handlers running
in worker threads can push streaming chunks onto the running event loop
via ``asyncio.run_coroutine_threadsafe()``.

This manager is the SilvaEngine Gateway counterpart to the AWS API Gateway
``post_to_connection()`` path.  When a ``ConnectionManager`` is injected
into the core ``Config`` at startup, ``send_data_to_stream`` uses it
instead of the boto3 API Gateway client.  When no manager is configured
(Lambda deployments), the AWS path runs unchanged.
"""

from __future__ import print_function

__author__ = "silvaengine"

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


def _log_send_error(future: Future[Any]) -> None:
    """Done-callback that logs exceptions from fire-and-forget sends."""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("WebSocket send failed: %s", exc, exc_info=exc)


class ConnectionManager:
    """Thread-safe registry of active WebSocket connections.

    The registry is protected by a re-entrant lock because FastAPI
    connection handlers (async) and sync dispatch threads can touch it
    concurrently.  Sends are fire-and-forget with a done-callback so
    exceptions are logged rather than silently discarded.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}
        self._pending_sends: Dict[str, List[Future[Any]]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.RLock()

    # ── Event-loop lifecycle ─────────────────────────────────────────

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the running event loop — called during lifespan startup."""
        self._loop = loop

    # ── Registration ─────────────────────────────────────────────────

    def register(self, connection_id: str, websocket: WebSocket) -> None:
        """Associate *connection_id* with *websocket*."""
        with self._lock:
            self._connections[connection_id] = websocket
            self._pending_sends.setdefault(connection_id, [])

    def unregister(self, connection_id: str) -> None:
        """Remove *connection_id* (e.g. on disconnect or send failure)."""
        with self._lock:
            self._connections.pop(connection_id, None)
            self._pending_sends.pop(connection_id, None)

    # ── Send ─────────────────────────────────────────────────────────

    def send_to_connection(self, connection_id: str, data: Any) -> bool:
        """Send *data* to the connection identified by *connection_id*.

        Called from sync dispatch threads.  Schedules
        ``websocket.send_text(payload)`` on the event loop via
        ``asyncio.run_coroutine_threadsafe()``.

        Returns ``True`` if the send was scheduled, ``False`` if the
        connection is unknown or the event loop is unavailable.
        """
        with self._lock:
            websocket = self._connections.get(connection_id)
            loop = self._loop

        if websocket is None:
            return False
        if loop is None or loop.is_closed():
            return False

        payload = data if isinstance(data, str) else json.dumps(data, default=str)
        future = asyncio.run_coroutine_threadsafe(
            websocket.send_text(payload), loop
        )
        with self._lock:
            self._pending_sends.setdefault(connection_id, []).append(future)

        def _on_done(done_future: Future[Any]) -> None:
            with self._lock:
                pending = self._pending_sends.get(connection_id)
                if pending is not None:
                    try:
                        pending.remove(done_future)
                    except ValueError:
                        pass
            _log_send_error(done_future)

        future.add_done_callback(_on_done)
        return True

    async def drain_pending_sends(
        self,
        connection_id: str,
        timeout: float = 5.0,
    ) -> bool:
        """Wait for all currently queued sends for *connection_id* to finish.

        Streaming sends are scheduled from sync worker threads onto the
        FastAPI event loop. WebSocket route handlers call this after dispatch
        returns so chunk frames are flushed before the trailing dispatch result
        is sent to the client.
        """
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                pending = list(self._pending_sends.get(connection_id, []))

            pending = [future for future in pending if not future.done()]
            if not pending:
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            wrapped = [asyncio.wrap_future(future) for future in pending]
            done, _pending = await asyncio.wait(
                wrapped,
                timeout=remaining,
                return_when=asyncio.ALL_COMPLETED,
            )
            if len(done) != len(wrapped):
                return False

    # ── Inspection ───────────────────────────────────────────────────

    @property
    def active_connections(self) -> List[str]:
        """Return a snapshot of registered connection IDs."""
        with self._lock:
            return list(self._connections.keys())

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        with self._lock:
            return len(self._connections)

    async def shutdown(self) -> None:
        """Close all active connections during shutdown."""
        with self._lock:
            connections = list(self._connections.items())
            self._connections.clear()
            self._pending_sends.clear()

        for conn_id, ws in connections:
            try:
                await ws.close(code=1001, reason="Gateway shutting down")
            except Exception as exc:
                logger.warning("Error closing connection %s: %s", conn_id, exc)