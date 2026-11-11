# -*- coding: utf-8 -*-
"""Tests for the SSE replay cursor (``id:`` framing + ``Last-Event-ID`` replay).

Offline only: a fake SSE manager mimics the shared daemon's
``silvaengine_daemon.sse.SSEManager`` API (``add_client`` /
``get_missed_messages`` / ``remove_client``) and assigns increasing message
ids, so no live service is required.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request

from silvaengine_gateway import router_builder
from silvaengine_gateway.router_builder import _make_sse_handler

# ---------------------------------------------------------------------------
# Fake SSE manager (mimics silvaengine_daemon.sse.SSEManager)
# ---------------------------------------------------------------------------


class FakeSSEManager:
    """Minimal stand-in for the shared daemon's SSEManager.

    Reproduces the daemon's relevant behaviours:

    * ``add_client`` returns ``(client_id, queue)`` and remembers the
      client's partition;
    * ``get_missed_messages`` returns history entries with ``id >
      last_event_id`` and accepts entries whose ``partition_key`` is missing
      (the daemon treats a missing partition as "matches every partition");
    * ``remove_client`` records the disconnect.
    """

    def __init__(self, history: Optional[List[Dict[str, Any]]] = None) -> None:
        self.history: List[Dict[str, Any]] = list(history or [])
        self.removed: List[Tuple[int, str]] = []
        self.client_partitions: Dict[int, str] = {}
        self.queues: Dict[int, asyncio.Queue] = {}
        self._next_client_id = 1

    # -- daemon API ------------------------------------------------------
    async def add_client(self, username: str, partition_key: str = ""):
        client_id = self._next_client_id
        self._next_client_id += 1
        queue: asyncio.Queue = asyncio.Queue()
        self.queues[client_id] = queue
        self.client_partitions[client_id] = partition_key
        return client_id, queue

    async def get_missed_messages(
        self, last_event_id: Optional[str], partition_key: str = ""
    ) -> List[Dict[str, Any]]:
        if not last_event_id or not str(last_event_id).isdigit():
            return []
        last = int(last_event_id)
        return [
            msg
            for msg in self.history
            if msg.get("id", 0) > last
            and (not partition_key or msg.get("partition_key") in (None, partition_key))
        ]

    async def remove_client(self, client_id: int, username: str) -> bool:
        self.removed.append((client_id, username))
        self.queues.pop(client_id, None)
        return True

    # -- test helpers ----------------------------------------------------
    def publish(
        self,
        message: Dict[str, Any],
        message_id: int,
        partition_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a message to the history, as the daemon's broadcast does."""
        stored = dict(message, id=message_id)
        if partition_key is not None:
            stored["partition_key"] = partition_key
        self.history.append(stored)
        return stored


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    headers: Optional[Dict[str, str]] = None,
    username: str = "alice",
    endpoint_id: str = "a2a",
) -> Request:
    """Build a Starlette Request the SSE handler can consume."""
    hdrs = {"Part-Id": "default"}
    hdrs.update(headers or {})
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in hdrs.items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/{endpoint_id}/a2a_sse",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "path_params": {"endpoint_id": endpoint_id},
    }
    request = Request(scope)
    request.state.user = {"username": username}
    return request


def _install_manager(monkeypatch, manager: FakeSSEManager) -> None:
    """Make the SSE handler resolve to *manager*."""

    def _fake_resolve(ref_path: str, *, require_callable: bool = True) -> Any:
        return manager

    monkeypatch.setattr(router_builder, "_resolve_ref", _fake_resolve)


def _parse_frames(frames: List[str]) -> List[Dict[str, str]]:
    """Split raw SSE frames into ``{"event": ..., "id": ..., "data": ...}`` dicts."""
    parsed = []
    for frame in frames:
        entry: Dict[str, str] = {}
        for line in frame.splitlines():
            if not line.strip():
                continue
            field, _, value = line.partition(":")
            entry[field.strip()] = value.strip()
        parsed.append(entry)
    return parsed


def _open_stream(monkeypatch, manager: FakeSSEManager, request: Request):
    """Invoke the handler and return ``(response, client_queue)``."""
    _install_manager(monkeypatch, manager)

    async def _open():
        handler = _make_sse_handler("fake.module:sse_manager")
        response = await handler(request)
        return response

    response = asyncio.run(_open())
    client_id = next(iter(manager.queues))
    return response, manager.queues[client_id]


def _collect(response, count: int) -> List[str]:
    """Pull *count* SSE frames out of a StreamingResponse body iterator."""

    async def _drain() -> None:
        iterator = response.body_iterator
        for _ in range(count):
            frame = await iterator.__anext__()
            frames.append(frame)

    frames: List[str] = []
    asyncio.run(_drain())
    return frames


# ---------------------------------------------------------------------------
# Tests: id: framing
# ---------------------------------------------------------------------------


def test_message_with_id_is_framed_with_id_line(monkeypatch):
    """A message carrying a daemon message id is framed with ``id: <n>``."""
    manager = FakeSSEManager()
    request = _make_request()
    response, queue = _open_stream(monkeypatch, manager, request)

    queue.put_nowait(
        {"type": "task_status", "task_id": "t-1", "state": "working", "id": 7}
    )

    # connected frame + init metadata frame + the queued message.
    frames = _collect(response, 3)

    connected, metadata, framed = frames
    # The first frame is the "connected" frame — unchanged shape, no cursor.
    assert connected.startswith("event: connected\ndata: ")
    assert "id:" not in connected.split("data: ")[0]
    # The init metadata frame carries no message id.
    assert metadata.startswith("data: ")
    assert "id:" not in metadata.split("data: ")[0]

    # The message frame carries the cursor.
    assert framed.startswith("id: 7\ndata: ")
    payload = json.loads(framed.split("data: ", 1)[1])
    assert payload["id"] == 7
    assert payload["type"] == "task_status"
    assert payload["task_id"] == "t-1"
    assert payload["state"] == "working"


def test_message_without_id_still_streams_valid_frame(monkeypatch):
    """A message without an id still streams as a well-formed frame."""
    manager = FakeSSEManager()
    request = _make_request()
    response, queue = _open_stream(monkeypatch, manager, request)

    queue.put_nowait({"type": "mcp_activity", "method": "ping"})

    frames = _collect(response, 3)
    framed = frames[-1]

    assert framed.startswith("data: ")
    assert "id:" not in framed.split("data: ")[0]
    payload = json.loads(framed.split("data: ", 1)[1])
    assert payload == {"type": "mcp_activity", "method": "ping"}


# ---------------------------------------------------------------------------
# Tests: Last-Event-ID replay
# ---------------------------------------------------------------------------


def _replay_frames(monkeypatch, headers, history) -> List[str]:
    """Run the handler with *history* and return every frame it emitted."""
    manager = FakeSSEManager(history=history)
    request = _make_request(headers=headers)
    response, queue = _open_stream(monkeypatch, manager, request)

    # The handler queues the replayed messages plus the init metadata frame.
    queued = queue.qsize()
    return _collect(response, 1 + queued)


def test_last_event_id_replays_only_messages_after_cursor(monkeypatch):
    """``Last-Event-ID: 3`` replays only the messages with id > 3."""
    history = [
        {"type": "task_status", "id": i, "partition_key": "a2a#default"}
        for i in range(1, 6)
    ]
    frames = _replay_frames(monkeypatch, {"Last-Event-ID": "3"}, history)

    parsed = _parse_frames(frames)
    replayed = [entry for entry in parsed if "id" in entry]
    assert [entry["id"] for entry in replayed] == ["4", "5"]
    for entry in replayed:
        assert json.loads(entry["data"])["partition_key"] == "a2a#default"


def test_absent_last_event_id_yields_no_replay(monkeypatch):
    """No ``Last-Event-ID`` header → no replay, no error."""
    history = [
        {"type": "task_status", "id": i, "partition_key": "a2a#default"}
        for i in range(1, 4)
    ]
    frames = _replay_frames(monkeypatch, {}, history)

    parsed = _parse_frames(frames)
    assert [entry["id"] for entry in parsed if "id" in entry] == []
    assert frames[0].startswith("event: connected\ndata: ")


def test_malformed_last_event_id_yields_no_replay(monkeypatch):
    """A garbage ``Last-Event-ID`` degrades to "no replay" instead of raising."""
    history = [
        {"type": "task_status", "id": i, "partition_key": "a2a#default"}
        for i in range(1, 4)
    ]
    frames = _replay_frames(monkeypatch, {"Last-Event-ID": "not-a-number"}, history)

    parsed = _parse_frames(frames)
    assert [entry["id"] for entry in parsed if "id" in entry] == []
    # The stream still opened normally.
    assert frames[0].startswith("event: connected\ndata: ")


# ---------------------------------------------------------------------------
# Tests: partition scoping of the replay
# ---------------------------------------------------------------------------


def test_replay_is_scoped_to_the_callers_partition(monkeypatch):
    """Only history entries for the caller's own partition are replayed.

    The shared daemon's filter accepts entries without a ``partition_key``
    (they match every partition), so the gateway re-applies the scoping:
    entries for another partition, and entries without a partition, are
    dropped.
    """
    history = [
        {"type": "task_status", "id": 1, "partition_key": "a2a#default"},
        {"type": "task_status", "id": 2, "partition_key": "other#tenant"},
        {"type": "a2a_activity", "id": 3},
    ]
    frames = _replay_frames(monkeypatch, {"Last-Event-ID": "0"}, history)

    parsed = _parse_frames(frames)
    replayed = [entry for entry in parsed if "id" in entry]
    assert [entry["id"] for entry in replayed] == ["1"]
    assert json.loads(replayed[0]["data"])["partition_key"] == "a2a#default"


# ---------------------------------------------------------------------------
# Tests: connected / heartbeat shapes and disconnect cleanup
# ---------------------------------------------------------------------------


def test_connected_frame_keeps_its_shape(monkeypatch):
    """The ``connected`` frame is unchanged and carries no cursor."""
    manager = FakeSSEManager()
    request = _make_request()
    response, _queue = _open_stream(monkeypatch, manager, request)

    frame = _collect(response, 1)[0]

    assert frame.startswith("event: connected\ndata: ")
    assert frame.endswith("\n\n")
    assert "id:" not in frame.split("data: ")[0]
    payload = json.loads(frame.split("data: ", 1)[1])
    assert set(payload) == {"client_id", "timestamp"}


def test_heartbeat_frame_keeps_its_shape(monkeypatch):
    """The heartbeat frame is unchanged and carries no cursor."""
    manager = FakeSSEManager()
    real_wait_for = asyncio.wait_for

    async def patched_wait_for(awaitable, timeout, *args, **kwargs):
        if timeout == 15:
            # Emulate the idle timeout without waiting 15 seconds.
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", patched_wait_for)

    request = _make_request()
    response, _queue = _open_stream(monkeypatch, manager, request)

    # connected + init metadata + heartbeat
    frames = _collect(response, 3)
    heartbeat = frames[-1]

    assert heartbeat.startswith("event: heartbeat\ndata: ")
    assert heartbeat.endswith("\n\n")
    assert "id:" not in heartbeat.split("data: ")[0]
    payload = json.loads(heartbeat.split("data: ", 1)[1])
    assert payload["type"] == "heartbeat"
    assert "client_id" in payload
    assert "timestamp" in payload


def test_client_is_removed_on_disconnect(monkeypatch):
    """The client is unregistered from the manager when the stream ends."""
    manager = FakeSSEManager()
    request = _make_request(username="alice")
    response, _queue = _open_stream(monkeypatch, manager, request)

    async def _disconnect():
        iterator = response.body_iterator
        await iterator.__anext__()  # connected
        await iterator.aclose()  # client disconnect

    asyncio.run(_disconnect())

    assert len(manager.removed) == 1
    client_id, username = manager.removed[0]
    assert username == "alice"
    assert client_id == 1


def test_manager_failure_does_not_break_the_stream(monkeypatch):
    """A replay lookup failure degrades to "no replay", the stream still opens."""

    class BrokenManager(FakeSSEManager):
        async def get_missed_messages(self, last_event_id, partition_key=""):
            raise RuntimeError("history unavailable")

    manager = BrokenManager()
    request = _make_request()
    response, _queue = _open_stream(monkeypatch, manager, request)

    frame = _collect(response, 1)[0]

    assert frame.startswith("event: connected\ndata: ")
