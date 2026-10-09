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
            # verify_aud: False — this gateway's own locally-minted tokens
            # (create_local_jwt above) never set an "aud" claim, but tokens
            # issued by modules like user_engine/perm_engine do (their
            # original silvaengine_base contract — see issue_access_token's
            # "aud": "banyan-graphql"). jose's jwt.decode() requires an
            # explicit `audience=` kwarg whenever the claim is present,
            # otherwise it raises "Invalid audience" even with a correct
            # signature — rejecting every such token with a 401 regardless
            # of validity. The HS256 signature (shared jwt_secret_key) is
            # what actually authenticates the token here; aud/iss are
            # informational across this gateway's multiple token issuers,
            # not enforced trust boundaries, so skip that check.
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
    token = create_local_jwt(
        {"username": GatewayConfig.admin_username, "role": "admin"}, forever=True
    )
    GatewayConfig.get_logger().info(f"Generated static admin token:\n   {token}")
    return token