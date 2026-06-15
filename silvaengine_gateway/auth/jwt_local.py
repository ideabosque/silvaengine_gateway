# -*- coding: utf-8 -*-
"""Local JWT creation and verification."""

from __future__ import print_function

__author__ = "silvaengine"

from functools import lru_cache
from typing import Any, Dict

import pendulum
from fastapi import HTTPException
from jose import JWTError, jwt

from ..config import GatewayConfig


def _expiry():
    return pendulum.now("UTC").add(minutes=GatewayConfig.access_token_exp)


def create_local_jwt(payload: Dict[str, Any], forever: bool = False) -> str:
    data = payload.copy()
    if forever:
        data["perm"] = True
    else:
        data["exp"] = _expiry()
    return jwt.encode(data, GatewayConfig.jwt_secret_key, algorithm=GatewayConfig.jwt_algorithm)


def verify_local_jwt(token: str) -> Dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            GatewayConfig.jwt_secret_key,
            algorithms=[GatewayConfig.jwt_algorithm],
            options={"verify_exp": False},
        )
        if not claims.get("perm"):
            if (
                claims.get("exp") is None
                or pendulum.now("UTC").timestamp() > claims["exp"]
            ):
                raise JWTError("expired")
        return claims
    except JWTError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid JWT ({e})",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


@lru_cache
def get_or_create_admin_token() -> str:
    if GatewayConfig.admin_static_token:
        return GatewayConfig.admin_static_token
    token = create_local_jwt(
        {"username": GatewayConfig.admin_username, "role": "admin"}, forever=True
    )
    GatewayConfig.get_logger().info(f"Generated static admin token:\n   {token}")
    return token