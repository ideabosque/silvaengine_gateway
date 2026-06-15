# -*- coding: utf-8 -*-
"""Tests for JWT local and middleware auth."""

import pytest
from silvaengine_gateway.auth.jwt_local import create_local_jwt, verify_local_jwt


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