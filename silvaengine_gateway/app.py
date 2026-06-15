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
from .router_builder import ModuleSpec, RouteSpec, build_router_from_manifest, validate_manifest

logger = logging.getLogger(__name__)


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
    env_routes_json = os.environ.get("GATEWAY_ROUTES_CONFIG_JSON") or config.routes_config_json
    if env_routes_json:
        try:
            modules = json.loads(env_routes_json)
            return [ModuleSpec(**m) for m in modules]
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to parse GATEWAY_ROUTES_CONFIG_JSON: {e}")
            raise

    # Priority 2: explicit path
    configured_path = config.routes_config_path or os.environ.get("GATEWAY_ROUTES_CONFIG_PATH")
    routes_file = Path(configured_path) if configured_path else Path(__file__).parent / "routes.yaml"

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
                    path="/{endpoint_id}/{part_id}/knowledge_graph_graphql",
                    handler_type="graphql",
                    dispatch="knowledge_graph_engine.main:dispatch_graphql",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/{part_id}/extract",
                    handler_type="background",
                    dispatch="knowledge_graph_engine.main:dispatch_extract",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/{part_id}/extract/status/{task_id}",
                    handler_type="task_status",
                    methods=["GET"],
                    auth=True,
                ),
            ],
        )
    ]


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

    # Initialize core KGE config
    _init_core_config(setting)

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

    app = FastAPI(
        title="SilvaEngine Gateway",
        description="FastAPI gateway with auth, module routing, and dispatch",
        lifespan=lifespan,
    )

    # CORS
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Rate limiting (global)
    rate_limit = int(os.environ.get("GATEWAY_RATE_LIMIT", "100"))
    rate_window = int(os.environ.get("GATEWAY_RATE_WINDOW", "60"))
    app.add_middleware(RateLimitMiddleware, max_requests=rate_limit, window_seconds=rate_window)

    # Auth middleware
    from .auth.middleware import FlexJWTMiddleware
    app.add_middleware(FlexJWTMiddleware, public_paths=["/health", "/auth"])

    # Auth routes
    from .routes.auth import router as auth_router
    app.include_router(auth_router)

    # Health routes
    from .routes.health import router as health_router
    app.include_router(health_router)

    # Load and validate route manifest
    manifest = load_route_manifest(GatewayConfig)
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


def _init_core_config(setting: Dict[str, Any]) -> None:
    """Initialize the core knowledge_graph_engine Config from shared settings."""
    try:
        from knowledge_graph_engine.handlers.config import Config as KGEConfig

        kge_settings = {
            k: v for k, v in setting.items()
            if k not in (
                "auth_provider", "jwt_secret_key", "jwt_algorithm", "access_token_exp",
                "admin_username", "admin_password", "admin_static_token",
                "cognito_user_pool_id", "cognito_app_client_id", "cognito_app_secret",
                "cognito_jwks_url", "jwks_cache_ttl", "local_user_file",
                "host", "port", "workers",
                "routes_config_path", "routes_config_json",
            )
        }

        if kge_settings:
            logger_msg = logging.getLogger("knowledge_graph_engine")
            KGEConfig.initialize(logger_msg, kge_settings)
    except Exception as e:
        logger.warning(f"Could not initialize KGE Config: {e}")


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

    setting = {
        # AWS (shared with core)
        "region_name": os.getenv("region_name"),
        "aws_access_key_id": os.getenv("aws_access_key_id"),
        "aws_secret_access_key": os.getenv("aws_secret_access_key"),
        # Tenant
        "endpoint_id": os.getenv("endpoint_id"),
        "part_id": os.getenv("part_id"),
        # Auth (gateway-specific)
        "auth_provider": os.getenv("GATEWAY_AUTH_PROVIDER", os.getenv("AUTH_PROVIDER", "local")),
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
    }

    app = create_app(setting)

    host = GatewayConfig.host
    port = GatewayConfig.port

    uvicorn.run(app, host=host, port=port)