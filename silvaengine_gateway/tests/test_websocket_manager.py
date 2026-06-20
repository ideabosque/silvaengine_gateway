# -*- coding: utf-8 -*-
"""Tests for the WebSocket ConnectionManager."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from silvaengine_gateway.websocket_manager import ConnectionManager


class TestConnectionManager:
    """Unit tests for the ConnectionManager."""

    @pytest.fixture
    def manager(self):
        return ConnectionManager()

    def test_connection_manager_register_unregister(self, manager):
        """Connection count and active ids update correctly."""
        ws1 = MagicMock()
        ws2 = MagicMock()
        assert manager.connection_count == 0
        assert manager.active_connections == []

        manager.register("conn-1", ws1)
        assert manager.connection_count == 1
        assert "conn-1" in manager.active_connections

        manager.register("conn-2", ws2)
        assert manager.connection_count == 2
        assert "conn-2" in manager.active_connections

        manager.unregister("conn-1")
        assert manager.connection_count == 1
        assert "conn-1" not in manager.active_connections

        manager.unregister("conn-2")
        assert manager.connection_count == 0
        assert manager.active_connections == []

    def test_connection_manager_unregister_unknown(self, manager):
        """Unregistering an unknown connection_id is a no-op."""
        manager.unregister("nonexistent")
        assert manager.connection_count == 0

    def test_connection_manager_send_missing(self, manager):
        """Unknown connection returns False."""
        result = manager.send_to_connection("unknown", {"hello": "world"})
        assert result is False

    def test_connection_manager_send_no_loop(self, manager):
        """Send returns False when no event loop is set."""
        ws = MagicMock()
        manager.register("conn-1", ws)
        result = manager.send_to_connection("conn-1", {"hello": "world"})
        assert result is False

    def test_connection_manager_send_from_thread(self, manager):
        """Sync thread can schedule a send on the event loop."""
        ws = MagicMock()
        ws.send_text = AsyncMock()

        # Set up a real event loop in a separate thread
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        manager.set_event_loop(loop)

        manager.register("conn-1", ws)
        result = manager.send_to_connection("conn-1", {"hello": "world"})

        assert result is True
        # Give the event loop time to process the coroutine
        import time

        time.sleep(0.1)
        ws.send_text.assert_called_once()
        sent_payload = ws.send_text.call_args[0][0]
        assert json.loads(sent_payload) == {"hello": "world"}

        # Cleanup
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()


    def test_connection_manager_drain_pending_sends(self, manager):
        """Drain waits until scheduled sends have reached the WebSocket."""
        sent_payloads = []

        async def run_case():
            manager.set_event_loop(asyncio.get_running_loop())

            ws = MagicMock()

            async def send_text(payload):
                await asyncio.sleep(0.01)
                sent_payloads.append(payload)

            ws.send_text = send_text
            manager.register("conn-1", ws)

            assert manager.send_to_connection("conn-1", {"index": 1}) is True
            assert manager.send_to_connection("conn-1", {"index": 2}) is True
            assert await manager.drain_pending_sends("conn-1", timeout=1.0) is True

        asyncio.run(run_case())

        assert [json.loads(payload)["index"] for payload in sent_payloads] == [1, 2]

    def test_connection_manager_send_string_data(self, manager):
        """String data is sent as-is (no JSON serialization)."""
        ws = MagicMock()
        ws.send_text = AsyncMock()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        manager.set_event_loop(loop)

        manager.register("conn-1", ws)
        manager.send_to_connection("conn-1", "raw string payload")

        import time

        time.sleep(0.1)
        ws.send_text.assert_called_once_with("raw string payload")

        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()