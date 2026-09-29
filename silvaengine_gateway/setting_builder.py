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

# ---------------------------------------------------------------------------
# functs_on_local helpers
# ---------------------------------------------------------------------------


def _module_invoker_class_name(module: ModuleSpec) -> str:
    """Return the class name used by downstream Invoker mappings."""
    configured = os.getenv(f"FUNCTS_{module.name.upper()}_CLASS")
    if configured:
        return configured

    if module.invoker_class_name:
        return module.invoker_class_name

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


# ---------------------------------------------------------------------------
# Banyan hosting — se-configdata setting provider (DDB-backed source)
# ---------------------------------------------------------------------------
# Banyan's iron rule: configuration has a single source — the DynamoDB
# ``se-configdata`` table. Rows live under the partition key
# ``setting_id = {stage}_{area}_{endpoint_id}`` (e.g. ``beta_core_banyan``)
# with a ``variable`` sort key and a typed ``value`` attribute, one row per
# variable (``plugins``, ``jwt_secret``, ``jwt_config``, ``region_name``,
# ``email_code_redis``, ...). The Lambda chain reads them via
# ``silvaengine_dynamodb_base.models.config.ConfigModel``; this provider
# reads the same rows with the boto3 resource API (whose item
# deserialization matches ``ConfigModel.boto3_items_to_dict_list``) so the
# gateway and Lambda interpret records identically.
#
# Merge policy (decision #4 of the merge plan, user-approved):
#
# 1. env-derived keys (settings.yaml spec) form the base;
# 2. se-configdata keys OVERLAY them — DDB wins, matching Lambda behavior
#    (the record is the unique source; env only bootstraps infrastructure
#    such as the DDB endpoint/region and stage/area/endpoint_id);
# 3. ``SETTING_OVERRIDE_<KEY>`` env vars are an audited emergency escape
#    hatch that beats the DDB record (logged loudly when used).
#
# Loopback rewrite: when ``BANYAN_LOOPBACK_BASE_URL`` is set, every httpx
# pool in the ``plugins`` config gets its ``settings.base_url`` rewritten
# to that value so cross-engine calls stay inside the container (decision
# #7). Outbound LLM provider calls are unaffected — llm_engine's providers
# pass absolute URLs, which httpx resolves regardless of ``base_url``.
#
# Fail-fast: with ``SETTING_SOURCE=se-configdata`` a missing or unreadable
# record raises at startup — mirroring ``ConfigModel.find`` — instead of
# letting engines run half-configured.

#: Setting source selector: "env" (default, no-op) | "se-configdata".
SETTING_SOURCE_ENV = "SETTING_SOURCE"
SETTING_SOURCE_DDB = "se-configdata"

#: Bootstrap variables (read from the environment, NOT the setting dict —
#: the record itself cannot describe how to reach it).
STAGE_ENV = "ADAPTER_STAGE"
AREA_ENV = "ADAPTER_AREA"
ENDPOINT_ID_ENV = "ENDPOINT_ID"
TABLE_ENV = "SE_CONFIGDATA_TABLE"
ENDPOINT_URL_ENV = "SE_CONFIGDATA_ENDPOINT_URL"

#: Emergency escape hatch prefix: SETTING_OVERRIDE_jwt_secret=... wins
#: over the DDB record for that single key.
OVERRIDE_PREFIX = "SETTING_OVERRIDE_"

#: When set (e.g. "http://127.0.0.1:8000/beta/core/banyan"), httpx pool
#: base_urls in the plugins config are rewritten to this value so
#: cross-engine calls loop back through this gateway instead of the cloud
#: API Gateway. Empty → record values pass through untouched.
LOOPBACK_BASE_URL_ENV = "BANYAN_LOOPBACK_BASE_URL"


def _load_se_configdata_setting(setting: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay the se-configdata record onto ``setting``.

    See the Banyan hosting section comment above for the merge policy.
    Returns the setting dict unchanged when ``SETTING_SOURCE`` is not
    ``se-configdata`` — existing deployments see zero behavior change.
    """
    source = os.getenv(SETTING_SOURCE_ENV, "").strip()
    if source != SETTING_SOURCE_DDB:
        return setting

    record = _read_configdata_record(setting)
    _rewrite_httpx_base_urls(record)

    merged = dict(setting)
    merged.update(record)  # DDB keys win over env-derived keys

    merged = _apply_env_overrides(merged)

    logger.info(
        "se-configdata setting loaded: setting_id=%s variables=%d "
        "(env base=%d, merged=%d)",
        _setting_id(),
        len(record),
        len(setting),
        len(merged),
    )
    return merged


def _setting_id() -> str:
    """Build ``{stage}_{area}_{endpoint_id}`` from bootstrap env vars."""
    stage = os.getenv(STAGE_ENV, "beta").strip() or "beta"
    area = os.getenv(AREA_ENV, "core").strip() or "core"
    endpoint_id = os.getenv(ENDPOINT_ID_ENV, "banyan").strip() or "banyan"
    return f"{stage}_{area}_{endpoint_id}"


def _read_configdata_record(setting: Dict[str, Any]) -> Dict[str, Any]:
    """Query se-configdata rows for the setting_id and flatten them.

    Uses the boto3 resource API: items come back deserialized to native
    types, matching ``ConfigModel.boto3_items_to_dict_list`` semantics.
    """
    import boto3

    table_name = os.getenv(TABLE_ENV, "se-configdata").strip() or "se-configdata"
    endpoint_url = os.getenv(ENDPOINT_URL_ENV, "").strip() or None

    region = (
        setting.get("region_name")
        or os.getenv("region_name")
        or os.getenv("AWS_REGION")
    )
    access_key = setting.get("aws_access_key_id") or os.getenv("aws_access_key_id")
    secret_key = (
        setting.get("aws_secret_access_key")
        or os.getenv("aws_secret_access_key")
    )

    resource_kwargs: Dict[str, Any] = {"region_name": region}
    if endpoint_url:
        # DynamoDB Local (or an in-VPC endpoint) for fully-offline runs.
        resource_kwargs["endpoint_url"] = endpoint_url
    if access_key and secret_key:
        resource_kwargs["aws_access_key_id"] = access_key
        resource_kwargs["aws_secret_access_key"] = secret_key

    table = boto3.resource("dynamodb", **resource_kwargs).Table(table_name)

    record: Dict[str, Any] = {}
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "setting_id = :sid",
        "ExpressionAttributeValues": {":sid": _setting_id()},
    }
    try:
        while True:
            response = table.query(**kwargs)
            for item in response.get("Items", []):
                variable = item.get("variable")
                if variable:
                    record[str(variable)] = item.get("value")
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    except Exception as e:
        raise ValueError(
            f"Failed to read se-configdata table '{table_name}' "
            f"(setting_id={_setting_id()}): {e}"
        ) from e

    if not record:
        # Mirror ConfigModel.find: an empty record is a configuration
        # error, not an empty config.
        raise ValueError(
            f"Cannot find values with the setting_id ({_setting_id()}) "
            f"in table '{table_name}'."
        )

    return record


def _rewrite_httpx_base_urls(record: Dict[str, Any]) -> None:
    """Point cross-engine httpx pools at the gateway loopback (in place).

    se-configdata's httpx pool base_urls point at the cloud API Gateway so
    Lambda-resident engines can reach each other. Inside the container
    those calls must loop back through this gateway (which authenticates
    the engines' short-lived service tokens via the authorizer bridge).
    Only httpx pools are touched; postgres/redis/neo4j pool configs pass
    through untouched, and llm_engine's provider calls use absolute URLs
    so they never consult base_url.
    """
    loopback = os.getenv(LOOPBACK_BASE_URL_ENV, "").strip()
    if not loopback:
        return

    plugins = record.get("plugins")
    if not isinstance(plugins, list):
        return

    rewritten = 0
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        config = entry.get("config")
        if not isinstance(config, dict):
            continue
        for pool_name, pool_cfg in config.items():
            if not isinstance(pool_name, str) or not pool_name.startswith(
                "httpx"
            ):
                continue
            if not isinstance(pool_cfg, dict):
                continue
            pool_settings = pool_cfg.get("settings")
            if isinstance(pool_settings, dict):
                pool_settings["base_url"] = loopback
                rewritten += 1

    if rewritten:
        logger.info(
            "Rewrote %d httpx pool base_url(s) to Banyan loopback (%s)",
            rewritten,
            loopback,
        )


def _apply_env_overrides(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Apply SETTING_OVERRIDE_<KEY> emergency overrides (last word)."""
    for name, value in os.environ.items():
        if not name.startswith(OVERRIDE_PREFIX):
            continue
        key = name[len(OVERRIDE_PREFIX):].strip()
        if not key:
            continue
        merged[key] = value
        logger.warning(
            "SETTING_OVERRIDE_%s applied — env override beats se-configdata",
            key,
        )
    return merged


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

    # Banyan: overlay the se-configdata record (DDB) when SETTING_SOURCE is
    # enabled — DDB keys win (config unique-source iron rule), with
    # SETTING_OVERRIDE_* as the audited emergency escape hatch. Inert
    # without SETTING_SOURCE=se-configdata.
    setting = _load_se_configdata_setting(setting)

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
