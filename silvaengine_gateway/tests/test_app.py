# -*- coding: utf-8 -*-
"""Tests for gateway app factory and lifespan."""

import pytest
from jose import jwt
from fastapi.testclient import TestClient

from silvaengine_gateway.app import create_app


def test_create_app_from_environment_defaults():
    """Test that create_app can initialize without an explicit settings dict."""
    app = create_app()
    assert app.title == "SilvaEngine Gateway"


def test_create_app():
    """Test that create_app returns a FastAPI app."""
    app = create_app({
        "auth_provider": "local",
        "jwt_secret_key": "test-secret",
    })
    assert app is not None
    assert app.title == "SilvaEngine Gateway"


def test_health_endpoint():
    """Test /health returns ok without auth."""
    app = create_app({
        "auth_provider": "local",
        "jwt_secret_key": "test-secret",
    })
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "silvaengine-gateway"


def test_me_requires_auth():
    """Test /me returns 401 without auth."""
    app = create_app({
        "auth_provider": "local",
        "jwt_secret_key": "test-secret",
    })
    client = TestClient(app)
    response = client.get("/me")
    assert response.status_code == 401


def test_build_setting_from_env_uses_default_invoker_class_names(monkeypatch):
    """WebSocket helper functions map to concrete invoker classes by default."""
    monkeypatch.delenv("FUNCTS_AI_AGENT_CORE_ENGINE_CLASS", raising=False)
    monkeypatch.delenv("FUNCTS_KNOWLEDGE_GRAPH_ENGINE_CLASS", raising=False)
    monkeypatch.delenv("FUNCTS_ON_LOCAL_OVERRIDES", raising=False)

    from silvaengine_gateway.app import build_setting_from_env

    setting = build_setting_from_env()
    functs_on_local = setting["functs_on_local"]

    assert functs_on_local["send_data_to_stream"] == {
        "module_name": "ai_agent_core_engine",
        "class_name": "AIAgentCoreEngine",
    }
    assert functs_on_local["async_insert_update_tool_call"] == {
        "module_name": "ai_agent_core_engine",
        "class_name": "AIAgentCoreEngine",
    }
    assert functs_on_local["knowledge_graph_graphql"] == {
        "module_name": "knowledge_graph_engine",
        "class_name": "KnowledgeGraphEngine",
    }
    assert functs_on_local["ai_rfq_graphql"] == {
        "module_name": "ai_rfq_engine",
        "class_name": "AIRFQEngine",
    }
    assert functs_on_local["ai_agent_core_graphql"] == {
        "module_name": "ai_agent_core_engine",
        "class_name": "AIAgentCoreEngine",
    }
    assert "dispatch_graphql" not in functs_on_local


def test_build_setting_from_env_internal_mcp_keeps_explicit_bearer(monkeypatch):
    """Existing internal_mcp_bearer_token wins over generated credentials."""
    monkeypatch.setenv("internal_mcp_base_url", "http://localhost:8765")
    monkeypatch.setenv("part_id", "nestaging")
    monkeypatch.setenv("internal_mcp_bearer_token", "explicit-token")
    monkeypatch.setenv("internal_mcp_token_username", "admin")
    monkeypatch.setenv("internal_mcp_token_password", "admin123")
    monkeypatch.delenv("FUNCTS_ON_LOCAL_OVERRIDES", raising=False)

    from silvaengine_gateway.app import build_setting_from_env

    setting = build_setting_from_env()

    assert setting["internal_mcp"]["base_url"] == "http://localhost:8765/{endpoint_id}/mcp"
    assert "Part-Id" not in setting["internal_mcp"]["headers"]
    assert setting["internal_mcp"]["bearer_token"] == "explicit-token"


def test_build_setting_from_env_internal_mcp_generates_local_admin_token(monkeypatch):
    """Internal MCP username/password can generate a local gateway JWT."""
    monkeypatch.setenv("internal_mcp_base_url", "http://localhost:8765")
    monkeypatch.setenv("part_id", "nestaging")
    monkeypatch.delenv("internal_mcp_bearer_token", raising=False)
    monkeypatch.setenv("GATEWAY_AUTH_PROVIDER", "local")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    monkeypatch.delenv("ADMIN_STATIC_TOKEN", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("internal_mcp_token_username", "admin")
    monkeypatch.setenv("internal_mcp_token_password", "admin123")
    monkeypatch.delenv("FUNCTS_ON_LOCAL_OVERRIDES", raising=False)

    from silvaengine_gateway.app import build_setting_from_env

    setting = build_setting_from_env()
    assert setting["internal_mcp"]["base_url"] == "http://localhost:8765/{endpoint_id}/mcp"
    assert "Part-Id" not in setting["internal_mcp"]["headers"]
    token = setting["internal_mcp"]["bearer_token"]
    claims = jwt.decode(token, "test-secret", algorithms=["HS256"])

    assert claims["username"] == "admin"
    assert claims["role"] == "admin"
    assert claims["perm"] is True

