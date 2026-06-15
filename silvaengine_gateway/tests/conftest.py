# -*- coding: utf-8 -*-
"""Test fixtures for gateway tests — unit + integration."""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from silvaengine_gateway.config import GatewayConfig
from silvaengine_gateway.app import create_app

# Load .env from tests/ directory for integration tests
_test_env_path = Path(__file__).parent / ".env"
if _test_env_path.exists():
    load_dotenv(_test_env_path)


# ── Unit test fixtures (no external services) ──────────────────────

@pytest.fixture
def app():
    """Create a test app with local auth (no external services)."""
    setting = {
        "auth_provider": "local",
        "jwt_secret_key": "test-secret-key",
        "admin_username": "admin",
        "admin_password": "admin123",
        "port": "8000",
    }
    return create_app(setting)


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def admin_token(client):
    """Get an admin JWT token for authenticated requests."""
    response = client.post(
        "/auth/token",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token):
    """Return authorization headers with a valid token."""
    return {"Authorization": f"Bearer {admin_token}"}


# ── Integration test fixtures (requires DynamoDB + Neo4j) ──────────

@pytest.fixture
def integration_setting():
    """Build settings dict from .env for integration tests."""
    return {
        # Auth
        "auth_provider": os.getenv("GATEWAY_AUTH_PROVIDER", "local"),
        "jwt_secret_key": os.getenv("JWT_SECRET_KEY", "integ-test-secret"),
        "jwt_algorithm": os.getenv("JWT_ALGORITHM", "HS256"),
        "access_token_exp": os.getenv("ACCESS_TOKEN_EXP", "60"),
        "admin_username": os.getenv("ADMIN_USERNAME", "admin"),
        "admin_password": os.getenv("ADMIN_PASSWORD", "admin123"),
        # Server
        "host": os.getenv("GATEWAY_HOST", "0.0.0.0"),
        "port": os.getenv("GATEWAY_PORT", "8765"),
        "workers": os.getenv("GATEWAY_WORKERS", "1"),
        # AWS (shared with KGE core)
        "region_name": os.getenv("region_name", "us-west-2"),
        "aws_access_key_id": os.getenv("aws_access_key_id", ""),
        "aws_secret_access_key": os.getenv("aws_secret_access_key", ""),
        # Neo4j
        "neo4j_uri": os.getenv("neo4j_uri", "bolt://localhost:7687"),
        "neo4j_username": os.getenv("neo4j_username", "neo4j"),
        "neo4j_password": os.getenv("neo4j_password", ""),
        "neo4j_database": os.getenv("neo4j_database", "neo4j"),
        # LLM
        "llm_type": os.getenv("llm_type", "openai"),
        "openai_api_key": os.getenv("openai_api_key", ""),
        "openai_base_url": os.getenv("openai_base_url", ""),
        # Embeddings
        "embedding_provider": os.getenv("embedding_provider", ""),
        "embedding_model": os.getenv("embedding_model", "text-embedding-3-small"),
        # Cache
        "cache_enabled": os.getenv("cache_enabled", "0"),
    }


@pytest.fixture
def integration_app(integration_setting):
    """Create a full integration app with real AWS + Neo4j settings."""
    return create_app(integration_setting)


@pytest.fixture
def integration_client(integration_app):
    """Create a test client backed by real services."""
    return TestClient(integration_app)


@pytest.fixture
def integration_token(integration_client):
    """Get an admin JWT token against the integration app."""
    response = integration_client.post(
        "/auth/token",
        data={
            "username": os.getenv("ADMIN_USERNAME", "admin"),
            "password": os.getenv("ADMIN_PASSWORD", "admin123"),
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def integration_headers(integration_token):
    """Auth headers for integration tests."""
    return {"Authorization": f"Bearer {integration_token}"}