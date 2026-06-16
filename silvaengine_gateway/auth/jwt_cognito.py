# -*- coding: utf-8 -*-
"""AWS Cognito JWT verification."""

from __future__ import print_function

__author__ = "silvaengine"

from time import monotonic
from typing import Any, Dict

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt

from ..config import GatewayConfig

_JWKS_CACHE: Dict[str, Any] | None = None
_JWKS_EXPIRES_AT = 0.0
_HTTP_CLIENT: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=10.0,
            http2=True,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
    return _HTTP_CLIENT


async def _jwks(force_refresh: bool = False) -> Dict[str, Any]:
    global _JWKS_CACHE, _JWKS_EXPIRES_AT
    now = monotonic()
    if force_refresh or _JWKS_CACHE is None or now >= _JWKS_EXPIRES_AT:
        client = await _get_http_client()
        resp = await client.get(GatewayConfig.jwks_endpoint)
        resp.raise_for_status()
        _JWKS_CACHE = resp.json()
        _JWKS_EXPIRES_AT = now + (GatewayConfig.jwks_cache_ttl or 3600)
    assert _JWKS_CACHE is not None
    return _JWKS_CACHE


def _find_signing_key(jwks_data: Dict[str, Any], kid: Any) -> Dict[str, Any] | None:
    """Return the JWK whose ``kid`` matches, or None if not present."""
    for key in jwks_data.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


async def verify_cognito_jwt(token: str) -> Dict[str, Any]:
    try:
        head = jwt.get_unverified_header(token)
        kid = head.get("kid")

        jwks_data = await _jwks()
        key = _find_signing_key(jwks_data, kid)
        if key is None:
            # The cached JWKS may be stale because Cognito rotated its signing
            # keys. Force a single refresh before declaring the key unknown —
            # otherwise a legitimate post-rotation token is rejected until the
            # cache TTL expires.
            jwks_data = await _jwks(force_refresh=True)
            key = _find_signing_key(jwks_data, kid)
        if key is None:
            # Distinct from a tampered/invalid signature: the token references a
            # signing key that is genuinely not published in the JWKS.
            raise HTTPException(
                status_code=401,
                detail=f"Unknown token signing key (kid={kid!r}); not present in JWKS",
                headers={"WWW-Authenticate": "Bearer"},
            )

        claims = jwt.decode(
            token,
            key,
            algorithms=[key["alg"]],
            audience=GatewayConfig.cognito_app_client_id,
            issuer=GatewayConfig.issuer,
        )
        return claims
    except HTTPException:
        # Already a precise auth error (e.g. unknown signing key) — don't mask it.
        raise
    except JWTError as e:
        raise HTTPException(
            status_code=401,
            detail="Invalid Cognito JWT",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def cleanup_http_client():
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None