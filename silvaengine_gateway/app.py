# -*- coding: utf-8 -*-
"""
SilvaEngine Gateway — FastAPI app factory.

Creates the FastAPI app, loads route manifest, initializes auth + rate limit
middleware, mounts health/auth routes, and dynamically registers module
dispatch routes from the manifest.
"""

from __future__ import print_function

__author__ = "silvaengine"

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI

from .config import GatewayConfig
from .middleware.rate_limit import RateLimitMiddleware
from .router_builder import (
    ModuleSpec,
    RouteSpec,
    build_router_from_manifest,
    validate_manifest,
    resolve_dispatch,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fix editable-install namespace shadowing
# ---------------------------------------------------------------------------
# When running from a project monorepo (cwd = .../silvaengine/), Python's
# PathFinder discovers silvaengine_* directories (project roots without
# __init__.py) and creates namespace packages — overriding the correct
# SourceFileLoader specs from pip's editable finders.  Moving all
# _EditableFinder instances above PathFinder ensures they resolve first.
# ---------------------------------------------------------------------------


def _promote_editable_finders() -> None:
    """Move all _EditableFinder entries above PathFinder in sys.meta_path.

    When running from a monorepo (cwd = .../silvaengine/), PathFinder
    discovers silvaengine_* project-root directories and creates namespace
    packages — shadowing the correct SourceFileLoader specs from pip's
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
# Route manifest loading
# ---------------------------------------------------------------------------


def load_route_manifest(config: GatewayConfig) -> List[ModuleSpec]:
    """
    Load route manifest from:
    1. GATEWAY_ROUTES_CONFIG_JSON env var (JSON string)
    2. GATEWAY_ROUTES_CONFIG_PATH env var (YAML or JSON file)
    3. routes.yaml packaged with the gateway
    4. Built-in default (KGE only)
    """
    # Priority 1: env var JSON
    env_routes_json = (
        os.environ.get("GATEWAY_ROUTES_CONFIG_JSON") or config.routes_config_json
    )
    if env_routes_json:
        try:
            modules = json.loads(env_routes_json)
            return [ModuleSpec(**m) for m in modules]
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to parse GATEWAY_ROUTES_CONFIG_JSON: {e}")
            raise

    # Priority 2: explicit path
    configured_path = config.routes_config_path or os.environ.get(
        "GATEWAY_ROUTES_CONFIG_PATH"
    )
    routes_file = (
        Path(configured_path)
        if configured_path
        else Path(__file__).parent / "routes.yaml"
    )

    if routes_file.exists():
        try:
            with open(routes_file) as f:
                data = yaml.safe_load(f)
            modules = data.get("modules", [])
            return [ModuleSpec(**m) for m in modules]
        except Exception as e:
            logger.error(f"Failed to load routes from {routes_file}: {e}")
            raise

    # Priority 3: Built-in default (KGE only)
    logger.info("No route manifest found — using built-in default (KGE only)")
    return _default_manifest()


def _default_manifest() -> List[ModuleSpec]:
    """Built-in default route manifest — KGE only."""
    return [
        ModuleSpec(
            name="knowledge_graph_engine",
            package="knowledge_graph_engine",
            transport="graphql",
            routes=[
                RouteSpec(
                    path="/{endpoint_id}/knowledge_graph_graphql",
                    handler_type="graphql",
                    dispatch="knowledge_graph_engine.main:dispatch_graphql",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/extract",
                    handler_type="background",
                    dispatch="knowledge_graph_engine.main:dispatch_extract",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/extract/status/{task_id}",
                    handler_type="task_status",
                    methods=["GET"],
                    auth=True,
                ),
            ],
        )
    ]


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
            "workers=%d but task_backend is in-memory — background task status is "
            "per-process; a poll may hit a different worker than the one that ran "
            "the job. Set GATEWAY_TASK_BACKEND=dynamodb.",
            workers,
        )
    if rl_kind != "dynamodb":
        gw_logger.warning(
            "workers=%d but rate_limit_backend is in-memory — the effective limit "
            "is max_requests*workers. Set GATEWAY_RATE_LIMIT_BACKEND=dynamodb.",
            workers,
        )
    if any(r.handler_type == "sse" for m in manifest for r in m.routes):
        gw_logger.warning(
            "workers=%d with SSE routes — the SSE registry is per-process. Use "
            "sticky sessions so each client's GET stream and POST land on the same "
            "worker; cross-user broadcast across workers needs a pub/sub backplane.",
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw_logger.info("Starting SilvaEngine Gateway...")
        yield
        gw_logger.info("Shutting down SilvaEngine Gateway...")

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

    # CORS
    from fastapi.middleware.cors import CORSMiddleware

    # A wildcard origin and credentialed requests are mutually exclusive per the
    # CORS spec — browsers reject "Access-Control-Allow-Origin: *" alongside
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
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

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
                    f"Registered exception handler: {exc_cls.__name__} → {status_code}"
                )
            except (ImportError, AttributeError, TypeError) as e:
                gw_logger.warning(
                    f"Module '{mod.name}': exception_class "
                    f"'{exc_spec.get('exception_class')}' could not be resolved — "
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
    )
    app.include_router(router)

    return app


def build_setting_from_env() -> Dict[str, Any]:
    """Build the gateway setting dict from environment variables.

    Shared by the single-process and multi-worker (factory) launch paths so both
    see an identical configuration.
    """
    setting = {
        # AWS (shared with core)
        "region_name": os.getenv("region_name"),
        "aws_access_key_id": os.getenv("aws_access_key_id"),
        "aws_secret_access_key": os.getenv("aws_secret_access_key"),
        # Tenant
        "endpoint_id": os.getenv("endpoint_id"),
        "part_id": os.getenv("part_id"),
        # Auth (gateway-specific)
        "auth_provider": os.getenv(
            "GATEWAY_AUTH_PROVIDER", os.getenv("AUTH_PROVIDER", "local")
        ),
        "jwt_secret_key": os.getenv("JWT_SECRET_KEY", "CHANGEME"),
        "jwt_algorithm": os.getenv("JWT_ALGORITHM", "HS256"),
        "access_token_exp": os.getenv("ACCESS_TOKEN_EXP", "15"),
        "admin_username": os.getenv("ADMIN_USERNAME", ""),
        "admin_password": os.getenv("ADMIN_PASSWORD", ""),
        "admin_static_token": os.getenv("ADMIN_STATIC_TOKEN", ""),
        "local_user_file": os.getenv("LOCAL_USER_FILE"),
        # Cognito
        "cognito_user_pool_id": os.getenv("COGNITO_USER_POOL_ID", ""),
        "cognito_app_client_id": os.getenv("COGNITO_APP_CLIENT_ID", ""),
        "cognito_app_secret": os.getenv("COGNITO_APP_SECRET", ""),
        "cognito_jwks_url": os.getenv("COGNITO_JWKS_URL"),
        # Server
        "host": os.getenv("GATEWAY_HOST", "0.0.0.0"),
        "port": os.getenv("GATEWAY_PORT", "8000"),
        "workers": os.getenv("GATEWAY_WORKERS", "1"),
        # Route manifest
        "routes_config_path": os.getenv("GATEWAY_ROUTES_CONFIG_PATH"),
        "routes_config_json": os.getenv("GATEWAY_ROUTES_CONFIG_JSON"),
        # Tables
        "initialize_tables": int(os.getenv("initialize_tables", "0")),
        # LLM (shared with core)
        "llm_type": os.getenv("llm_type", "openai"),
        "llm_name": os.getenv("llm_name", "gpt-4o"),
        "openai_api_key": os.getenv("openai_api_key"),
        "openai_base_url": os.getenv("openai_base_url"),
        "anthropic_api_key": os.getenv("anthropic_api_key"),
        "anthropic_base_url": os.getenv("anthropic_base_url"),
        "ollama_host": os.getenv("ollama_host", "http://localhost:11434"),
        "mistralai_api_key": os.getenv("mistralai_api_key"),
        "vertexai_system_instruction": os.getenv("vertexai_system_instruction"),
        # Embeddings
        "embedding_provider": os.getenv("embedding_provider"),
        "embedding_model": os.getenv("embedding_model", "text-embedding-3-small"),
        # Neo4j
        "neo4j_uri": os.getenv("neo4j_uri", "bolt://localhost:7687"),
        "neo4j_username": os.getenv("neo4j_username", "neo4j"),
        "neo4j_password": os.getenv("neo4j_password"),
        "neo4j_database": os.getenv("neo4j_database", "neo4j"),
        # Cache
        "cache_enabled": int(os.getenv("cache_enabled", "0")),
        # MCP Daemon Engine — forwarded to mcp_daemon_engine.handlers.config:Config
        "transport": os.getenv("MCP_TRANSPORT", "sse"),
        "funct_bucket_name": os.getenv("FUNCT_BUCKET_NAME"),
        "funct_zip_path": os.getenv("FUNCT_ZIP_PATH"),
        "funct_extract_path": os.getenv("FUNCT_EXTRACT_PATH"),
        # Shared-store backends (multi-process support)
        "task_backend": os.getenv("GATEWAY_TASK_BACKEND", "memory"),
        "task_table": os.getenv("GATEWAY_TASK_TABLE"),
        "task_ttl": os.getenv("GATEWAY_TASK_TTL"),
        "rate_limit_backend": os.getenv("GATEWAY_RATE_LIMIT_BACKEND", "memory"),
        "rate_limit_table": os.getenv("GATEWAY_RATE_LIMIT_TABLE"),
    }

    # Build functs_on_local from route manifest (data-driven, no hard-coded module names)
    # Each module with a config_class and graphql routes gets a local-function entry.
    manifest_for_functs = load_route_manifest(GatewayConfig)
    functs_on_local: Dict[str, Any] = {}
    for mod in manifest_for_functs:
        if mod.config_class:
            for route in mod.routes:
                if route.handler_type == "graphql" and route.dispatch:
                    # e.g. "knowledge_graph_engine.main:dispatch_graphql" → "dispatch_graphql"
                    func_name = (
                        route.dispatch.rsplit(":", 1)[-1]
                        if ":" in route.dispatch
                        else route.dispatch.rsplit(".", 1)[-1]
                    )
                    functs_on_local[func_name] = {
                        "module_name": mod.package,
                        "class_name": os.getenv(
                            f"FUNCTS_{mod.name.upper()}_CLASS",
                            mod.config_class.rsplit(":", 1)[-1].replace("Config", ""),
                        ),
                    }
    # Allow env var overrides / additions
    functs_on_local.update(json.loads(os.getenv("FUNCTS_ON_LOCAL_OVERRIDES", "{}")))
    setting["functs_on_local"] = functs_on_local

    return setting


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
