# -*- coding: utf-8 -*-
"""
Durable task state interface.

Provides an abstraction for background task state storage.
Default implementation uses in-memory dict; DynamoDB implementation
can be swapped via the task_backend_class config option.
"""

from __future__ import print_function

__author__ = "silvaengine"

import time
import uuid
from typing import Any, Dict, Optional


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
    """In-memory task state — suitable for single-process deployments."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create(self, task_id: str, meta: Dict[str, Any]) -> None:
        self._tasks[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "created_at": time.time(),
            **meta,
            "result": None,
            "error": None,
        }

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    def update(self, task_id: str, status: str, **fields: Any) -> None:
        task = self._tasks.get(task_id)
        if task:
            task["status"] = status
            task.update(fields)

    def delete(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)


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

        _executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="taskmgr")
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
_task_backend = InMemoryTaskBackend()
_task_manager: Optional[TaskManager] = None


def get_task_backend() -> TaskBackend:
    """Return the current task backend instance."""
    return _task_backend


def get_task_manager() -> TaskManager:
    """Return the current TaskManager instance."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager(_task_backend)
    return _task_manager


def generate_task_id(prefix: str = "task") -> str:
    """Generate a unique task ID."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"