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
            # verify_aud=False: this verifier only checks signature (+ exp,
            # manually below) — it has no configured expected audience.
            # python-jose (like PyJWT) rejects ANY token carrying an `aud`
            # claim unless the caller passes a matching `audience=`, even
            # with verify_exp disabled. Cross-engine service tokens (see
            # */utils/service_token.py's R6b payload convention) always set
            # `aud`, so without this they 401 here even with a valid
            # signature — real Banyan-routed calls are unaffected since
            # perm_engine.PermAuthorizer._verify_jwt passes a real
            # audience/issuer pair and validates it properly.
            options={"verify_exp": False, "verify_aud": False},
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
    # System plan 5.1 / E-2 (dev test bed): the local admin acts as the QA/QC
    # lead — claims carry user_id + roles so server-side RBAC (E-2)
    # attributes and authorizes decisions (Cognito replaces this in uat/prod).
    token = create_local_jwt(
        {
            "username": GatewayConfig.admin_username,
            "user_id": GatewayConfig.admin_username,
            "roles": ["qa-lead"],
            "role": "admin",
        },
        forever=True,
    )
    GatewayConfig.get_logger().info(f"Generated static admin token:\n   {token}")
    return token