# -*- coding: utf-8 -*-
"""WebSocket JWT verification helpers."""

from __future__ import print_function

__author__ = "silvaengine"

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import WebSocket

from .jwt_local import verify_local_jwt

logger = logging.getLogger(__name__)

WS_CLOSE_AUTH_FAILURE = 4001
WS_CLOSE_PARTITION_ERROR = 4002


async def verify_websocket_token(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """Verify a local JWT supplied by the WebSocket handshake."""
    token = websocket.query_params.get("token")
    if not token:
        logger.warning(
            "WebSocket auth failed: no token in query params (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Missing token")
        return None

    try:
        return verify_local_jwt(token)
    except Exception as exc:
        logger.warning(
            "WebSocket auth failed: invalid token (path=%s, error=%s)",
            _safe_path(websocket),
            exc,
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Invalid token")
        return None


async def verify_websocket_token_cognito(
    websocket: WebSocket,
) -> Optional[Dict[str, Any]]:
    """Verify a Cognito JWT supplied by the WebSocket handshake."""
    from .jwt_cognito import verify_cognito_jwt

    token = websocket.query_params.get("token")
    if not token:
        logger.warning(
            "WebSocket auth failed: no token in query params (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Missing token")
        return None

    try:
        return await verify_cognito_jwt(token)
    except Exception as exc:
        logger.warning(
            "WebSocket auth failed: invalid cognito token (path=%s, error=%s)",
            _safe_path(websocket),
            exc,
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Invalid token")
        return None


def resolve_websocket_part_id(websocket: WebSocket) -> Optional[str]:
    """Resolve the tenant partition id from query, headers, or subprotocols.

    Browser clients can use ``?part_id=<tenant>``. Non-browser clients can use
    a real ``Part-Id`` header. Clients that can only influence WebSocket
    subprotocols may send ``part-id:<tenant>`` in ``Sec-WebSocket-Protocol``.
    """
    values = _websocket_part_id_values(websocket)
    for value in values:
        if value:
            return value
    return None


def has_websocket_part_id_mismatch(websocket: WebSocket) -> bool:
    """Return True when multiple partition sources disagree."""
    values = {value for value in _websocket_part_id_values(websocket) if value}
    return len(values) > 1


def _websocket_part_id_values(websocket: WebSocket) -> Tuple[Optional[str], ...]:
    query_part_id = websocket.query_params.get("part_id")
    header_part_id = (
        websocket.headers.get("part-id")
        or websocket.headers.get("part_id")
        or websocket.headers.get("Part-Id")
        or websocket.headers.get("Part-ID")
    )
    subprotocol_part_id = _part_id_from_subprotocols(
        websocket.headers.get("sec-websocket-protocol")
    )
    return query_part_id, header_part_id, subprotocol_part_id


def _part_id_from_subprotocols(subprotocols: Optional[str]) -> Optional[str]:
    if not subprotocols:
        return None
    for proto in subprotocols.split(","):
        proto = proto.strip()
        if proto.startswith("part-id:"):
            return proto[len("part-id:") :]
    return None


def _safe_path(websocket: WebSocket) -> str:
    """Return the URL path without query string to avoid token leakage."""
    try:
        return websocket.url.path
    except Exception:
        return "<unknown>"


async def authenticate_websocket(
    websocket: WebSocket,
    auth_provider: str = "local",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Verify token and partition context for a WebSocket handshake."""
    if auth_provider == "cognito":
        claims = await verify_websocket_token_cognito(websocket)
    else:
        claims = await verify_websocket_token(websocket)

    if claims is None:
        return None, None

    if has_websocket_part_id_mismatch(websocket):
        logger.warning(
            "WebSocket partition failed: mismatched part_id sources (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(
            code=WS_CLOSE_PARTITION_ERROR,
            reason="Mismatched partition id",
        )
        return None, None

    part_id = resolve_websocket_part_id(websocket)
    if not part_id:
        logger.warning(
            "WebSocket partition failed: no part_id (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(
            code=WS_CLOSE_PARTITION_ERROR,
            reason="Missing partition id",
        )
        return None, None

    return claims, part_id
