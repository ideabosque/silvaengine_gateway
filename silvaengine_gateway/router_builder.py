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
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    handler_type: str = (
        "graphql"  # "graphql" | "rest" | "background" | "task_status" | "sse" | "websocket"
    )
    dispatch: Optional[str] = None  # "knowledge_graph_engine.main:dispatch_graphql"
    methods: List[str] = Field(default_factory=lambda: ["POST"])
    auth: bool = True
    name: Optional[str] = None

    @model_validator(mode="after")
    def _check_dispatch_required(self) -> "RouteSpec":
        # WebSocket routes may omit dispatch (the handler manages its own loop).
        # task_status and sse routes also don't need dispatch.
        if (
            self.handler_type
            in ("graphql", "rest", "background")
            and not self.dispatch
        ):
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

    # Module Config auto-initialization
    config_class: Optional[str] = None
    # "package.module:ClassName" — resolved via importlib at startup
    # e.g. "knowledge_graph_engine.handlers.config:Config"

    config_init_style: str = "dict"
    # "dict"   → Config.initialize(logger, setting)     (KGE pattern)
    # "kwargs" → Config.initialize(logger, **setting)     (ai_rfq_engine pattern)

    config_exclude_keys: List[str] = Field(
        default_factory=lambda: [
            # Gateway-only keys never passed to module configs
            "auth_provider",
            "jwt_secret_key",
            "jwt_algorithm",
            "access_token_exp",
            "admin_username",
            "admin_password",
            "admin_static_token",
            "cognito_user_pool_id",
            "cognito_app_client_id",
            "cognito_app_secret",
            "cognito_jwks_url",
            "jwks_cache_ttl",
            "local_user_file",
            "host",
            "port",
            "workers",
            "routes_config_path",
            "task_backend",
            "task_table",
            "task_ttl",
            "rate_limit_backend",
            "rate_limit_table",
        ]
    )

    # Lifecycle hooks — "package.module:function" resolved via importlib
    on_startup: Optional[str] = None
    # Called after Config.initialize(); receives (logger, setting) dict-style.
    on_shutdown: Optional[str] = None
    # Called during FastAPI lifespan shutdown; async or sync, receives no args.

    # Exception handlers — "package.module:ExceptionClass" resolved via importlib
    # Maps domain exception classes to HTTP status codes.
    # The gateway registers FastAPI exception handlers at startup.
    exception_handlers: List[Dict[str, Any]] = Field(default_factory=list)
    # Example:
    #   - exception_class: "mcp_daemon_engine.utils.exceptions:AuthenticationError"
    #     status_code: 401
    #   - exception_class: "mcp_daemon_engine.utils.exceptions:InvalidRequestError"
    #     status_code: 400

    # SSE manager — "package.module:sse_manager_instance" resolved via importlib
    # Used by handler_type: "sse" routes to connect/disconnect clients.
    # If not specified, SSE routes will use a built-in in-memory manager.
    sse_manager: Optional[str] = None
    # e.g. "mcp_daemon_engine.handlers.sse_manager:sse_manager"


# ---------------------------------------------------------------------------
# Dispatch resolution
# ---------------------------------------------------------------------------


def _resolve_ref(ref_path: str, *, require_callable: bool = True) -> Any:
    """Resolve a dotted path to a module attribute.

    Format: ``"package.module:function"`` or ``"package.module.function"``.

    When *require_callable* is True (default) the resolved object must be
    callable — used for dispatch functions.  When False, any object is
    accepted — used for SSE manager *instances* which are not callable.
    """
    if ":" in ref_path:
        module_path, attr_name = ref_path.rsplit(":", 1)
    else:
        module_path, attr_name = ref_path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"Cannot import module '{module_path}' for '{ref_path}': {e}"
        ) from e

    obj = getattr(module, attr_name, None)
    if obj is None:
        raise AttributeError(
            f"Module '{module_path}' has no attribute '{attr_name}' (from '{ref_path}')"
        )

    if require_callable and not callable(obj):
        raise TypeError(
            f"Ref '{ref_path}' resolved to {type(obj).__name__}, expected callable"
        )

    return obj


def resolve_dispatch(dispatch_path: str) -> Callable:
    """
    Resolve a dotted dispatch path to a callable.
    Format: "package.module:function" or "package.module.function"

    Examples:
        "knowledge_graph_engine.main:dispatch_graphql"
        "knowledge_graph_engine.handlers.extraction.handler:dispatch_extract"
    """
    return _resolve_ref(dispatch_path, require_callable=True)


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
            route_key = (route.path, tuple(sorted(route.methods)))
            if route_key in seen_paths:
                warnings.append(
                    f"Duplicate route path/methods '{route.path}' {route.methods} in module '{module.name}'"
                )
            seen_paths.add(route_key)

            # Try to resolve the dispatch — log a warning if it fails
            # (websocket routes may omit dispatch)
            if route.dispatch and route.handler_type != "websocket":
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
    """Extract partition_key from the endpoint path and the Part-Id header.

    Returns (partition_key, endpoint_id, part_id). ``endpoint_id`` comes from the
    URL path; ``part_id`` is supplied by the ``Part-Id`` header (the ``part_id``
    path parameter remains a legacy fallback for any older route templates).
    """
    endpoint_id = request.path_params.get("endpoint_id", "")
    part_id = (
        request.headers.get("Part-Id")
        or request.headers.get("Part-ID")
        or request.path_params.get("part_id")
    )

    if not part_id:
        raise HTTPException(
            status_code=400,
            detail="Part-Id header is required to construct partition_key",
        )
    partition_key = f"{endpoint_id}#{part_id}"
    return partition_key, endpoint_id, part_id


def _dispatch_label(params: Dict[str, Any]) -> str:
    """Human-readable summary of a dispatch payload for request logging.

    Surfaces the MCP JSON-RPC method (and tool name for ``tools/call``) so MCP
    activity is visible in the gateway log; falls back to a GraphQL hint.
    """
    method = params.get("method")
    if method:
        if method == "tools/call":
            name = (params.get("params") or {}).get("name")
            return f"jsonrpc:tools/call tool={name}" if name else "jsonrpc:tools/call"
        return f"jsonrpc:{method}"
    if params.get("query"):
        return "graphql"
    return ""


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
        # Safely read JSON body — GET/DELETE requests may have no body
        try:
            params = await request.json()
        except Exception:
            params = {}

        if not isinstance(params, dict):
            params = {}

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
        label = _dispatch_label(params)
        logger.info(
            "→ %s %s [%s%s]",
            request.method,
            request.url.path,
            dispatch_fn.__name__,
            f" {label}" if label else "",
        )
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                _executor,
                lambda: dispatch_fn(**params),
            )
        except HTTPException:
            raise
        except Exception:
            logger.error(
                f"Dispatch error [{dispatch_fn.__name__}]: {traceback.format_exc()}"
            )
            raise

        logger.info(
            "← %s %s [%s%s] %.0f ms",
            request.method,
            request.url.path,
            dispatch_fn.__name__,
            f" {label}" if label else "",
            (time.perf_counter() - started) * 1000,
        )

        # Normalize response — KGE returns {"body": json_string} or a dict
        try:
            body = (
                response.get("body", response)
                if isinstance(response, dict)
                else response
            )
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
        # Safely read JSON body — may be empty for some request types
        try:
            params = await request.json()
        except Exception:
            params = {}

        if not isinstance(params, dict):
            params = {}

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
        task_backend.create(
            task_id,
            {
                "partition_key": partition_key,
                "document_external_id": params.get("document_external_id"),
            },
        )

        label = _dispatch_label(params)
        logger.info(
            "⇢ %s %s [%s%s] background task=%s",
            request.method,
            request.url.path,
            dispatch_fn.__name__,
            f" {label}" if label else "",
            task_id,
        )

        loop = asyncio.get_running_loop()
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
            "message": f"Job submitted. Poll GET /{endpoint_id}/extract/status/{task_id} (with Part-Id header) for status.",
        }

    return handler


def _run_background_dispatch(
    task_id: str, dispatch_fn: Callable, params: Dict[str, Any]
) -> None:
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


def _make_sse_handler(sse_manager_ref: Optional[str] = None) -> Callable:
    """Create a GET endpoint that returns an SSE StreamingResponse.

    The handler connects the client to an SSEManager queue and streams
    events (messages + heartbeats) until the client disconnects.
    SSE is unidirectional (server → client). Clients send messages
    via a separate POST endpoint (handler_type: rest, dispatch: ...sse_message).

    Args:
        sse_manager_ref: Dotted path to the SSE manager instance
            (e.g. "mcp_daemon_engine.handlers.sse_manager:sse_manager").
            If None, raises 503 when an SSE route is hit.
    """
    # Resolve the SSE manager at handler-creation time, not per-request
    _sse_manager = None
    if sse_manager_ref:
        try:
            _sse_manager = _resolve_ref(sse_manager_ref, require_callable=False)
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning(
                f"SSE manager '{sse_manager_ref}' could not be resolved: {e}"
            )

    async def sse_handler(request: Request) -> Any:
        from fastapi.responses import StreamingResponse

        if _sse_manager is None:
            raise HTTPException(
                status_code=503,
                detail="SSE manager not available — no sse_manager configured for this module",
            )

        user = getattr(request.state, "user", None) or {}
        username = user.get("username", "")

        # Extract partition_key for partition-aware SSE delivery
        try:
            partition_key, endpoint_id, part_id = _extract_partition_key(request)
        except HTTPException:
            partition_key, endpoint_id, part_id = "", "", ""

        # Register client with partition context
        client_id, queue = await _sse_manager.add_client(
            username,
            partition_key=partition_key,
        )

        # Replay missed messages
        last_event_id = request.headers.get("last-event-id")
        missed = await _sse_manager.get_missed_messages(
            last_event_id,
            partition_key=partition_key,
        )
        for msg in missed:
            try:
                await queue.put(msg)
            except asyncio.QueueFull:
                break

        # Send initialization metadata
        metadata = {
            "type": "mcp_activity",
            "method": "initialize",
            "response": {
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": "MCP SSE Server", "version": "1.0.0"},
                }
            },
        }
        try:
            await queue.put(metadata)
        except asyncio.QueueFull:
            await _sse_manager.remove_client(client_id, username)
            raise HTTPException(status_code=503, detail="Server too busy")

        async def event_generator():
            """Async generator yielding SSE frames."""
            import json as _json
            import pendulum

            try:
                yield (
                    f"event: connected\ndata: "
                    f"{_json.dumps({'client_id': client_id, 'timestamp': pendulum.now('UTC').isoformat()})}\n\n"
                )
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {_json.dumps(message, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        # Heartbeat
                        heartbeat = _json.dumps(
                            {
                                "client_id": client_id,
                                "type": "heartbeat",
                                "timestamp": pendulum.now("UTC").isoformat(),
                            }
                        )
                        yield f"event: heartbeat\ndata: {heartbeat}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                await _sse_manager.remove_client(client_id, username)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return sse_handler


# ---------------------------------------------------------------------------
# WebSocket route handler factory
# ---------------------------------------------------------------------------


def _make_websocket_handler(
    dispatch_fn: Optional[Callable],
    connection_manager: Any = None,
    auth_provider: str = "local",
) -> Callable:
    """Create an async WebSocket handler that authenticates, registers the
    connection, and dispatches incoming messages.

    The handler:
    1. Verifies the JWT token from ``?token=<jwt>`` (before ``accept()``)
    2. Resolves the partition id from ``?part_id=<tenant>``
    3. Accepts the WebSocket and registers it with the ConnectionManager
    4. Sends a ``connection_ack`` message with the assigned ``connection_id``
    5. Loops: receives a JSON message → dispatches in the thread pool → sends response
    6. Unregisters on disconnect

    Args:
        dispatch_fn: The dispatch callable (e.g. ``dispatch_ask_model``).
            May be ``None`` — the handler will send an error message for each
            incoming request.
        connection_manager: The ``ConnectionManager`` instance.
        auth_provider: ``"local"`` or ``"cognito"`` — selects the JWT verifier.
    """
    import uuid

    async def ws_handler(websocket: WebSocket, endpoint_id: str) -> None:
        from .auth.websocket import authenticate_websocket

        # Authenticate before accepting — closes with 4001/4002 on failure
        claims, part_id = await authenticate_websocket(websocket, auth_provider)
        if claims is None:
            return  # already closed by authenticate_websocket

        await websocket.accept()

        connection_id = str(uuid.uuid4())
        if connection_manager is not None:
            connection_manager.register(connection_id, websocket)

        try:
            # Send connection_ack so the client knows its connection_id
            await websocket.send_json({
                "type": "connection_ack",
                "connection_id": connection_id,
            })

            partition_key = f"{endpoint_id}#{part_id}"
            user = claims if claims else {}

            while True:
                message = await websocket.receive_json()

                if dispatch_fn is None:
                    await websocket.send_json({
                        "type": "error",
                        "detail": "No dispatch configured for this route",
                    })
                    continue

                # Build params dict mirroring HTTP context injection.
                # The client sends an action-style message:
                #   {"action": "ask_model", "arguments": {...}}
                # We unwrap this into the flat kwargs the dispatch function expects:
                #   async_task_uuid, arguments, + context (endpoint_id, part_id, etc.)
                if not isinstance(message, dict):
                    message = {}
                params = {}

                # Unwrap action/arguments envelope (ai_agent_core_engine pattern)
                if "arguments" in message and isinstance(message["arguments"], dict):
                    params["arguments"] = message["arguments"]
                else:
                    # Flat message — use as-is
                    params.update(message)

                # Generate async_task_uuid (required by async_execute_ask_model)
                import uuid as _uuid

                params["async_task_uuid"] = str(_uuid.uuid4())

                # Inject gateway context
                if not params.get("context"):
                    params["context"] = {}
                params["context"]["partition_key"] = partition_key
                params["context"]["part_id"] = part_id
                params["context"]["endpoint_id"] = endpoint_id
                params["context"]["connection_id"] = connection_id
                params["context"]["user"] = user
                params["partition_key"] = partition_key
                params["endpoint_id"] = endpoint_id
                params["part_id"] = part_id
                params["connection_id"] = connection_id

                # Execute dispatch in thread pool (sync dispatch functions)
                loop = asyncio.get_running_loop()
                try:
                    result = await loop.run_in_executor(
                        _executor,
                        lambda: dispatch_fn(**params),
                    )
                except Exception as exc:
                    logger.error(
                        "WebSocket dispatch error [%s]: %s",
                        dispatch_fn.__name__ if dispatch_fn else "None",
                        traceback.format_exc(),
                    )
                    await websocket.send_json({
                        "type": "error",
                        "detail": str(exc),
                    })
                    continue

                # Wait for pending ConnectionManager sends scheduled by
                # streaming dispatch code before sending the trailing result.
                if connection_manager is not None and hasattr(
                    connection_manager, "drain_pending_sends"
                ):
                    drained = await connection_manager.drain_pending_sends(
                        connection_id,
                        timeout=5.0,
                    )
                    if not drained:
                        logger.warning(
                            "Timed out draining WebSocket sends for %s",
                            connection_id,
                        )

                # Send the dispatch result back (if any)
                # Streaming chunks were already delivered via ConnectionManager
                if result is not None:
                    try:
                        if isinstance(result, dict):
                            await websocket.send_json(result)
                        elif isinstance(result, str):
                            await websocket.send_text(result)
                    except Exception:
                        pass

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected: %s", connection_id)
        except Exception as exc:
            logger.warning("WebSocket error for %s: %s", connection_id, exc)
        finally:
            if connection_manager is not None:
                connection_manager.unregister(connection_id)

    return ws_handler


# ---------------------------------------------------------------------------
# Router builder
# ---------------------------------------------------------------------------


def init_module_configs(
    modules: List[ModuleSpec],
    setting: Dict[str, Any],
) -> None:
    """
    Auto-initialize module Config classes declared in the route manifest.

    For each module with a ``config_class`` field, this resolves the class
    via importlib, filters the gateway setting dict (stripping
    ``config_exclude_keys``), and calls ``Config.initialize(logger, ...)``.

    The calling convention is controlled by ``config_init_style``:
    - ``"dict"``   → ``Config.initialize(logger, setting_dict)``
    - ``"kwargs"`` → ``Config.initialize(logger, **setting_dict)``

    Modules without ``config_class`` are skipped — their Config must be
    initialized elsewhere or they don't need gateway-managed init.
    """
    for module in modules:
        if not module.config_class:
            continue

        try:
            # Resolve "package.module:ClassName" → the Config class
            config_cls = resolve_dispatch(module.config_class)
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning(
                f"Module '{module.name}': config_class '{module.config_class}' "
                f"could not be resolved — skipping init: {e}"
            )
            continue

        # Filter gateway-only keys
        module_setting = {
            k: v for k, v in setting.items() if k not in module.config_exclude_keys
        }

        if not module_setting:
            logger.debug(
                f"Module '{module.name}': no settings to pass after filtering — skipping init"
            )
            continue

        module_logger = logging.getLogger(module.name)

        try:
            if module.config_init_style == "kwargs":
                config_cls.initialize(module_logger, **module_setting)
            else:
                # Default: dict style
                config_cls.initialize(module_logger, module_setting)

            logger.info(
                f"Module '{module.name}': Config initialized "
                f"(style={module.config_init_style}, keys={len(module_setting)})"
            )
        except Exception as e:
            logger.warning(f"Module '{module.name}': Config.initialize() failed: {e}")


def build_router_from_manifest(
    modules: List[ModuleSpec],
    config: Any = None,
    auth_dependency: Optional[Callable] = None,
    connection_manager: Any = None,
    auth_provider: str = "local",
) -> APIRouter:
    """
    Build a FastAPI APIRouter from the route manifest.

    For each route in each module:
    1. Resolve the dispatch callable via importlib (if dispatch is set)
    2. Wrap it with the appropriate handler factory based on handler_type
    3. Register it as an API route or WebSocket route

    handler_type determines the factory:
    - "graphql" / "rest": _make_sync_handler (runs dispatch in thread pool)
    - "background": _make_background_handler (submits task, returns task_id)
    - "task_status": _make_task_status_handler (polls task state)
    - "sse": _make_sse_handler (GET streaming via SSEManager)
    - "websocket": _make_websocket_handler (WebSocket with auth + ConnectionManager)

    Args:
        modules: List of ModuleSpec from the route manifest
        config: GatewayConfig instance
        auth_dependency: Optional FastAPI dependency for auth enforcement (HTTP only)
        connection_manager: ConnectionManager instance for WebSocket routes
        auth_provider: "local" or "cognito" — selects the WebSocket JWT verifier

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
            elif handler_type == "sse":
                # SSE routes — streaming via SSEManager, no dispatch needed
                handler = _make_sse_handler(sse_manager_ref=module.sse_manager)
            elif handler_type == "websocket":
                # WebSocket routes — resolve dispatch if provided
                ws_dispatch_fn = None
                if route.dispatch:
                    try:
                        ws_dispatch_fn = resolve_dispatch(route.dispatch)
                    except (ImportError, AttributeError, TypeError) as e:
                        logger.error(
                            f"Skipping WebSocket route {route.path} in {module.name}: "
                            f"dispatch '{route.dispatch}' failed to resolve: {e}"
                        )
                        continue

                handler = _make_websocket_handler(
                    dispatch_fn=ws_dispatch_fn,
                    connection_manager=connection_manager,
                    auth_provider=auth_provider,
                )

                # WebSocket routes do not use HTTP auth dependencies
                router.add_api_websocket_route(
                    route.path,
                    handler,
                    name=route.name
                    or f"{module.name}_ws_{route.path.strip('/').replace('/', '_')}",
                )
                logger.info(
                    f"  Registered WebSocket route: {route.path} "
                    f"auth={route.auth} dispatch={route.dispatch or 'none'}"
                )
                continue
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
                    name=route.name
                    or f"{module.name}_{method.lower()}_{route.path.strip('/').replace('/', '_')}",
                )

            logger.info(
                f"  Registered route: {route.path} [{','.join(route.methods)}] "
                f"auth={route.auth} handler_type={handler_type}"
            )

    return router
