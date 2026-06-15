#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launch the SilvaEngine Gateway daemon for integration testing.

Usage:
    python -m silvaengine_gateway.tests.run_daemon [--port PORT] [--dotenv PATH]

Reads environment variables from a .env file (defaults to the .env file
in the same directory as this script). Starts the gateway on the given
port and blocks until Ctrl+C.

Typical workflow:
    1. Terminal 1:  python -m silvaengine_gateway.tests.run_daemon
    2. Terminal 2:  python -m silvaengine_gateway.tests.call_search
"""

from __future__ import print_function

__author__ = "silvaengine"
import argparse
import logging
import os
import sys
from pathlib import Path

# ── Ensure project roots are on sys.path ───────────────────────────
# When run via VS Code debugger or `python path/to/run_daemon.py`,
# the package roots aren't on sys.path. Add them so both
# `silvaengine_gateway` and `knowledge_graph_engine` can be imported
# regardless of how this script is invoked.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_KGE_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent / "knowledge_graph_engine")
for _p in [_PROJECT_ROOT, _KGE_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvicorn
from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the SilvaEngine Gateway daemon for integration testing"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port to listen on (default: from .env or 8765)"
    )
    parser.add_argument(
        "--dotenv", type=str, default=None,
        help="Path to .env file (default: <this_script_dir>/.env)"
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Host to bind (default: from .env or 0.0.0.0)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── Load .env ───────────────────────────────────────────────────
    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"ERROR: .env file not found: {env_file}")
        print("Copy .env.example to .env and fill in real values.")
        sys.exit(1)

    load_dotenv(env_file, override=True)
    print(f"Loaded environment from: {env_file}")

    # Ensure OPENAI_API_KEY (uppercase) is set for libraries that read it from env
    # (dotenv preserves case, but openai/neo4j_graphrag expect uppercase)
    _oak = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
    if _oak:
        os.environ["OPENAI_API_KEY"] = _oak

    # ── Resolve CLI overrides (CLI arg > .env > hardcoded default) ──
    host = args.host or os.getenv("GATEWAY_HOST", "0.0.0.0")
    port = args.port or int(os.getenv("GATEWAY_PORT", "8765"))

    # ── Configure logging ───────────────────────────────────────────
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # ── Build setting dict (all from .env, no hardcoded secrets) ────
    setting = {
        # AWS (shared with core)
        "region_name": os.getenv("region_name"),
        "aws_access_key_id": os.getenv("aws_access_key_id"),
        "aws_secret_access_key": os.getenv("aws_secret_access_key"),
        # Tenant
        "endpoint_id": os.getenv("endpoint_id", "test-ep"),
        "part_id": os.getenv("part_id", "test-part"),
        # Auth (gateway-specific)
        "auth_provider": os.getenv("GATEWAY_AUTH_PROVIDER", os.getenv("AUTH_PROVIDER", "local")),
        "jwt_secret_key": os.getenv("JWT_SECRET_KEY", "CHANGEME"),
        "jwt_algorithm": os.getenv("JWT_ALGORITHM", "HS256"),
        "access_token_exp": os.getenv("ACCESS_TOKEN_EXP", "15"),
        "admin_username": os.getenv("ADMIN_USERNAME", "admin"),
        "admin_password": os.getenv("ADMIN_PASSWORD", "admin123"),
        "admin_static_token": os.getenv("ADMIN_STATIC_TOKEN", ""),
        "local_user_file": os.getenv("LOCAL_USER_FILE", ""),
        # Cognito
        "cognito_user_pool_id": os.getenv("COGNITO_USER_POOL_ID", ""),
        "cognito_app_client_id": os.getenv("COGNITO_APP_CLIENT_ID", ""),
        "cognito_app_secret": os.getenv("COGNITO_APP_SECRET", ""),
        "cognito_jwks_url": os.getenv("COGNITO_JWKS_URL", ""),
        # Server
        "host": host,
        "port": str(port),
        "workers": os.getenv("GATEWAY_WORKERS", "1"),
        # Route manifest
        "routes_config_path": os.getenv("GATEWAY_ROUTES_CONFIG_PATH"),
        "routes_config_json": os.getenv("GATEWAY_ROUTES_CONFIG_JSON"),
        # DynamoDB tables
        "initialize_tables": int(os.getenv("initialize_tables", "0")),
        # LLM (shared with core)
        "llm_type": os.getenv("llm_type", "openai"),
        "llm_name": os.getenv("llm_name", "gpt-4o"),
        "openai_api_key": os.getenv("openai_api_key", ""),
        "openai_base_url": os.getenv("openai_base_url") or None,
        "anthropic_api_key": os.getenv("anthropic_api_key", ""),
        "anthropic_base_url": os.getenv("anthropic_base_url") or None,
        "ollama_host": os.getenv("ollama_host", "http://localhost:11434"),
        "mistralai_api_key": os.getenv("mistralai_api_key", ""),
        "vertexai_system_instruction": os.getenv("vertexai_system_instruction", ""),
        # Ensure OPENAI_API_KEY is set for libraries that read it from env (neo4j_graphrag, openai)
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key", ""),
        # Embeddings
        "embedding_provider": os.getenv("embedding_provider", ""),
        "embedding_model": os.getenv("embedding_model", "text-embedding-3-small"),
        # Neo4j
        "neo4j_uri": os.getenv("neo4j_uri", "bolt://localhost:7687"),
        "neo4j_username": os.getenv("neo4j_username", "neo4j"),
        "neo4j_password": os.getenv("neo4j_password", ""),
        "neo4j_database": os.getenv("neo4j_database", "neo4j"),
        # Cache
        "cache_enabled": int(os.getenv("cache_enabled", "0")),
    }

    # ── Create app ──────────────────────────────────────────────────
    from silvaengine_gateway.app import create_app

    app = create_app(setting)

    print(f"\n{'='*60}")
    print(f"  SilvaEngine Gateway")
    print(f"  http://{host}:{port}")
    print(f"  Auth: {setting['auth_provider']}")
    print(f"  Endpoint: {setting['endpoint_id']} / Partition: {setting['part_id']}")
    print(f"  Neo4j: {setting['neo4j_uri']}")
    print(f"{'='*60}\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()