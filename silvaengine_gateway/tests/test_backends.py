# -*- coding: utf-8 -*-
"""Unit tests for shared-store backends, task TTL, rate limiting, and Cognito
key lookup. No external services — DynamoDB is exercised through a fake table."""

from __future__ import print_function

import re
import time

import pytest

from silvaengine_gateway.auth.jwt_cognito import _find_signing_key
from silvaengine_gateway.middleware.rate_limit import (
    DynamoDBRateLimitStore,
    InMemoryRateLimitStore,
)
from silvaengine_gateway.tasks.backend import (
    DynamoDBTaskBackend,
    InMemoryTaskBackend,
)


# ---------------------------------------------------------------------------
# Fake DynamoDB table — implements the subset of the resource API the backends
# use, including a small UpdateExpression evaluator for SET / ADD / if_not_exists.
# ---------------------------------------------------------------------------


def _split_top_commas(s: str):
    """Split on commas that are not inside parentheses."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return [p for p in parts if p.strip()]


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items = {}

    def put_item(self, Item):
        self.items[Item[self.key_name]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key[self.key_name])
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, Key):
        self.items.pop(Key[self.key_name], None)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ReturnValues=None,
    ):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        key = Key[self.key_name]
        item = self.items.setdefault(key, {self.key_name: key})
        updated = {}

        expr = UpdateExpression.strip()
        add_match = re.search(r"\bADD\b", expr)
        if expr.upper().startswith("SET"):
            set_part = expr[3 : add_match.start()] if add_match else expr[3:]
            add_part = expr[add_match.end() :] if add_match else ""
        else:
            set_part, add_part = "", expr[add_match.end() :] if add_match else ""

        def resolve(tok):
            tok = tok.strip()
            return names.get(tok, tok)

        for assign in _split_top_commas(set_part):
            lhs, rhs = assign.split("=", 1)
            attr = resolve(lhs)
            rhs = rhs.strip()
            m = re.match(r"if_not_exists\(\s*([^,]+)\s*,\s*(:\w+)\s*\)", rhs)
            if m:
                val = item.get(resolve(m.group(1)), values[m.group(2)])
            else:
                val = values[rhs]
            item[attr] = val
            updated[attr] = val

        if add_part.strip():
            toks = add_part.split()
            attr = resolve(toks[0])
            item[attr] = item.get(attr, 0) + values[toks[1]]
            updated[attr] = item[attr]

        return {"Attributes": updated}


# ---------------------------------------------------------------------------
# In-memory task backend — TTL
# ---------------------------------------------------------------------------


def test_inmemory_task_lifecycle():
    backend = InMemoryTaskBackend(ttl_seconds=3600)
    backend.create("t1", {"partition_key": "ep#part"})
    task = backend.get("t1")
    assert task["status"] == "pending"
    assert task["partition_key"] == "ep#part"

    backend.update("t1", "completed", result={"ok": True})
    assert backend.get("t1")["result"] == {"ok": True}

    backend.delete("t1")
    assert backend.get("t1") is None


def test_inmemory_task_ttl_expiry():
    backend = InMemoryTaskBackend(ttl_seconds=1)
    backend.create("t1", {})
    # Force the stored created_at into the past so it is expired.
    backend._tasks["t1"]["created_at"] = time.time() - 10
    assert backend.get("t1") is None  # lazy expiry on read
    assert "t1" not in backend._tasks


def test_inmemory_task_sweep_drops_stale():
    backend = InMemoryTaskBackend(ttl_seconds=1)
    backend.create("old", {})
    backend._tasks["old"]["created_at"] = time.time() - 10
    backend._last_sweep = 0  # allow an immediate sweep
    backend.create("new", {})  # triggers _maybe_sweep
    assert "old" not in backend._tasks
    assert "new" in backend._tasks


# ---------------------------------------------------------------------------
# DynamoDB task backend (fake table)
# ---------------------------------------------------------------------------


def test_dynamodb_task_backend_roundtrip():
    backend = DynamoDBTaskBackend(FakeTable("task_id"), ttl_seconds=3600)
    backend.create("t1", {"partition_key": "ep#part", "document_external_id": "d1"})

    task = backend.get("t1")
    assert task["status"] == "pending"
    assert task["partition_key"] == "ep#part"
    assert task["document_external_id"] == "d1"
    assert isinstance(task["created_at"], float)

    backend.update("t1", "completed", result={"nested": [1, 2, 3]})
    task = backend.get("t1")
    assert task["status"] == "completed"
    assert task["result"] == {"nested": [1, 2, 3]}  # survived JSON round-trip

    backend.update("t1", "failed", error="boom")
    assert backend.get("t1")["error"] == "boom"

    backend.delete("t1")
    assert backend.get("t1") is None


def test_dynamodb_task_backend_lazy_expiry():
    table = FakeTable("task_id")
    backend = DynamoDBTaskBackend(table, ttl_seconds=3600)
    backend.create("t1", {})
    table.items["t1"]["expires_at"] = int(time.time()) - 5  # already expired
    assert backend.get("t1") is None
    assert "t1" not in table.items  # lazily deleted


# ---------------------------------------------------------------------------
# Rate-limit stores
# ---------------------------------------------------------------------------


def test_inmemory_rate_limit_allows_then_blocks():
    store = InMemoryRateLimitStore()
    assert store.hit("ip", max_requests=3, window_seconds=60)
    assert store.hit("ip", max_requests=3, window_seconds=60)
    assert store.hit("ip", max_requests=3, window_seconds=60)
    assert store.hit("ip", max_requests=3, window_seconds=60) is False  # 4th blocked
    # A different key has its own bucket.
    assert store.hit("other", max_requests=3, window_seconds=60)


def test_dynamodb_rate_limit_fixed_window():
    store = DynamoDBRateLimitStore(FakeTable("rl_key"))
    assert store.hit("ip", max_requests=2, window_seconds=60)
    assert store.hit("ip", max_requests=2, window_seconds=60)
    assert store.hit("ip", max_requests=2, window_seconds=60) is False  # over limit


def test_dynamodb_rate_limit_fails_open_on_error():
    class BrokenTable:
        def update_item(self, **kwargs):
            raise RuntimeError("dynamo down")

    store = DynamoDBRateLimitStore(BrokenTable())
    # Store outage must not block traffic.
    assert store.hit("ip", max_requests=1, window_seconds=60) is True


# ---------------------------------------------------------------------------
# Cognito signing-key lookup
# ---------------------------------------------------------------------------


def test_find_signing_key():
    jwks = {"keys": [{"kid": "a", "alg": "RS256"}, {"kid": "b", "alg": "RS256"}]}
    assert _find_signing_key(jwks, "b")["alg"] == "RS256"
    assert _find_signing_key(jwks, "missing") is None
    assert _find_signing_key({}, "a") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
