# -*- coding: utf-8 -*-
"""WebSocket handshake authentication helpers.

Providers (chosen per route; default = the gateway ``auth_provider``):

- ``local``      — gateway-issued JWT (``verify_local_jwt``);
- ``cognito``    — Cognito JWT;
- ``authorizer`` — any Lambda-style authorizer class, set up through
  variables instead of code. The class is built as ``cls(logger, **setting)``
  and its method is called with a synthesized proxy event — the same
  contract the HTTP auth bridges use. Each option is read from the route's
  ``authorizer`` mapping first, then from the gateway setting
  (``settings.yaml`` → environment), then the default:

  - ``class`` / ``ws_authorizer`` / ``WS_AUTHORIZER`` — required,
    ``"package.module:ClassName"``;
  - ``method`` / ``ws_authorizer_method`` / ``WS_AUTHORIZER_METHOD`` —
    default ``verify_permission``;
  - ``partition_claim`` / ``ws_partition_claim`` / ``WS_PARTITION_CLAIM`` —
    default ``tenant_id``;
  - ``admin_claim`` / ``ws_admin_claim`` / ``WS_ADMIN_CLAIM`` —
    default ``is_admin``.

  Claims are read from the returned event's ``requestContext.authorizer``.
  With a ``partition_claim``, a non-admin caller may only use the partition
  named in that claim, which is also the default when the client sends none.
  Set ``partition_claim`` (or ``admin_claim``) to ``none`` to turn it off.
"""

from __future__ import print_function

__author__ = "silvaengine"

import asyncio
import importlib
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


_AUTHORIZER_DEFAULTS = {
    "method": "verify_permission",
    "partition_claim": "tenant_id",
    "admin_claim": "is_admin",
}
_AUTHORIZER_SETTING_KEYS = {
    "class": "ws_authorizer",
    "method": "ws_authorizer_method",
    "partition_claim": "ws_partition_claim",
    "admin_claim": "ws_admin_claim",
}


def resolve_authorizer_options(
    route_options: Optional[Dict[str, Any]],
    setting: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Route ``authorizer`` options over gateway settings over defaults."""
    route_options = dict(route_options or {})
    setting = setting or {}
    resolved: Dict[str, Any] = {}
    for key, setting_key in _AUTHORIZER_SETTING_KEYS.items():
        value = route_options.get(key)
        if value in (None, ""):
            value = setting.get(setting_key)
        if value in (None, ""):
            value = _AUTHORIZER_DEFAULTS.get(key)
        if value is False or str(value).strip().lower() == "none":
            value = None  # explicit opt-out, e.g. no partition binding
        resolved[key] = value
    return resolved


def _load_authorizer_class(path: str) -> Any:
    module_name, _, attr = str(path).partition(":")
    if not module_name or not attr:
        raise ImportError(
            f"authorizer class must be 'package.module:Class', got {path!r}"
        )
    return getattr(importlib.import_module(module_name), attr)


def _authorizer_proxy_event(websocket: WebSocket, token: str) -> Dict[str, Any]:
    """Lambda-style proxy event for an authorizer. Browsers cannot set
    headers on a WebSocket upgrade, so the ``?token=`` value is presented as
    ``Authorization: Bearer <token>`` and dropped from the query."""
    headers = {k: v for k, v in websocket.headers.items()}
    headers["authorization"] = f"Bearer {token}"
    query = {k: v for k, v in websocket.query_params.items() if k != "token"}
    return {
        "httpMethod": "GET",
        "path": websocket.url.path,
        "headers": headers,
        "queryStringParameters": query,
        "body": None,
        "requestContext": {"http": {"method": "GET"}},
    }


async def verify_websocket_token_authorizer(
    websocket: WebSocket,
    options: Dict[str, Any],
    setting: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Verify the handshake token through the configured authorizer class.

    Fails closed: any import, configuration or verification error closes the
    socket and returns ``None``.
    """
    token = websocket.query_params.get("token")
    if not token:
        logger.warning(
            "WebSocket auth failed: no token in query params (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Missing token")
        return None

    try:
        if not options.get("class"):
            raise ImportError("no authorizer class configured (WS_AUTHORIZER)")
        authorizer_cls = _load_authorizer_class(options["class"])
        authorizer = authorizer_cls(
            logging.getLogger("silvaengine_gateway.ws_authorizer"),
            **dict(setting or {}),
        )
        method = getattr(authorizer, options["method"])
    except Exception as exc:  # noqa: BLE001 — misconfiguration fails closed
        logger.error(
            "WebSocket authorizer unavailable (path=%s): %s",
            _safe_path(websocket),
            exc,
        )
        await websocket.close(code=1011, reason="Authorizer unavailable")
        return None

    event = _authorizer_proxy_event(websocket, token)
    try:
        # Authorizers may block (e.g. DB role lookups) — use a worker thread.
        result = await asyncio.get_running_loop().run_in_executor(
            None, method, event, None
        )
    except Exception as exc:  # noqa: BLE001 — any failure rejects the upgrade
        logger.warning(
            "WebSocket auth failed: authorizer rejected token (path=%s, error=%s)",
            _safe_path(websocket),
            exc,
        )
        await websocket.close(code=WS_CLOSE_AUTH_FAILURE, reason="Invalid token")
        return None

    claims = ((result or {}).get("requestContext") or {}).get("authorizer")
    if not isinstance(claims, dict) or not claims:
        logger.warning(
            "WebSocket auth failed: authorizer returned no identity (path=%s)",
            _safe_path(websocket),
        )
        await websocket.close(
            code=WS_CLOSE_AUTH_FAILURE, reason="Authentication required"
        )
        return None
    return claims


async def authenticate_websocket(
    websocket: WebSocket,
    auth_provider: str = "local",
    setting: Optional[Dict[str, Any]] = None,
    authorizer_options: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Verify token and partition context for a WebSocket handshake."""
    options: Dict[str, Any] = {}
    if auth_provider == "authorizer":
        options = resolve_authorizer_options(authorizer_options, setting)
        claims = await verify_websocket_token_authorizer(websocket, options, setting)
    elif auth_provider == "cognito":
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

    partition_claim = options.get("partition_claim")
    if partition_claim:
        # Bind the partition to the verified identity: a non-admin caller may
        # not register a connection under another tenant.
        claimed = claims.get(partition_claim)
        claimed = str(claimed) if claimed else None
        is_admin = bool(claims.get(options.get("admin_claim") or ""))
        if part_id and claimed and part_id != claimed and not is_admin:
            logger.warning(
                "WebSocket partition failed: part_id does not match the "
                "token's %s claim (path=%s)",
                partition_claim,
                _safe_path(websocket),
            )
            await websocket.close(
                code=WS_CLOSE_PARTITION_ERROR,
                reason="Partition not allowed",
            )
            return None, None
        part_id = part_id or claimed

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
