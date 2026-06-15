# -*- coding: utf-8 -*-
"""
Dynamic router builder from route manifest.

Validates module specifications, resolves dispatch targets via importlib,
and generates FastAPI APIRouter routes at startup.

The builder auto-wraps each dispatch function with gateway cross-cutting
concerns: partition key extraction, user context injection, and background
task submission. No adapter layer is needed — routes.yaml points directly
at core dispatch functions (e.g. knowledge_graph_engine.main:dispatch_graphql).
"""

from __future__ import print_function

__author__ = "silvaengine"

import asyncio
import importlib
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .tasks.backend import generate_task_id, get_task_backend

logger = logging.getLogger(__name__)

# Shared thread pool for sync dispatch execution
_executor = ThreadPoolExecutor(
    max_workers=int(__import__("os").environ.get("GATEWAY_DISPATCH_WORKERS", "8")),
    thread_name_prefix="gw-dispatch",
)

# ---------------------------------------------------------------------------
# Pydantic models for manifest validation
# ---------------------------------------------------------------------------


class RouteSpec(BaseModel):
    """A single route within a module."""

    path: str
    handler_type: str = "graphql"  # "graphql" | "rest" | "background" | "task_status"
    dispatch: Optional[str] = None  # "knowledge_graph_engine.main:dispatch_graphql"
    methods: List[str] = Field(default_factory=lambda: ["POST"])
    auth: bool = True
    name: Optional[str] = None

    @model_validator(mode="after")
    def _check_dispatch_required(self) -> "RouteSpec":
        if self.handler_type in ("graphql", "rest", "background") and not self.dispatch:
            raise ValueError(
                f"Route '{self.path}' with handler_type='{self.handler_type}' "
                f"requires a 'dispatch' field"
            )
        return self


class ModuleSpec(BaseModel):
    """A module registered in the route manifest."""

    name: str
    package: str  # e.g. "knowledge_graph_engine"
    transport: str = "graphql"  # "graphql" | "rest" | "hybrid"
    routes: List[RouteSpec] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dispatch resolution
# ---------------------------------------------------------------------------


def resolve_dispatch(dispatch_path: str) -> Callable:
    """
    Resolve a dotted dispatch path to a callable.
    Format: "package.module:function" or "package.module.function"

    Examples:
        "knowledge_graph_engine.main:dispatch_graphql"
        "knowledge_graph_engine.handlers.extraction.handler:dispatch_extract"
    """
    if ":" in dispatch_path:
        module_path, attr_name = dispatch_path.rsplit(":", 1)
    else:
        module_path, attr_name = dispatch_path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"Cannot import dispatch module '{module_path}' for '{dispatch_path}': {e}"
        ) from e

    handler = getattr(module, attr_name, None)
    if handler is None:
        raise AttributeError(
            f"Module '{module_path}' has no attribute '{attr_name}' (from dispatch '{dispatch_path}')"
        )

    if not callable(handler):
        raise TypeError(
            f"Dispatch '{dispatch_path}' resolved to {type(handler).__name__}, expected callable"
        )

    return handler


def validate_manifest(modules: List[ModuleSpec]) -> List[str]:
    """
    Validate the route manifest for common issues.
    Returns a list of warning strings (empty if valid).

    Checks:
    - Duplicate route paths
    - Invalid transport values
    - Unresolvable dispatch paths (logs warnings, doesn't fail)
    """
    warnings: list = []
    seen_paths: set = set()

    for module in modules:
        if module.transport not in ("graphql", "rest", "hybrid"):
            warnings.append(
                f"Module '{module.name}' has invalid transport '{module.transport}' "
                f"(expected: graphql, rest, hybrid)"
            )

        for route in module.routes:
            if route.path in seen_paths:
                warnings.append(
                    f"Duplicate route path '{route.path}' in module '{module.name}'"
                )
            seen_paths.add(route.path)

            # Try to resolve the dispatch — log a warning if it fails
            if route.dispatch:
                try:
                    resolve_dispatch(route.dispatch)
                except (ImportError, AttributeError, TypeError) as e:
                    warnings.append(
                        f"Module '{module.name}' route '{route.path}': "
                        f"dispatch '{route.dispatch}' cannot be resolved: {e}"
                    )

    return warnings


# ---------------------------------------------------------------------------
# Partition key extraction from request headers
# ---------------------------------------------------------------------------


def _extract_partition_key(request: Request) -> tuple:
    """Extract partition_key from the endpoint path and Part-Id header.

    The path-level ``part_id`` remains part of the public URL, but the header is
    required so callers explicitly identify the partition used by core
    dispatch functions.

    Returns (partition_key, endpoint_id, part_id).
    Raises HTTPException(400) if Part-Id header is missing.
    """
    endpoint_id = request.path_params.get("endpoint_id", "")
    part_id = request.headers.get("Part-Id") or request.headers.get("Part-ID")

    if not part_id:
        raise HTTPException(
            status_code=400,
            detail="Part-Id header is required to construct partition_key",
        )
    partition_key = f"{endpoint_id}#{part_id}"
    return partition_key, endpoint_id, part_id


# ---------------------------------------------------------------------------
# Route handler factories
# ---------------------------------------------------------------------------


def _make_sync_handler(dispatch_fn: Callable) -> Callable:
    """Create an async route handler that runs dispatch_fn in a thread pool.

    The handler:
    1. Reads JSON body from request
    2. Extracts partition_key from path params + Part-Id header
    3. Injects context (partition_key, part_id, user) into params
    4. Runs dispatch_fn(**params) in thread pool
    5. Returns the result
    """

    async def handler(request: Request) -> Dict[str, Any]:
        params = await request.json()
        partition_key, endpoint_id, part_id = _extract_partition_key(request)

        # Build params dict
        if not params.get("context"):
            params["context"] = {}
        params["context"]["partition_key"] = partition_key
        params["context"]["part_id"] = part_id
        params["partition_key"] = partition_key
        params["endpoint_id"] = endpoint_id
        params["part_id"] = part_id

        # Inject authenticated user
        user = getattr(request.state, "user", None)
        if user:
            params["context"]["user"] = user

        # Execute dispatch in thread pool (sync dispatch functions)
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                _executor,
                lambda: dispatch_fn(**params),
            )
        except Exception as e:
            logger.error(f"Dispatch error [{dispatch_fn.__name__}]: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=str(e))

        # Normalize response — KGE returns {"body": json_string} or a dict
        try:
            body = response.get("body", response) if isinstance(response, dict) else response
            result = json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, TypeError):
            result = response

        return result

    return handler


def _make_background_handler(dispatch_fn: Callable) -> Callable:
    """Create an async route handler that submits dispatch_fn as a background task.

    Returns immediately with a task_id. Client polls for status.
    """

    async def handler(request: Request) -> Dict[str, Any]:
        params = await request.json()
        partition_key, endpoint_id, part_id = _extract_partition_key(request)

        if not params.get("context"):
            params["context"] = {}
        params["context"]["partition_key"] = partition_key
        params["context"]["part_id"] = part_id
        params["partition_key"] = partition_key
        params["endpoint_id"] = endpoint_id
        params["part_id"] = part_id

        user = getattr(request.state, "user", None)
        if user:
            params["context"]["user"] = user

        task_id = generate_task_id()
        task_backend = get_task_backend()
        task_backend.create(task_id, {
            "partition_key": partition_key,
            "document_external_id": params.get("document_external_id"),
        })

        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            _executor,
            _run_background_dispatch,
            task_id,
            dispatch_fn,
            params,
        )

        return {
            "task_id": task_id,
            "status": "pending",
            "message": f"Job submitted. Poll GET /{endpoint_id}/{part_id}/extract/status/{task_id} for status.",
        }

    return handler


def _run_background_dispatch(task_id: str, dispatch_fn: Callable, params: Dict[str, Any]) -> None:
    """Execute a dispatch function in background and update task state."""
    task_backend = get_task_backend()
    try:
        task_backend.update(task_id, "running")
        result = dispatch_fn(**params)
        task_backend.update(task_id, "completed", result=result)
        logger.info(f"Background task {task_id} completed via {dispatch_fn.__name__}")
    except Exception as e:
        task_backend.update(task_id, "failed", error=str(e))
        logger.error(f"Background task {task_id} failed: {traceback.format_exc()}")


def _make_task_status_handler() -> Callable:
    """Create a handler that returns the status of a background task."""

    async def handler(task_id: str) -> Dict[str, Any]:
        task_backend = get_task_backend()
        task = task_backend.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        response = {
            "task_id": task_id,
            "status": task["status"],
            "partition_key": task.get("partition_key"),
            "document_external_id": task.get("document_external_id"),
        }

        if task["status"] == "completed":
            response["result"] = task.get("result")
            task_backend.delete(task_id)
        elif task["status"] == "failed":
            response["error"] = task.get("error")
            task_backend.delete(task_id)
        elif task["status"] == "pending":
            import time
            elapsed = time.time() - task.get("created_at", time.time())
            response["elapsed_seconds"] = round(elapsed, 1)

        return response

    return handler


# ---------------------------------------------------------------------------
# Router builder
# ---------------------------------------------------------------------------


def build_router_from_manifest(
    modules: List[ModuleSpec],
    config: Any = None,
    auth_dependency: Optional[Callable] = None,
) -> APIRouter:
    """
    Build a FastAPI APIRouter from the route manifest.

    For each route in each module:
    1. Resolve the dispatch callable via importlib
    2. Wrap it with the appropriate handler factory based on handler_type
    3. Register it as an API route with auth dependency

    handler_type determines the factory:
    - "graphql" / "rest": _make_sync_handler (runs dispatch in thread pool)
    - "background": _make_background_handler (submits task, returns task_id)
    - "task_status": _make_task_status_handler (polls task state)

    Args:
        modules: List of ModuleSpec from the route manifest
        config: GatewayConfig instance
        auth_dependency: Optional FastAPI dependency for auth enforcement

    Returns:
        APIRouter with all routes registered
    """
    router = APIRouter()

    for module in modules:
        logger.info(f"Registering module: {module.name} (transport={module.transport})")

        for route in module.routes:
            handler_type = route.handler_type

            # Task status routes — always use the built-in status handler
            if handler_type == "task_status":
                handler = _make_task_status_handler()
            else:
                try:
                    dispatch_fn = resolve_dispatch(route.dispatch)
                except (ImportError, AttributeError, TypeError) as e:
                    logger.error(
                        f"Skipping route {route.path} in {module.name}: "
                        f"dispatch '{route.dispatch}' failed to resolve: {e}"
                    )
                    continue

                # Wrap dispatch with appropriate handler factory
                if handler_type == "background":
                    handler = _make_background_handler(dispatch_fn)
                else:
                    # graphql + rest both use sync handler
                    handler = _make_sync_handler(dispatch_fn)

            dependencies = []
            if route.auth and auth_dependency:
                dependencies.append(Depends(auth_dependency))

            for method in route.methods:
                router.add_api_route(
                    route.path,
                    handler,
                    methods=[method],
                    dependencies=dependencies,
                    name=route.name or f"{module.name}_{method.lower()}_{route.path.strip('/').replace('/', '_')}",
                )

            logger.info(
                f"  Registered route: {route.path} [{','.join(route.methods)}] "
                f"auth={route.auth} handler_type={handler_type}"
            )

    return router
