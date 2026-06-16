# -*- coding: utf-8 -*-
"""
Durable task state interface.

Provides an abstraction for background task state storage.

Two implementations ship in the box:

- ``InMemoryTaskBackend`` — per-process dict. Fine for single-process
  deployments. Entries expire after a TTL so tasks that are never polled do not
  accumulate.
- ``DynamoDBTaskBackend`` — task state in a DynamoDB table, shared across
  gateway worker processes. Required when running with ``workers > 1`` so a poll
  that lands on a different worker than the one that ran the job can still see
  the result.

Select the backend via the gateway ``task_backend`` setting
(``GATEWAY_TASK_BACKEND`` = ``memory`` | ``dynamodb``).
"""

from __future__ import print_function

__author__ = "silvaengine"

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TASK_TTL_SECONDS = 3600


class TaskBackend:
    """Abstract interface for task state storage."""

    def create(self, task_id: str, meta: Dict[str, Any]) -> None:
        raise NotImplementedError

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def update(self, task_id: str, status: str, **fields: Any) -> None:
        raise NotImplementedError

    def delete(self, task_id: str) -> None:
        raise NotImplementedError


class InMemoryTaskBackend(TaskBackend):
    """In-memory task state — suitable for single-process deployments.

    Entries older than ``ttl_seconds`` are swept lazily (on create/get) so tasks
    that complete but are never polled, or that hang, do not leak memory.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._ttl = ttl_seconds
        self._last_sweep = time.time()

    def create(self, task_id: str, meta: Dict[str, Any]) -> None:
        self._maybe_sweep()
        self._tasks[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "created_at": time.time(),
            **meta,
            "result": None,
            "error": None,
        }

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        self._maybe_sweep()
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if self._is_expired(task, time.time()):
            self._tasks.pop(task_id, None)
            return None
        return task

    def update(self, task_id: str, status: str, **fields: Any) -> None:
        task = self._tasks.get(task_id)
        if task:
            task["status"] = status
            task.update(fields)

    def delete(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def _is_expired(self, task: Dict[str, Any], now: float) -> bool:
        return now - task.get("created_at", now) > self._ttl

    def _maybe_sweep(self) -> None:
        """Drop expired tasks. Throttled so it runs at most ~once a minute."""
        now = time.time()
        if now - self._last_sweep < min(self._ttl, 60):
            return
        self._last_sweep = now
        expired = [
            tid for tid, task in self._tasks.items() if self._is_expired(task, now)
        ]
        for tid in expired:
            self._tasks.pop(tid, None)
        if expired:
            logger.debug("Swept %d expired in-memory tasks", len(expired))


class DynamoDBTaskBackend(TaskBackend):
    """Task state in a DynamoDB table — shared across gateway worker processes.

    The table must have a string hash key named ``task_id``. Enable DynamoDB TTL
    on the ``expires_at`` attribute for automatic server-side cleanup; this class
    also lazily drops expired items it reads, so cleanup is correct even when TTL
    is not configured.

    Complex values (``result``, ``meta``) are stored as JSON strings to avoid
    DynamoDB's float/Decimal and nested-type constraints.
    """

    def __init__(self, table: Any, ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS) -> None:
        self._table = table
        self._ttl = ttl_seconds

    def create(self, task_id: str, meta: Dict[str, Any]) -> None:
        now = int(time.time())
        self._table.put_item(
            Item={
                "task_id": task_id,
                "status": "pending",
                "created_at": now,
                "expires_at": now + self._ttl,
                "meta": json.dumps(meta or {}, default=str),
            }
        )

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        item = self._table.get_item(Key={"task_id": task_id}).get("Item")
        if not item:
            return None

        now = time.time()
        if float(item.get("expires_at", now + 1)) < now:
            self.delete(task_id)
            return None

        task: Dict[str, Any] = {
            "task_id": item.get("task_id"),
            "status": item.get("status"),
            "created_at": float(item.get("created_at", now)),
            "result": json.loads(item["result"]) if item.get("result") else None,
            "error": item.get("error"),
        }
        meta = item.get("meta")
        if meta:
            try:
                task.update(json.loads(meta))
            except (TypeError, ValueError):
                pass
        return task

    def update(self, task_id: str, status: str, **fields: Any) -> None:
        now = int(time.time())
        # ``status`` is a DynamoDB reserved word — alias every name to be safe.
        set_exprs = ["#s = :s", "expires_at = :e"]
        names = {"#s": "status"}
        values: Dict[str, Any] = {":s": status, ":e": now + self._ttl}

        if "result" in fields:
            set_exprs.append("#r = :r")
            names["#r"] = "result"
            values[":r"] = json.dumps(fields["result"], default=str)
        if "error" in fields:
            set_exprs.append("#err = :err")
            names["#err"] = "error"
            values[":err"] = str(fields["error"])

        self._table.update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET " + ", ".join(set_exprs),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def delete(self, task_id: str) -> None:
        self._table.delete_item(Key={"task_id": task_id})


def make_dynamodb_task_backend(
    table_name: str,
    *,
    region_name: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS,
) -> "DynamoDBTaskBackend":
    """Build a DynamoDBTaskBackend from AWS credentials and a table name."""
    import boto3

    resource_kwargs: Dict[str, Any] = {}
    if region_name:
        resource_kwargs["region_name"] = region_name
    if aws_access_key_id and aws_secret_access_key:
        resource_kwargs["aws_access_key_id"] = aws_access_key_id
        resource_kwargs["aws_secret_access_key"] = aws_secret_access_key

    table = boto3.resource("dynamodb", **resource_kwargs).Table(table_name)
    return DynamoDBTaskBackend(table, ttl_seconds=ttl_seconds)


class TaskManager:
    """High-level task management: submit jobs and poll status.

    Wraps a TaskBackend and a dispatch callable for convenient
    background execution and status queries.
    """

    def __init__(self, backend: Optional[TaskBackend] = None) -> None:
        self.backend = backend or InMemoryTaskBackend()

    def submit(
        self,
        dispatch_fn,
        params: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
        executor=None,
        loop=None,
    ) -> str:
        """Submit a background task and return the task_id."""
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        task_id = generate_task_id()
        self.backend.create(task_id, meta or {})

        _executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="taskmgr"
        )
        _loop = loop or asyncio.get_event_loop()

        def _run():
            try:
                self.backend.update(task_id, "running")
                result = dispatch_fn(**params)
                self.backend.update(task_id, "completed", result=result)
            except Exception as e:
                self.backend.update(task_id, "failed", error=str(e))

        _loop.run_in_executor(_executor, _run)
        return task_id

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the current task status dict, or None if not found."""
        return self.backend.get(task_id)


# Module-level singleton used by route handlers
_task_backend: TaskBackend = InMemoryTaskBackend()
_task_manager: Optional[TaskManager] = None


def get_task_backend() -> TaskBackend:
    """Return the current task backend instance."""
    return _task_backend


def set_task_backend(backend: TaskBackend) -> None:
    """Replace the process-wide task backend used by gateway routes."""
    if not isinstance(backend, TaskBackend):
        raise TypeError("backend must implement TaskBackend")

    global _task_backend, _task_manager
    _task_backend = backend
    _task_manager = TaskManager(backend)


def get_task_manager() -> TaskManager:
    """Return the current TaskManager instance."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager(_task_backend)
    return _task_manager


def generate_task_id(prefix: str = "task") -> str:
    """Generate a unique task ID."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
