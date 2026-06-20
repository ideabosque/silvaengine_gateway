# -*- coding: utf-8 -*-
"""Authentication helpers for SilvaEngine Gateway."""

from importlib import import_module

__all__ = [
    "create_local_jwt",
    "verify_local_jwt",
    "get_or_create_admin_token",
    "verify_cognito_jwt",
    "cleanup_http_client",
    "FlexJWTMiddleware",
    "get_current_user",
    "LocalUser",
    "load_users",
    # WebSocket auth
    "verify_websocket_token",
    "verify_websocket_token_cognito",
    "resolve_websocket_part_id",
    "authenticate_websocket",
    "WS_CLOSE_AUTH_FAILURE",
    "WS_CLOSE_PARTITION_ERROR",
]

_EXPORT_MODULES = {
    "create_local_jwt": ".jwt_local",
    "verify_local_jwt": ".jwt_local",
    "get_or_create_admin_token": ".jwt_local",
    "verify_cognito_jwt": ".jwt_cognito",
    "cleanup_http_client": ".jwt_cognito",
    "FlexJWTMiddleware": ".middleware",
    "get_current_user": ".middleware",
    "LocalUser": ".users",
    "load_users": ".users",
    # WebSocket auth
    "verify_websocket_token": ".websocket",
    "verify_websocket_token_cognito": ".websocket",
    "resolve_websocket_part_id": ".websocket",
    "authenticate_websocket": ".websocket",
    "WS_CLOSE_AUTH_FAILURE": ".websocket",
    "WS_CLOSE_PARTITION_ERROR": ".websocket",
}


def __getattr__(name):
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(_EXPORT_MODULES[name], __name__), name)
