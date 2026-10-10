# -*- coding: utf-8 -*-
"""Tests for JWT local and middleware auth."""

import pytest
from unittest.mock import Mock

from fastapi.security import OAuth2PasswordRequestForm
from silvaengine_gateway.auth.jwt_local import create_local_jwt, verify_local_jwt


def test_module_login_without_endpoint_selection(monkeypatch):
    from silvaengine_gateway.config import GatewayConfig
    from silvaengine_gateway.routes.auth import login

    dispatch = Mock(return_value={"body": '{"data":{"login":{"authToken":"module-token","requiresMfa":false}}}'})
    resolve = Mock(return_value=dispatch)
    monkeypatch.setattr(GatewayConfig, "auth_provider", "module")
    monkeypatch.setattr(GatewayConfig, "auth_module_dispatch", "custom_auth:dispatch")
    monkeypatch.setattr("silvaengine_gateway.router_builder.resolve_dispatch", resolve)

    result = login(OAuth2PasswordRequestForm(username="user@example.com", password="password"))

    assert result == {"access_token": "module-token", "token_type": "bearer"}
    resolve.assert_called_once_with("custom_auth:dispatch")
    kwargs = dispatch.call_args.kwargs
    assert kwargs["endpoint_id"] is None
    assert "part_id" not in kwargs
    assert kwargs["variables"]["input"] == {"email": "user@example.com", "password": "password"}


def test_create_and_verify_local_jwt():
    """Test JWT creation and verification."""
    from silvaengine_gateway.config import GatewayConfig

    GatewayConfig.initialize(
        __import__("logging").getLogger("test"),
        {"jwt_secret_key": "test-secret", "jwt_algorithm": "HS256", "auth_provider": "local"},
    )

    token = create_local_jwt({"username": "testuser", "roles": ["user"]})
    claims = verify_local_jwt(token)
    assert claims["username"] == "testuser"
    assert "roles" in claims


def test_create_permanent_jwt():
    """Test permanent JWT creation."""
    from silvaengine_gateway.config import GatewayConfig

    GatewayConfig.initialize(
        __import__("logging").getLogger("test"),
        {"jwt_secret_key": "test-secret", "jwt_algorithm": "HS256", "auth_provider": "local"},
    )

    token = create_local_jwt({"username": "admin", "role": "admin"}, forever=True)
    claims = verify_local_jwt(token)
    assert claims["username"] == "admin"
    assert claims.get("perm") is True


def test_service_token_verifies_with_shared_jwt_secret(monkeypatch):
    """Without jwt_secret_key (se-configdata deployments), FlexJWT uses jwt_secret."""
    import time

    import jwt as pyjwt
    from silvaengine_gateway.config import GatewayConfig

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    GatewayConfig.initialize(
        __import__("logging").getLogger("test"),
        {"jwt_secret_key": None, "jwt_secret": "shared-ddb-secret", "auth_provider": "local"},
    )
    assert GatewayConfig.jwt_secret_key == "shared-ddb-secret"

    token = pyjwt.encode(
        {"sub": "u-1", "aud": "banyan-graphql", "iss": "banyan", "exp": int(time.time()) + 60},
        "shared-ddb-secret",
        algorithm="HS256",
    )
    assert verify_local_jwt(token)["sub"] == "u-1"
