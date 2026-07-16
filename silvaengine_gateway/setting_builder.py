# -*- coding: utf-8 -*-
"""
Build the gateway ``setting`` dict from environment variables.

``build_setting_from_env()`` is the single source of truth for the setting dict
that is handed to ``create_app()`` and forwarded (minus each module's
``config_exclude_keys``) to every module's ``Config.initialize()``. Keeping the
env -> setting contract in one module means the daemon, the uvicorn factory, and
the test helpers all see an identical configuration.

Also builds two derived pieces of that contract:

- ``internal_mcp``  — config ai_agent_core uses to call back into the gateway.
- ``functs_on_local`` — the invoker's local-dispatch map, derived from the
  route manifest rather than hard-coded module names.
"""

from __future__ import print_function

__author__ = "silvaengine"

import functools
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

import yaml

from .config import GatewayConfig
from .manifest import load_route_manifest
from .router_builder import ModuleSpec

logger = logging.getLogger(__name__)

# Concrete Invoker class names per package. Used when a module does not set
# FUNCTS_<NAME>_CLASS and its config_class does not imply a usable name.
_DEFAULT_INVOKER_CLASS_NAMES = {
    "ai_agent_core_engine": "AIAgentCoreEngine",
    "ai_coordination_engine": "AICoordinationEngine",
    "rfq_engine": "RFQEngine",
    "knowledge_graph_engine": "KnowledgeGraphEngine",
    "mcp_daemon_engine": "MCPDaemonEngine",
}


# ---------------------------------------------------------------------------
# functs_on_local helpers
# ---------------------------------------------------------------------------


def _module_invoker_class_name(module: ModuleSpec) -> str:
    """Return the class name used by downstream Invoker mappings."""
    configured = os.getenv(f"FUNCTS_{module.name.upper()}_CLASS")
    if configured:
        return configured

    default_name = _DEFAULT_INVOKER_CLASS_NAMES.get(module.package)
    if default_name:
        return default_name

    if module.config_class:
        config_name = module.config_class.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
        if config_name and config_name != "Config":
            return config_name.replace("Config", "")

    return "".join(part.capitalize() for part in module.package.split("_"))


# ---------------------------------------------------------------------------
# Internal MCP config (forwarded to ai_agent_core_engine)
# ---------------------------------------------------------------------------


def _internal_mcp_base_url() -> str:
    """Return the configured internal MCP gateway base URL."""
    return os.getenv("internal_mcp_base_url", "").rstrip("/")


def _build_internal_mcp_headers() -> Dict[str, Any]:
    """Build static headers shared by all internal MCP calls.

    The tenant Part-Id is added later by ai_agent_core from request context.
    """
    return {
        "x-api-key": os.getenv("x-api-key"),
        "Content-Type": "application/json",
    }


def _generate_local_internal_mcp_token(username: str, password: str) -> str:
    """Generate an internal MCP bearer token from local gateway credentials."""
    admin_username = os.getenv("ADMIN_USERNAME", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    admin_static_token = os.getenv("ADMIN_STATIC_TOKEN", "")

    if admin_username and admin_password:
        if username == admin_username and password == admin_password:
            if admin_static_token:
                return admin_static_token
            from jose import jwt

            payload = {
                "username": admin_username,
                "role": "admin",
                "perm": True,
            }
            return jwt.encode(
                payload,
                os.getenv("JWT_SECRET_KEY", "CHANGEME"),
                algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            )

    local_user_file = os.getenv("LOCAL_USER_FILE")
    if local_user_file:
        import pendulum
        from jose import jwt

        from .auth.users import load_users

        user = load_users(local_user_file).get(username)
        if user and user.verify(password):
            exp = pendulum.now("UTC").add(
                minutes=int(os.getenv("ACCESS_TOKEN_EXP", "15"))
            )
            return jwt.encode(
                {"username": user.username, "roles": user.roles, "exp": exp},
                os.getenv("JWT_SECRET_KEY", "CHANGEME"),
                algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            )

    return ""


def _generate_cognito_internal_mcp_token(username: str, password: str) -> str:
    """Generate an internal MCP bearer token via the Cognito IdP SDK.

    Uses GatewayConfig.aws_cognito_idp directly — no HTTP call to the
    gateway's own /auth/token endpoint, so it works at startup before the
    daemon is listening.  Returns an empty string on any failure so the
    caller can fall through to the lazy-fetch path.
    """
    import base64
    import hashlib
    import hmac

    client = GatewayConfig.aws_cognito_idp
    if client is None:
        return ""

    client_id = GatewayConfig.cognito_app_client_id
    client_secret = GatewayConfig.cognito_app_secret
    secret_hash = ""
    if client_id and client_secret:
        message = (username + client_id).encode("utf-8")
        key = client_secret.encode("utf-8")
        digest = hmac.new(key, message, hashlib.sha256).digest()
        secret_hash = base64.b64encode(digest).decode()

    try:
        resp = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client_id,
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": secret_hash,
            },
        )
        tokens = resp.get("AuthenticationResult", {})
        return tokens.get("AccessToken", "")
    except Exception as exc:
        logger.warning(
            "Cognito initiate_auth failed for internal MCP token: %s", exc
        )
        return ""


def _resolve_internal_mcp_bearer_token(base_url: str) -> str:
    """Resolve the bearer token for internal MCP.

    The token is generated via the gateway's own auth path — no HTTP call
    to /auth/token, so it works at startup before the daemon is listening.

    * Explicit ``internal_mcp_bearer_token`` env var wins.
    * Local auth: JWT generated synchronously (no network I/O).
    * Cognito auth: ``initiate_auth`` via the Cognito IdP SDK client.
    """
    bearer_token = os.getenv("internal_mcp_bearer_token", "")
    if bearer_token:
        return bearer_token

    username = os.getenv("internal_mcp_token_username", "")
    password = os.getenv("internal_mcp_token_password", "")
    if not username or not password:
        return ""

    auth_provider = os.getenv(
        "GATEWAY_AUTH_PROVIDER", os.getenv("AUTH_PROVIDER", "local")
    )

    if auth_provider == "local":
        return _generate_local_internal_mcp_token(username, password)

    if auth_provider == "cognito":
        token = _generate_cognito_internal_mcp_token(username, password)
        if token:
            return token
        logger.warning(
            "Cognito IdP client not available or auth failed; "
            "internal MCP bearer token will be empty."
        )

    return ""


# Re-mint the internal MCP token this many seconds before it actually expires,
# so a call can't start with a token that dies mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


def _token_expiry(token: str) -> float | None:
    """Return a token's ``exp`` (epoch seconds), or None if it never expires.

    Both locally minted JWTs and Cognito access tokens are JWTs, so one claim
    read covers both providers. Local admin tokens carry ``perm: True`` and no
    ``exp`` — those never expire. Anything unreadable is treated as
    non-expiring, since re-minting on every call would be worse than a stale
    token that may still be valid.
    """
    try:
        from jose import jwt as _jose_jwt

        claims = _jose_jwt.get_unverified_claims(token)
    except Exception:
        return None

    if claims.get("perm"):
        return None
    exp = claims.get("exp")
    try:
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _make_internal_mcp_token_provider(base_url: str) -> Callable[[], str]:
    """Return a cached, expiry-aware provider for the internal MCP bearer token.

    ai_agent_core calls this once per request. Without it, the token resolved at
    startup is frozen forever and internal MCP calls start returning 401 as soon
    as it expires (~1h for Cognito; ACCESS_TOKEN_EXP for local user-file tokens)
    until the gateway is restarted.

    The token is re-minted only when it is within
    ``_TOKEN_REFRESH_MARGIN_SECONDS`` of expiry, so the common path is a cheap
    dict read rather than a Cognito round-trip on every MCP call. A lock keeps
    concurrent gateway dispatch threads from stampeding ``initiate_auth``.

    An explicitly configured ``internal_mcp_bearer_token`` is treated as static:
    re-resolving would just return the same env value.
    """
    state: Dict[str, Any] = {"token": "", "exp": None}
    lock = threading.Lock()
    is_static = bool(os.getenv("internal_mcp_bearer_token", ""))

    def _provider() -> str:
        with lock:
            token = state["token"]
            exp = state["exp"]
            if token and (
                is_static
                or exp is None
                or time.time() < exp - _TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return token

            new_token = _resolve_internal_mcp_bearer_token(base_url)
            if not new_token:
                # Keep the previous token rather than going blank: it may still
                # be valid, and a blank header is a guaranteed 401.
                logger.warning(
                    "Internal MCP token refresh returned empty; keeping previous token"
                )
                return token

            state["token"] = new_token
            state["exp"] = _token_expiry(new_token)
            if token:
                logger.info("Internal MCP bearer token refreshed")
            return new_token

    return _provider


def _build_internal_mcp_config() -> Dict[str, Any] | None:
    """Build ai_agent_core internal MCP config from one env contract.

    URL shape follows the gateway routing contract: endpoint_id is formatted
    into the path by ai_agent_core. Tenant part_id is added there from request
    context as the Part-Id header.

    ``token_provider`` lets ai_agent_core refresh the bearer token per request;
    ``bearer_token`` is the initial value, kept so consumers that only read the
    static field keep working.
    """
    base_url = _internal_mcp_base_url()
    if not base_url:
        return None

    provider = _make_internal_mcp_token_provider(base_url)
    return {
        "base_url": f"{base_url}/{{endpoint_id}}/mcp",
        "bearer_token": provider(),  # primes the cache; back-compat for readers
        "token_provider": provider,
        "headers": _build_internal_mcp_headers(),
    }


# ---------------------------------------------------------------------------
# Setting builder
# ---------------------------------------------------------------------------


_SETTINGS_FILE = Path(__file__).parent / "settings.yaml"


@functools.lru_cache(maxsize=1)
def _load_setting_spec() -> Dict[str, Any]:
    """Load and cache the env -> setting map from settings.yaml.

    The spec is static for the process lifetime; only the resolved values
    (read from os.environ on every call) change, so caching the parse is safe.
    """
    try:
        with open(_SETTINGS_FILE) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load setting manifest {_SETTINGS_FILE}: {e}")
        raise
    return data.get("settings", {}) or {}


def _coerce(value: Any, type_name: Any, key: str) -> Any:
    """Apply the optional ``type:`` from the spec to a resolved value."""
    if not type_name:
        return value
    try:
        if type_name == "int":
            return int(value)
        if type_name == "float":
            return float(value)
        if type_name == "bool":
            return str(value).strip().lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        logger.warning(
            "settings.yaml: '%s' -> cannot coerce %r to %s; using raw value",
            key,
            value,
            type_name,
        )
        return value
    logger.warning(
        "settings.yaml: '%s' -> unknown type %r; using raw value", key, type_name
    )
    return value


def _resolve_setting(key: str, spec: Dict[str, Any]) -> Any:
    """Resolve one setting from os.environ per its spec entry.

    The first env var with a non-empty value wins; otherwise ``default``;
    otherwise None.
    """
    env_names = spec.get("env", key)
    if isinstance(env_names, str):
        env_names = [env_names]

    value = None
    for name in env_names:
        raw = os.environ.get(name)
        if raw not in (None, ""):
            value = raw
            break

    if value is None:
        value = spec.get("default")
    if value is None:
        return None

    return _coerce(value, spec.get("type"), key)


def build_setting_from_env() -> Dict[str, Any]:
    """Build the gateway setting dict from environment variables.

    The env -> setting map is declared in ``settings.yaml``; values are read
    from ``os.environ`` (populated from .env by the launchers). Two derived
    keys are computed here because they need code rather than data:
    ``internal_mcp`` and ``functs_on_local``.

    Shared by the single-process and multi-worker (factory) launch paths so both
    see an identical configuration.
    """
    spec = _load_setting_spec()
    setting: Dict[str, Any] = {
        key: _resolve_setting(key, entry or {}) for key, entry in spec.items()
    }

    # Initialize GatewayConfig early so the Cognito IdP client is available
    # when _build_internal_mcp_config() resolves the bearer token below.
    # GatewayConfig.initialize() is idempotent — create_app() will no-op.
    _gw_logger = logging.getLogger("silvaengine_gateway")
    GatewayConfig.initialize(_gw_logger, setting)

    # Internal MCP server — forwarded to ai_agent_core_engine.handlers.config:Config
    # Used by _get_agent() to fetch agent MCP server config at runtime.
    setting["internal_mcp"] = _build_internal_mcp_config()

    # Build functs_on_local from route manifest (data-driven, no hard-coded module names)
    # Each module with a config_class and graphql routes gets a local-function entry.
    # Also, modules with websocket routes that need streaming (e.g. ai_agent_core_engine)
    # get their auxiliary streaming functions (send_data_to_stream,
    # async_insert_update_tool_call) added so the invoker resolves them locally.
    manifest_for_functs = load_route_manifest(GatewayConfig)
    functs_on_local: Dict[str, Any] = {}
    for mod in manifest_for_functs:
        if mod.config_class:
            for route in mod.routes:
                if route.handler_type == "graphql" and route.dispatch:
                    # Invoker calls target class methods, not wrapper names.
                    # e.g. "/{endpoint_id}/knowledge_graph_graphql" -> "knowledge_graph_graphql"
                    func_name = route.path.rstrip("/").rsplit("/", 1)[-1]
                    functs_on_local[func_name] = {
                        "module_name": mod.package,
                        "class_name": _module_invoker_class_name(mod),
                    }

                # WebSocket routes that need streaming require their
                # auxiliary functions resolved locally by the invoker.
                if route.handler_type == "websocket":
                    class_name = _module_invoker_class_name(mod)
                    # Core streaming bridge functions that must be local
                    for aux_fn in (
                        "send_data_to_stream",
                        "async_insert_update_tool_call",
                    ):
                        functs_on_local.setdefault(
                            aux_fn,
                            {
                                "module_name": mod.package,
                                "class_name": class_name,
                            },
                        )

    # Allow env var overrides / additions
    functs_on_local.update(json.loads(os.getenv("FUNCTS_ON_LOCAL_OVERRIDES", "{}")))
    setting["functs_on_local"] = functs_on_local

    return setting
