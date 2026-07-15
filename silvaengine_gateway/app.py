# -*- coding: utf-8 -*-
"""
SilvaEngine Gateway - FastAPI app factory.

Creates the FastAPI app, loads route manifest, initializes auth + rate limit
middleware, mounts health/auth routes, and dynamically registers module
dispatch routes from the manifest.
"""

from __future__ import print_function

__author__ = "silvaengine"

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI

from .config import GatewayConfig
from .manifest import load_route_manifest
from .middleware.rate_limit import RateLimitMiddleware
from .router_builder import (
    ModuleSpec,
    build_router_from_manifest,
    validate_manifest,
    resolve_dispatch,
)
from .setting_builder import build_setting_from_env
from .websocket_manager import ConnectionManager

logger = logging.getLogger(__name__)

# Re-exported for backward compatibility: callers (run_daemon, gen_token, tests)
# import these from silvaengine_gateway.app. They now live in dedicated modules.
__all__ = [
    "create_app",
    "create_app_from_env",
    "run_gateway",
    "build_setting_from_env",
    "load_route_manifest",
]


# ---------------------------------------------------------------------------
# Fix editable-install namespace shadowing
# ---------------------------------------------------------------------------
# When running from a project monorepo (cwd = .../silvaengine/), Python's
# PathFinder discovers silvaengine_* directories (project roots without
# __init__.py) and creates namespace packages - overriding the correct
# SourceFileLoader specs from pip's editable finders.  Moving all
# _EditableFinder instances above PathFinder ensures they resolve first.
# ---------------------------------------------------------------------------


def _promote_editable_finders() -> None:
    """Move all _EditableFinder entries above PathFinder in sys.meta_path.

    When running from a monorepo (cwd = .../silvaengine/), PathFinder
    discovers silvaengine_* project-root directories and creates namespace
    packages - shadowing the correct SourceFileLoader specs from pip's
    editable finders.  This fix ensures editable installs resolve first.
    """
    import sys as _sys
    from importlib.machinery import PathFinder as _PathFinder

    meta_path = _sys.meta_path
    # Editable finders are class objects (not instances), so we check
    # f.__name__ rather than type(f).__name__.
    editable = [
        f
        for f in meta_path
        if hasattr(f, "__name__") and f.__name__ == "_EditableFinder"
    ]
    if not editable:
        return

    pf_index = None
    for i, finder in enumerate(meta_path):
        if finder is _PathFinder:
            pf_index = i
            break

    if pf_index is None:
        return

    # Check if any editable finder is already above PathFinder
    editable_indices = [meta_path.index(f) for f in editable]
    if all(idx < pf_index for idx in editable_indices):
        return  # Already in correct order

    # Remove editable finders and re-insert above PathFinder
    for f in editable:
        meta_path.remove(f)
    # PathFinder may have shifted; find its new index
    for i, finder in enumerate(meta_path):
        if finder is _PathFinder:
            pf_index = i
            break
    for f in reversed(editable):
        meta_path.insert(pf_index, f)

    logger.debug(
        f"Promoted {len(editable)} editable finder(s) above PathFinder "
        f"in sys.meta_path"
    )


_promote_editable_finders()


# ---------------------------------------------------------------------------
# Shared-store backend selection (multi-process support)
# ---------------------------------------------------------------------------


def _aws_creds(setting: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "region_name": setting.get("region_name") or os.getenv("region_name"),
        "aws_access_key_id": setting.get("aws_access_key_id")
        or os.getenv("aws_access_key_id"),
        "aws_secret_access_key": setting.get("aws_secret_access_key")
        or os.getenv("aws_secret_access_key"),
    }


def _configure_task_backend(setting: Dict[str, Any], gw_logger: logging.Logger) -> str:
    """Select and install the process-wide task backend. Returns its kind."""
    from .tasks.backend import (
        DEFAULT_TASK_TTL_SECONDS,
        InMemoryTaskBackend,
        make_dynamodb_task_backend,
        set_task_backend,
    )

    kind = str(
        setting.get("task_backend") or os.getenv("GATEWAY_TASK_BACKEND", "memory")
    ).lower()
    ttl = int(
        setting.get("task_ttl")
        or os.getenv("GATEWAY_TASK_TTL", str(DEFAULT_TASK_TTL_SECONDS))
    )

    if kind == "dynamodb":
        table = setting.get("task_table") or os.getenv(
            "GATEWAY_TASK_TABLE", "silvaengine-gateway-tasks"
        )
        set_task_backend(
            make_dynamodb_task_backend(table, ttl_seconds=ttl, **_aws_creds(setting))
        )
        gw_logger.info(f"Task backend: DynamoDB table '{table}' (ttl={ttl}s)")
    else:
        set_task_backend(InMemoryTaskBackend(ttl_seconds=ttl))
        gw_logger.info(f"Task backend: in-memory (ttl={ttl}s)")
    return kind


def _make_rate_limit_store(setting: Dict[str, Any], gw_logger: logging.Logger):
    """Build the rate-limit store. Returns ``(store, kind)``."""
    from .middleware.rate_limit import (
        InMemoryRateLimitStore,
        make_dynamodb_rate_limit_store,
    )

    kind = str(
        setting.get("rate_limit_backend")
        or os.getenv("GATEWAY_RATE_LIMIT_BACKEND", "memory")
    ).lower()

    if kind == "dynamodb":
        table = setting.get("rate_limit_table") or os.getenv(
            "GATEWAY_RATE_LIMIT_TABLE", "silvaengine-gateway-ratelimit"
        )
        gw_logger.info(f"Rate-limit backend: DynamoDB table '{table}'")
        return make_dynamodb_rate_limit_store(table, **_aws_creds(setting)), kind

    gw_logger.info("Rate-limit backend: in-memory")
    return InMemoryRateLimitStore(), kind


def _warn_multiprocess_compat(
    setting: Dict[str, Any],
    manifest: List[ModuleSpec],
    task_kind: str,
    rl_kind: str,
    gw_logger: logging.Logger,
) -> None:
    """Warn loudly when in-memory state is used with more than one worker."""
    try:
        workers = int(setting.get("workers") or os.getenv("GATEWAY_WORKERS", "1") or 1)
    except (TypeError, ValueError):
        workers = 1
    if workers <= 1:
        return

    if task_kind != "dynamodb":
        gw_logger.warning(
            "workers=%d but task_backend is in-memory - background task status is "
            "per-process; a poll may hit a different worker than the one that ran "
            "the job. Set GATEWAY_TASK_BACKEND=dynamodb.",
            workers,
        )
    if rl_kind != "dynamodb":
        gw_logger.warning(
            "workers=%d but rate_limit_backend is in-memory - the effective limit "
            "is max_requests*workers. Set GATEWAY_RATE_LIMIT_BACKEND=dynamodb.",
            workers,
        )
    if any(r.handler_type == "sse" for m in manifest for r in m.routes):
        gw_logger.warning(
            "workers=%d with SSE routes - the SSE registry is per-process. Use "
            "sticky sessions so each client's GET stream and POST land on the same "
            "worker; cross-user broadcast across workers needs a pub/sub backplane.",
            workers,
        )
    if any(r.handler_type == "websocket" for m in manifest for r in m.routes):
        gw_logger.warning(
            "workers=%d with WebSocket routes - the ConnectionManager registry is "
            "per-process. A client may connect to worker A, but a sync dispatch "
            "thread on worker B cannot deliver to A's socket. Use a single worker "
            "for WebSocket MVP, or implement a Redis-backed broker for multi-worker.",
            workers,
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(setting: Dict[str, Any] = None) -> FastAPI:
    """
    Create and configure the FastAPI gateway application.

    Args:
        setting: Optional dict of settings to initialize GatewayConfig.
                 If not provided, reads from environment variables.

    Returns:
        Configured FastAPI app instance.
    """
    setting = setting or {}

    # Initialize gateway config
    gw_logger = logging.getLogger("silvaengine_gateway")
    GatewayConfig.initialize(gw_logger, setting)

    # Load route manifest
    manifest = load_route_manifest(GatewayConfig)

    # Auto-initialize module Config classes declared in manifest
    from .router_builder import init_module_configs

    init_module_configs(manifest, setting)

    # Create the WebSocket ConnectionManager (single-process MVP)
    connection_manager = ConnectionManager()

    # Inject the ConnectionManager into module Config classes that support it
    # (e.g. ai_agent_core_engine.handlers.config:Config.set_connection_manager)
    for mod in manifest:
        if not mod.config_class:
            continue
        try:
            config_cls = resolve_dispatch(mod.config_class)
            if hasattr(config_cls, "set_connection_manager"):
                config_cls.set_connection_manager(connection_manager)
                gw_logger.info(f"Injected ConnectionManager into {mod.name} Config")
        except (ImportError, AttributeError, TypeError):
            pass  # Module does not support connection manager - skip

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw_logger.info("Starting SilvaEngine Gateway...")

        # Bind the running event loop to the ConnectionManager
        import asyncio as _asyncio

        connection_manager.set_event_loop(_asyncio.get_running_loop())

        yield
        gw_logger.info("Shutting down SilvaEngine Gateway...")

        # Close active WebSocket connections
        await connection_manager.shutdown()

        # Cleanup Cognito HTTP client if needed
        if GatewayConfig.auth_provider == "cognito":
            try:
                from .auth.jwt_cognito import cleanup_http_client

                await cleanup_http_client()
            except Exception:
                pass

        # Call module on_shutdown hooks (e.g. SSEManager cleanup)
        for mod in manifest:
            if mod.on_shutdown:
                try:
                    shutdown_fn = resolve_dispatch(mod.on_shutdown)
                    import asyncio

                    if asyncio.iscoroutinefunction(shutdown_fn):
                        await shutdown_fn()
                    else:
                        shutdown_fn()
                    gw_logger.info(f"Module '{mod.name}' shutdown hook completed")
                except Exception as e:
                    gw_logger.warning(f"Module '{mod.name}' shutdown hook failed: {e}")

    app = FastAPI(
        title="SilvaEngine Gateway",
        description="FastAPI gateway with auth, module routing, and dispatch",
        lifespan=lifespan,
    )

    # NOTE: CORS is added LAST (below), after rate-limit and auth, so it is the
    # OUTERMOST middleware. Starlette runs the last-added middleware first, so
    # this lets CORS answer preflight OPTIONS (which carry no Authorization
    # header) and stamp Access-Control-* headers onto every response — including
    # auth 401s — instead of the auth middleware rejecting the preflight first.

    # Task + rate-limit backends (shared stores when configured for multi-process)
    task_kind = _configure_task_backend(setting, gw_logger)
    rate_limit_store, rl_kind = _make_rate_limit_store(setting, gw_logger)
    _warn_multiprocess_compat(setting, manifest, task_kind, rl_kind, gw_logger)

    # Rate limiting (global)
    rate_limit = int(os.environ.get("GATEWAY_RATE_LIMIT", "100"))
    rate_window = int(os.environ.get("GATEWAY_RATE_WINDOW", "60"))
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=rate_limit,
        window_seconds=rate_window,
        store=rate_limit_store,
    )

    # Auth middleware
    from .auth.middleware import FlexJWTMiddleware

    app.add_middleware(FlexJWTMiddleware, public_paths=["/health", "/auth"])

    # CORS — added LAST so it is the OUTERMOST middleware (see note above).
    from fastapi.middleware.cors import CORSMiddleware

    # A wildcard origin and credentialed requests are mutually exclusive per the
    # CORS spec - browsers reject "Access-Control-Allow-Origin: *" alongside
    # credentials, and Starlette will not echo the wildcard in that case. Read an
    # explicit allowlist from GATEWAY_CORS_ORIGINS (comma-separated) to enable
    # credentials; otherwise fall back to a wildcard with credentials disabled.
    cors_env = os.environ.get("GATEWAY_CORS_ORIGINS", "").strip()
    if cors_env and cors_env != "*":
        allow_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
        allow_credentials = True
    else:
        allow_origins = ["*"]
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth routes
    from .routes.auth import router as auth_router

    app.include_router(auth_router)

    # Health routes
    from .routes.health import router as health_router

    app.include_router(health_router)

    # Register domain exception handlers from manifest modules
    from fastapi.responses import JSONResponse

    for mod in manifest:
        for exc_spec in mod.exception_handlers:
            try:
                exc_cls = resolve_dispatch(exc_spec["exception_class"])
                status_code = exc_spec.get("status_code", 500)

                # Closure captures status_code; FastAPI calls handler(request, exc)
                async def _domain_exc_handler(request, exc, _status_code=status_code):
                    msg = getattr(exc, "message", str(exc))
                    return JSONResponse(
                        status_code=_status_code, content={"detail": msg}
                    )

                app.add_exception_handler(exc_cls, _domain_exc_handler)
                gw_logger.info(
                    f"Registered exception handler: {exc_cls.__name__} -> {status_code}"
                )
            except (ImportError, AttributeError, TypeError) as e:
                gw_logger.warning(
                    f"Module '{mod.name}': exception_class "
                    f"'{exc_spec.get('exception_class')}' could not be resolved - "
                    f"skipping: {e}"
                )

    # Validate and build dynamic routes from manifest (already loaded above)
    warnings = validate_manifest(manifest)
    for w in warnings:
        gw_logger.warning(f"Route manifest warning: {w}")

    # Build dynamic routes from manifest
    from .auth.middleware import get_current_user

    router = build_router_from_manifest(
        manifest,
        config=GatewayConfig,
        auth_dependency=get_current_user,
        connection_manager=connection_manager,
        auth_provider=GatewayConfig.auth_provider,
    )
    app.include_router(router)

    return app


def create_app_from_env() -> FastAPI:
    """App factory for uvicorn import-string / multi-worker launches.

    Uvicorn spawns each worker as a fresh process and re-imports the app, so the
    app must be built from environment, not passed as an object.
    """
    from dotenv import load_dotenv

    load_dotenv()
    return create_app(build_setting_from_env())


def run_gateway() -> None:
    """Run the gateway as a daemon process (entry point for __main__)."""
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        stream=__import__("sys").stdout,
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    host = os.getenv("GATEWAY_HOST", "0.0.0.0")
    port = int(os.getenv("GATEWAY_PORT", "8000"))
    try:
        workers = int(os.getenv("GATEWAY_WORKERS", "1"))
    except (TypeError, ValueError):
        workers = 1

    if workers > 1:
        # Multi-worker requires an import string + factory so uvicorn can build
        # the app inside each spawned worker process.
        uvicorn.run(
            "silvaengine_gateway.app:create_app_from_env",
            factory=True,
            host=host,
            port=port,
            workers=workers,
        )
    else:
        app = create_app(build_setting_from_env())
        uvicorn.run(app, host=host, port=port)
