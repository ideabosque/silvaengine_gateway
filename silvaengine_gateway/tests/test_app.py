# -*- coding: utf-8 -*-
"""Tests for gateway app factory and lifespan."""

import pytest
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
