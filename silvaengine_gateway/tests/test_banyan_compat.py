# -*- coding: utf-8 -*-
"""Tests for the gateway's built-in Banyan hosting (P0 merge from api-runtime).

Covers, unit-by-unit and full-stack:

- BanyanPathNormalizer (middleware.path_normalizer) — /{stage}/{area}
  prefix stripping, pass-through rules, raw_path rewrite, and WebSocket
  scope handling.
- se-configdata setting provider (setting_builder._load_se_configdata_setting)
  — env-gated no-op, DDB-wins overlay, emergency SETTING_OVERRIDE_* escape
  hatch, empty-record ValueError, boto3 resource query shape, and
  httpx-only loopback rewriting.
- framework pool bootstrap (app._bootstrap_framework_pools) — no-op
  without ``plugins``; direct registration via the framework
  ``ConnectionPoolManager`` (``register_connection_type`` +
  ``create_pools_from_config``) against a faked module — never imports
  the real silvaengine_connections.
- BanyanAuthorizerBridge (auth.middleware) — claims promotion, 401/403
  mapping, OPTIONS short-circuit, non-Banyan pass-through, fail-closed
  on ImportError, and body replay to the downstream handler.
- FlexJWTMiddleware — hand-off when the bridge marked the request
  (user claims or the anonymous marker), rejection otherwise.
- router_builder — the ``part_id`` header fallback and the
  _inject_user_claims anti-spoofing promotion (Lambda parity).
- create_app wiring — middleware mount order and a full
  Normalizer → Bridge → FlexJWT → route request round-trip.

perm_engine is NOT importable in the gateway dev environment; the
authorizer is always faked through the module-level _perm_components
seam (auth.middleware). DynamoDB is always faked — no network or AWS
calls in tests.
"""

from __future__ import print_function

__author__ = "silvaengine"

import asyncio
import copy
import json
import logging
import os
import sys
import types

import boto3
import pytest
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from silvaengine_gateway import setting_builder
from silvaengine_gateway.app import _bootstrap_framework_pools, create_app
from silvaengine_gateway.auth import middleware as auth_middleware
from silvaengine_gateway.auth.middleware import (
    AUTH_CHECKED_ATTR,
    BanyanAuthorizerBridge,
    FlexJWTMiddleware,
    get_current_user,
)
from silvaengine_gateway.config import GatewayConfig
from silvaengine_gateway.manifest import load_route_manifest
from silvaengine_gateway.middleware.path_normalizer import BanyanPathNormalizer
from silvaengine_gateway.router_builder import (
    _extract_partition_key,
    _inject_user_claims,
    build_router_from_manifest,
)
from silvaengine_gateway.setting_builder import _load_se_configdata_setting
import silvaengine_gateway.router_builder as router_builder_module

_TEST_SETTING = {
    "auth_provider": "local",
    "jwt_secret_key": "test-secret-key",
    "admin_username": "admin",
    "admin_password": "admin123",
    "port": "8000",
}


@pytest.fixture(autouse=True)
def _clean_banyan_env(monkeypatch):
    """Keep Banyan env gates OFF unless a test sets them explicitly."""
    for var in (
        "SETTING_SOURCE",
        "ENDPOINT_ID",
        "ADAPTER_STAGE",
        "ADAPTER_AREA",
        "BANYAN_LOOPBACK_BASE_URL",
        "SE_CONFIGDATA_TABLE",
        "SE_CONFIGDATA_ENDPOINT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith("SETTING_OVERRIDE_"):
            monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# BanyanPathNormalizer
# ---------------------------------------------------------------------------


def _make_normalizer_app():
    async def echo_path(request):
        return JSONResponse(
            {"path": request.url.path, "params": dict(request.path_params)}
        )

    app = Starlette(
        routes=[
            Route("/health", echo_path),
            Route("/banyan/{rest:path}", echo_path),
            Route("/{fallback:path}", echo_path),
        ]
    )
    app.add_middleware(BanyanPathNormalizer, stage="beta", area="core")
    return app


def test_normalizer_strips_stage_area_prefix():
    client = TestClient(_make_normalizer_app())
    r = client.get("/beta/core/banyan/user_engine_graphql")
    assert r.status_code == 200
    assert r.json()["path"] == "/banyan/user_engine_graphql"
    assert r.json()["params"]["rest"] == "user_engine_graphql"


def test_normalizer_passthrough_short_and_wrong_prefix_paths():
    client = TestClient(_make_normalizer_app())
    # Two segments (native contract) and health route: untouched
    assert client.get("/health").json()["path"] == "/health"
    assert client.get("/banyan/some_function").json()["path"] == "/banyan/some_function"
    # Wrong stage / wrong area: pass through unchanged (fail-loud 404 shape)
    assert (
        client.get("/prod/core/banyan/user_engine_graphql").json()["path"]
        == "/prod/core/banyan/user_engine_graphql"
    )
    assert (
        client.get("/beta/wrong/banyan/user_engine_graphql").json()["path"]
        == "/beta/wrong/banyan/user_engine_graphql"
    )


def test_normalizer_updates_scope_in_place():
    captured = {}

    async def capture_app(scope, receive, send):
        captured.update(scope)

    middleware = BanyanPathNormalizer(capture_app, stage="beta", area="core")
    scope = {
        "type": "http",
        "path": "/beta/core/banyan/graphql",
        "raw_path": b"/beta/core/banyan/graphql",
    }
    asyncio.run(middleware(scope, None, None))
    assert captured["path"] == "/banyan/graphql"
    assert captured["raw_path"] == b"/banyan/graphql"

    # Missing raw_path must not explode (scope without it passes cleanly)
    captured.clear()
    scope = {"type": "http", "path": "/beta/core/banyan/graphql"}
    asyncio.run(middleware(scope, None, None))
    assert captured["path"] == "/banyan/graphql"
    assert "raw_path" not in captured


def test_normalizer_rewrites_websocket_scope():
    async def ws_echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(websocket.url.path)
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/banyan/ws/{name}", ws_echo)])
    app.add_middleware(BanyanPathNormalizer, stage="beta", area="core")
    with TestClient(app).websocket_connect("/beta/core/banyan/ws/echo") as ws:
        assert ws.receive_text() == "/banyan/ws/echo"


# ---------------------------------------------------------------------------
# se-configdata setting provider (setting_builder)
# ---------------------------------------------------------------------------


def test_provider_noop_without_setting_source(monkeypatch):
    monkeypatch.delenv("SETTING_SOURCE", raising=False)

    def _fail(_setting):
        pytest.fail("DDB must not be read without SETTING_SOURCE=se-configdata")

    monkeypatch.setattr(setting_builder, "_read_configdata_record", _fail)
    base = {"jwt_secret": "env-value", "auth_provider": "local"}
    out = _load_se_configdata_setting(base)
    assert out is base  # same object, zero behavior change


def test_setting_id_from_env(monkeypatch):
    assert setting_builder._setting_id() == "beta_core_banyan"
    monkeypatch.setenv("ADAPTER_STAGE", "prod")
    monkeypatch.setenv("ADAPTER_AREA", "core")
    monkeypatch.setenv("ENDPOINT_ID", "banyan2")
    assert setting_builder._setting_id() == "prod_core_banyan2"


def test_provider_ddb_keys_win_over_env(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    record = {
        "jwt_secret": "ddb-secret",
        "region_name": "ddb-region",
    }
    monkeypatch.setattr(
        setting_builder,
        "_read_configdata_record",
        lambda _s: copy.deepcopy(record),
    )
    base = {"jwt_secret": "env-secret", "region_name": "env-region", "extra": 1}
    out = _load_se_configdata_setting(base)
    assert out["jwt_secret"] == "ddb-secret"  # DDB wins
    assert out["region_name"] == "ddb-region"
    assert out["extra"] == 1  # env-only keys preserved
    assert base["jwt_secret"] == "env-secret"  # input dict not mutated


def test_provider_env_override_beats_ddb(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    monkeypatch.setenv("SETTING_OVERRIDE_jwt_secret", "override-secret")
    monkeypatch.setattr(
        setting_builder,
        "_read_configdata_record",
        lambda _s: {"jwt_secret": "ddb-secret"},
    )
    out = _load_se_configdata_setting({"jwt_secret": "env-secret"})
    assert out["jwt_secret"] == "override-secret"


class _FakeTable:
    def __init__(self, items):
        self._items = items
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": self._items}


def _install_fake_boto3(monkeypatch, items):
    table = _FakeTable(items)

    class _Resource:
        @staticmethod
        def Table(name):
            table.name = name
            return table

    captured = {}

    def _fake_resource(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return _Resource()

    monkeypatch.setattr(boto3, "resource", _fake_resource)
    return table, captured


def test_provider_reads_record_via_boto3_resource(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    items = [
        {"setting_id": "beta_core_banyan", "variable": "jwt_secret", "value": "s"},
        {
            "variable": "jwt_config",
            "value": {"issuer": "banyan", "audience": "banyan-graphql"},
        },
    ]
    table, captured = _install_fake_boto3(monkeypatch, items)
    out = _load_se_configdata_setting({"jwt_secret": "env", "region_name": "us-west-2"})
    assert out["jwt_secret"] == "s"
    assert out["jwt_config"]["issuer"] == "banyan"
    assert captured["name"] == "dynamodb"
    assert captured["region_name"] == "us-west-2"
    kwargs = table.queries[0]
    assert kwargs["KeyConditionExpression"] == "setting_id = :sid"
    assert kwargs["ExpressionAttributeValues"][":sid"] == "beta_core_banyan"


def test_provider_endpoint_url_passthrough(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    monkeypatch.setenv("SE_CONFIGDATA_ENDPOINT_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("SE_CONFIGDATA_TABLE", "local-configdata")
    items = [{"variable": "jwt_secret", "value": "s"}]
    _install_fake_boto3(monkeypatch, items)
    out = _load_se_configdata_setting({})
    assert out["jwt_secret"] == "s"


def test_provider_empty_record_raises(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    _install_fake_boto3(monkeypatch, [])
    with pytest.raises(ValueError, match="Cannot find values"):
        _load_se_configdata_setting({})


def test_provider_loopback_rewrites_httpx_pools_only(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    monkeypatch.setenv(
        "BANYAN_LOOPBACK_BASE_URL", "http://127.0.0.1:8000/beta/core/banyan"
    )
    record = {
        "plugins": [
            {
                "config": {
                    "postgres_main": {
                        "settings": {"host": "postgres", "password": "pg-pass"}
                    },
                    "httpx_agent": {
                        "settings": {"base_url": "https://cloud.example/beta/core/banyan"}
                    },
                    "email_code_redis": {"settings": {"host": "redis"}},
                }
            }
        ]
    }
    monkeypatch.setattr(
        setting_builder,
        "_read_configdata_record",
        lambda _s: copy.deepcopy(record),
    )
    out = _load_se_configdata_setting({})
    pools = out["plugins"][0]["config"]
    assert (
        pools["httpx_agent"]["settings"]["base_url"]
        == "http://127.0.0.1:8000/beta/core/banyan"
    )
    assert pools["postgres_main"]["settings"]["host"] == "postgres"
    assert pools["postgres_main"]["settings"]["password"] == "pg-pass"
    assert pools["email_code_redis"]["settings"]["host"] == "redis"


def test_provider_no_loopback_leaves_base_urls(monkeypatch):
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    monkeypatch.delenv("BANYAN_LOOPBACK_BASE_URL", raising=False)
    record = {
        "plugins": [
            {
                "config": {
                    "httpx_agent": {"settings": {"base_url": "https://cloud.example"}}
                }
            }
        ]
    }
    monkeypatch.setattr(
        setting_builder,
        "_read_configdata_record",
        lambda _s: copy.deepcopy(record),
    )
    out = _load_se_configdata_setting({})
    assert (
        out["plugins"][0]["config"]["httpx_agent"]["settings"]["base_url"]
        == "https://cloud.example"
    )


# ---------------------------------------------------------------------------
# framework pool bootstrap (app)
# ---------------------------------------------------------------------------


def test_bootstrap_noop_without_plugins():
    assert (
        _bootstrap_framework_pools({"auth_provider": "local"}, logging.getLogger("t"))
        is False
    )


def _fake_connections_modules(monkeypatch, calls):
    """Install fakes for silvaengine_connections and its connections.* submodules.

    The fake ConnectionPoolManager records register_connection_type /
    create_pools_from_config calls without touching any real driver.
    """

    class _FakeManager:
        def get_connection_types(self):
            return list(calls["types"])

        def register_connection_type(self, type_name, pool_class, connection_class):
            calls["types"].append(type_name)
            calls["registered"].append((type_name, pool_class, connection_class))

        def create_pools_from_config(self, pools_config):
            calls["pools_config"] = pools_config
            calls["created"] = sorted(pools_config.keys())
            return calls["created"]

    fake_pkg = types.ModuleType("silvaengine_connections")
    fake_pkg.ConnectionPoolManager = _FakeManager

    fake_conns = types.ModuleType("silvaengine_connections.connections")
    driver_classes = {
        "postgresql": ("PostgreSQLConnection", "PostgreSQLConnectionPool"),
        "httpx": ("HTTPXConnection", "HTTPXConnectionPool"),
        "neo4j": ("Neo4jConnection", "Neo4jConnectionPool"),
        "boto3": ("Boto3Connection", "Boto3ConnectionPool"),
    }
    for driver in ("postgresql", "httpx", "neo4j", "boto3"):
        sub = types.ModuleType(f"silvaengine_connections.connections.{driver}")
        conn_cls, pool_cls = driver_classes[driver]
        setattr(sub, conn_cls, f"Fake{driver}Conn")
        setattr(sub, pool_cls, f"Fake{driver}Pool")
        monkeypatch.setitem(
            sys.modules, f"silvaengine_connections.connections.{driver}", sub
        )
    fake_conns.postgresql = sys.modules["silvaengine_connections.connections.postgresql"]
    fake_conns.httpx = sys.modules["silvaengine_connections.connections.httpx"]
    fake_conns.neo4j = sys.modules["silvaengine_connections.connections.neo4j"]
    fake_conns.boto3 = sys.modules["silvaengine_connections.connections.boto3"]
    fake_pkg.connections = fake_conns

    monkeypatch.setitem(sys.modules, "silvaengine_connections", fake_pkg)
    monkeypatch.setitem(
        sys.modules, "silvaengine_connections.connections", fake_conns
    )


def test_bootstrap_creates_pools_via_pool_manager(monkeypatch):
    calls = {"types": [], "registered": [], "created": [], "pools_config": None}
    _fake_connections_modules(monkeypatch, calls)

    setting = {
        "plugins": [
            {
                "config": {
                    "postgres_main": {"settings": {"host": "postgres"}},
                    "httpx_agent": {
                        "settings": {"base_url": "https://cloud.example"}
                    },
                }
            }
        ]
    }
    assert _bootstrap_framework_pools(setting, logging.getLogger("t")) is True
    # both connection types registered on demand, postgresql first seen
    assert sorted(calls["types"]) == ["httpx", "postgresql"]
    # pools config carries the derived type per pool
    assert calls["pools_config"]["postgres_main"]["type"] == "postgresql"
    assert calls["pools_config"]["httpx_agent"]["type"] == "httpx"
    assert calls["created"] == ["httpx_agent", "postgres_main"]


def test_bootstrap_explicit_type_wins_and_unknown_skipped(monkeypatch):
    calls = {"types": [], "registered": [], "created": [], "pools_config": None}
    _fake_connections_modules(monkeypatch, calls)

    setting = {
        # flat shape (no "config" wrapper) + explicit type + unrecognised name
        "plugins": [
            {"pg_custom": {"type": "postgresql", "settings": {}}},
            {"mystery_bus": {"settings": {}}},
        ]
    }
    assert _bootstrap_framework_pools(setting, logging.getLogger("t")) is True
    # explicit type respected; unrecognised pool skipped, not passed through
    assert list(calls["pools_config"].keys()) == ["pg_custom"]
    assert calls["pools_config"]["pg_custom"]["type"] == "postgresql"


def test_bootstrap_empty_bundle_is_noop(monkeypatch):
    calls = {"types": [], "registered": [], "created": [], "pools_config": None}
    _fake_connections_modules(monkeypatch, calls)

    setting = {"plugins": [{"config": {}}]}
    assert _bootstrap_framework_pools(setting, logging.getLogger("t")) is False
    assert calls["pools_config"] is None


# ---------------------------------------------------------------------------
# BanyanAuthorizerBridge
# ---------------------------------------------------------------------------


class _FakeAuthError(PermissionError):
    pass


class _FakeAuthorizer:
    outcome = "allow"
    claims = None
    last_event = None
    instances = []

    def __init__(self, logger, **setting):
        self.logger = logger
        self.setting = setting
        type(self).instances.append(self)

    def verify_permission(self, event, context):
        type(self).last_event = event
        outcome = type(self).outcome
        if outcome == "auth_error":
            raise _FakeAuthError("invalid token")
        if outcome == "perm_error":
            raise PermissionError("no role")
        claims = type(self).claims
        if claims:
            event["requestContext"]["authorizer"] = claims
        return event


@pytest.fixture
def fake_perm(monkeypatch):
    monkeypatch.setattr(
        auth_middleware, "_perm_components", (_FakeAuthorizer, _FakeAuthError)
    )
    _FakeAuthorizer.outcome = "allow"
    _FakeAuthorizer.claims = None
    _FakeAuthorizer.last_event = None
    _FakeAuthorizer.instances = []
    return _FakeAuthorizer


def _make_bridge_app(endpoint_id="banyan"):
    async def echo(request):
        try:
            body = await request.json()
        except Exception:
            body = None
        return JSONResponse(
            {
                "user": getattr(request.state, "user", None),
                "marked": bool(getattr(request.state, AUTH_CHECKED_ATTR, False)),
                "body": body,
            }
        )

    app = Starlette(
        routes=[
            Route("/banyan/{rest:path}", echo, methods=["GET", "POST"]),
            Route("/{fallback:path}", echo, methods=["GET", "POST"]),
        ]
    )
    app.add_middleware(
        BanyanAuthorizerBridge,
        endpoint_id=endpoint_id,
        setting={"jwt_secret": "bridge-test-secret"},
    )
    return app


_CLAIMS = {
    "user_id": "u-1",
    "sub": "u-1",
    "is_admin": False,
    "tenant_id": "t-1",
    "roles": [{"role_id": "r", "name": "user", "tenant_id": "t-1"}],
}


def test_bridge_authenticates_banyan_path(fake_perm):
    fake_perm.claims = dict(_CLAIMS)
    client = TestClient(_make_bridge_app())
    r = client.post(
        "/banyan/user_engine_graphql",
        headers={
            "Authorization": "Bearer tok",
            "part_id": "p-1",
            "x-api-key": "key-1",
        },
        json={"query": "{ me { user_id } }"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["user"] == _CLAIMS
    assert data["marked"] is True
    assert data["body"] == {"query": "{ me { user_id } }"}  # body replayed downstream

    event = fake_perm.last_event
    assert event["headers"]["authorization"] == "Bearer tok"
    assert event["headers"]["x-api-key"] == "key-1"
    assert json.loads(event["body"])["query"] == "{ me { user_id } }"

    authorizer = fake_perm.instances[-1]
    assert authorizer.setting == {"jwt_secret": "bridge-test-secret"}


def test_bridge_passes_non_banyan_path_through(fake_perm):
    client = TestClient(_make_bridge_app())
    r = client.post("/other_engine/fn", json={"x": 1})
    assert r.status_code == 200
    assert fake_perm.last_event is None  # authorizer never ran
    assert r.json()["marked"] is False


def test_bridge_authentication_error_maps_401(fake_perm):
    fake_perm.outcome = "auth_error"
    client = TestClient(_make_bridge_app())
    r = client.post("/banyan/user_engine_graphql", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_bridge_permission_error_maps_403(fake_perm):
    fake_perm.outcome = "perm_error"
    client = TestClient(_make_bridge_app())
    r = client.post("/banyan/user_engine_graphql", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403


def test_bridge_anonymous_allow_sets_marker_without_user(fake_perm):
    client = TestClient(_make_bridge_app())
    r = client.post("/banyan/user_engine_graphql", json={"query": "mutation login"})
    assert r.status_code == 200
    assert r.json()["user"] is None
    assert r.json()["marked"] is True


def test_bridge_options_short_circuits_authorizer(fake_perm):
    client = TestClient(_make_bridge_app())
    r = client.options("/banyan/user_engine_graphql")
    assert r.status_code != 401 and r.status_code != 403
    assert r.status_code != 500
    assert fake_perm.last_event is None  # authorizer never ran


def test_bridge_fails_closed_on_import_error(monkeypatch):
    def _raise():
        raise ImportError("perm_engine not installed")

    monkeypatch.setattr(auth_middleware, "_perm_components", None)
    monkeypatch.setattr(auth_middleware, "_load_perm_components", _raise)
    client = TestClient(_make_bridge_app())
    r = client.post("/banyan/user_engine_graphql", json={"query": "mutation login"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Banyan authorizer unavailable"


# ---------------------------------------------------------------------------
# FlexJWTMiddleware hand-off
# ---------------------------------------------------------------------------


def _make_flex_app():
    async def echo(request):
        return JSONResponse({"user": getattr(request.state, "user", None)})

    class Marker(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/marked-anon":
                setattr(request.state, AUTH_CHECKED_ATTR, True)
            if request.url.path == "/marked-user":
                request.state.user = {"sub": "svc-token"}
            return await call_next(request)

    app = Starlette(routes=[Route("/{p:path}", echo)])
    app.add_middleware(FlexJWTMiddleware)
    app.add_middleware(Marker)  # added later → runs first (outer)
    return app


def test_flexjwt_hands_off_on_banyan_marker():
    client = TestClient(_make_flex_app())
    r = client.get("/marked-anon")  # no Authorization header at all
    assert r.status_code == 200


def test_flexjwt_hands_off_on_state_user():
    client = TestClient(_make_flex_app())
    r = client.get("/marked-user")
    assert r.status_code == 200
    assert r.json()["user"] == {"sub": "svc-token"}


def test_flexjwt_still_rejects_unmarked_requests():
    client = TestClient(_make_flex_app())
    assert client.get("/plain").status_code == 401
    assert client.get("/plain", headers={"Authorization": "junk"}).status_code == 401


# ---------------------------------------------------------------------------
# router_builder — partition key + claims injection
# ---------------------------------------------------------------------------


def _make_scope_request(headers=None, path_params=None, method="POST"):
    scope = {
        "type": "http",
        "method": method,
        "path": "/banyan/user_engine_graphql",
        "raw_path": b"/banyan/user_engine_graphql",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
        "query_string": b"",
        "path_params": path_params or {},
    }
    return Request(scope)


def test_partition_key_reads_part_id_header():
    req = _make_scope_request(
        headers={"part_id": "p-under"}, path_params={"endpoint_id": "banyan"}
    )
    assert _extract_partition_key(req) == ("banyan#p-under", "banyan", "p-under")


def test_partition_key_header_precedence():
    # Part-Id (hyphen) wins over part_id (underscore) when both present
    req = _make_scope_request(
        headers={"Part-Id": "hyphen", "part_id": "underscore"},
        path_params={"endpoint_id": "banyan"},
    )
    assert _extract_partition_key(req)[2] == "hyphen"
    # Case-insensitive hyphen variants
    req = _make_scope_request(
        headers={"Part-ID": "cap"}, path_params={"endpoint_id": "banyan"}
    )
    assert _extract_partition_key(req)[2] == "cap"


def test_partition_key_path_param_fallback():
    req = _make_scope_request(path_params={"endpoint_id": "e", "part_id": "pp"})
    assert _extract_partition_key(req) == ("e#pp", "e", "pp")


def test_partition_key_missing_raises_400():
    req = _make_scope_request(path_params={"endpoint_id": "e"})
    with pytest.raises(HTTPException) as exc_info:
        _extract_partition_key(req)
    assert exc_info.value.status_code == 400


def test_inject_user_claims_promotes_and_overrides_spoofed_values():
    params = {"context": {"user_id": "spoofed", "is_admin": True}}
    user = {"user_id": "u-1", "is_admin": False, "merchant_id": None, "roles": ["r1"]}
    _inject_user_claims(params, user)
    ctx = params["context"]
    assert ctx["user"] is user
    assert ctx["user_id"] == "u-1"  # claims overwrite spoofed body value
    assert ctx["is_admin"] is False
    assert ctx["roles"] == ["r1"]
    assert "merchant_id" not in ctx  # None values are not promoted


def test_inject_user_claims_creates_context():
    params = {}
    _inject_user_claims(params, {"user_id": "u-1"})
    assert params["context"]["user_id"] == "u-1"
    assert params["context"]["user"]["user_id"] == "u-1"


def test_inject_user_claims_non_dict_legacy_behaviour():
    params = {}
    _inject_user_claims(params, "opaque-user")
    assert params["context"]["user"] == "opaque-user"
    assert "user_id" not in params["context"]


# ---------------------------------------------------------------------------
# create_app wiring + full-stack request
# ---------------------------------------------------------------------------


def _middleware_classes(app):
    return [m.cls for m in app.user_middleware]


def test_create_app_mounts_banyan_middlewares_in_order(monkeypatch):
    monkeypatch.setenv("ENDPOINT_ID", "banyan")
    app = create_app(dict(_TEST_SETTING))
    classes = _middleware_classes(app)
    assert BanyanAuthorizerBridge in classes
    assert BanyanPathNormalizer in classes
    # user_middleware is outermost-first: CORS → Normalizer → Bridge → FlexJWT
    assert classes[0] is CORSMiddleware
    assert classes.index(BanyanPathNormalizer) < classes.index(BanyanAuthorizerBridge)
    assert classes.index(BanyanAuthorizerBridge) < classes.index(FlexJWTMiddleware)


def test_create_app_without_endpoint_id_adds_no_banyan_middleware(monkeypatch):
    monkeypatch.delenv("ENDPOINT_ID", raising=False)
    app = create_app(dict(_TEST_SETTING))
    classes = _middleware_classes(app)
    assert BanyanAuthorizerBridge not in classes
    assert BanyanPathNormalizer not in classes


def test_full_stack_banyan_request_round_trip(fake_perm, monkeypatch):
    monkeypatch.setenv("ENDPOINT_ID", "banyan")
    monkeypatch.setenv("ADAPTER_STAGE", "beta")
    monkeypatch.setenv("ADAPTER_AREA", "core")
    fake_perm.claims = dict(_CLAIMS)
    app = create_app(dict(_TEST_SETTING))

    from fastapi import Request as FastAPIRequest
    from fastapi.responses import JSONResponse as FastAPIJSONResponse

    @app.post("/banyan/echo_route")
    async def _echo(request: FastAPIRequest):
        try:
            body = await request.json()
        except Exception:
            body = None
        return FastAPIJSONResponse(
            {
                "user": getattr(request.state, "user", None),
                "marked": bool(getattr(request.state, AUTH_CHECKED_ATTR, False)),
                "body": body,
            }
        )

    client = TestClient(app)
    r = client.post(
        "/beta/core/banyan/echo_route",  # frontend contract path
        headers={
            "Authorization": "Bearer not-a-local-jwt",  # only Banyan can verify this
            "part_id": "p-1",
        },
        json={"query": "{ me { user_id } }"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["user"] == _CLAIMS
    assert data["marked"] is True
    assert data["body"] == {"query": "{ me { user_id } }"}
    # The authorizer saw the frontend-style headers and GraphQL body
    assert fake_perm.last_event["headers"]["part_id"] == "p-1"
    assert json.loads(fake_perm.last_event["body"])["query"] == "{ me { user_id } }"


def test_full_stack_without_bridge_rejects_banyan_token(monkeypatch):
    monkeypatch.delenv("ENDPOINT_ID", raising=False)
    app = create_app(dict(_TEST_SETTING))
    client = TestClient(app)
    r = client.post(
        "/banyan/user_engine_graphql",
        headers={"Authorization": "Bearer not-a-local-jwt", "part_id": "p-1"},
        json={"query": "{ me { user_id } }"},
    )
    # Without the bridge, FlexJWT is the gate — and it cannot verify
    # Banyan tokens, so the request must be rejected.
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# P0 regression guards — real manifest → real router → full-stack anonymous
#
# The 12 Banyan engine yamls declare ``auth: false`` so the bridge (not the
# gateway's local-JWT route dependency) is the only gate on Banyan routes.
# Without that, a route-level ``Depends(get_current_user)`` would reject the
# anonymous (bridge-marked) login/registerUser calls with 401 before any
# engine dispatch runs. These tests walk the REAL loading paths — packaged
# routes.yaml via ``!include``, ``build_router_from_manifest`` with the real
# ``get_current_user``, and ``create_app``'s full middleware chain.
# ---------------------------------------------------------------------------

#: The 12 Banyan engine modules (user-confirmed P0 fix scope).
_BANYAN_ENGINES = (
    "agent_engine",
    "capability_engine",
    "knowledge_engine",
    "llm_engine",
    "memory_engine",
    "merchant_engine",
    "monitor_engine",
    "orchestration_engine",
    "perm_engine",
    "prompt_engine",
    "setting_engine",
    "user_engine",
)


def _load_packaged_manifest(monkeypatch):
    """Load the packaged routes.yaml (master manifest with !include children).

    Guarded against env leakage: an exported GATEWAY_ROUTES_CONFIG_PATH or a
    leftover class-level routes_config_path from another test must not divert
    the manifest this suite asserts against.
    """
    monkeypatch.delenv("GATEWAY_ROUTES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(GatewayConfig, "routes_config_path", None)
    return load_route_manifest(GatewayConfig)


def test_manifest_banyan_modules_are_anonymous(monkeypatch):
    modules = _load_packaged_manifest(monkeypatch)
    by_name = {m.name: m for m in modules}

    # All 12 Banyan engine modules are present in the packaged manifest.
    missing = [n for n in _BANYAN_ENGINES if n not in by_name]
    assert not missing, f"Banyan modules missing from routes.yaml: {missing}"

    # Every route of every Banyan engine is anonymous at the gateway route
    # level — the BanyanAuthorizerBridge is the single gate (P0 fix).
    for name in _BANYAN_ENGINES:
        module = by_name[name]
        assert module.routes, f"{name} declares no routes"
        for route in module.routes:
            assert route.auth is False, (
                f"{name} route {route.path} must be auth:false "
                "(Banyan auth lives in the bridge, not the route dependency)"
            )

    # Boundary: non-Banyan modules keep gateway-side route auth (the fix must
    # never leak into them).
    kge = by_name["knowledge_graph_engine"]
    assert kge.routes
    assert all(r.auth is True for r in kge.routes)


def test_router_builder_omits_auth_dependency_for_banyan_routes(monkeypatch):
    modules = _load_packaged_manifest(monkeypatch)

    # Engines are not importable in the dev environment; stand in a fake
    # dispatch so every manifest route actually registers. The patch targets
    # the router_builder module attribute (what build_router_from_manifest
    # looks up at call time), matching the existing websocket suite pattern.
    def _fake_dispatch(**params):
        return {"dispatched": True}

    monkeypatch.setattr(
        router_builder_module, "resolve_dispatch", lambda spec: _fake_dispatch
    )

    router = build_router_from_manifest(
        modules,
        auth_dependency=get_current_user,
    )

    banyan_paths = {
        route.path
        for name in _BANYAN_ENGINES
        for route in next(m for m in modules if m.name == name).routes
    }
    api_routes = {r.path: r for r in router.routes if isinstance(r, APIRoute)}

    # Banyan routes carry no route-level auth dependency → anonymous
    # (bridge-marked) requests are not short-circuited by get_current_user.
    for path in banyan_paths:
        assert path in api_routes, f"Banyan route not registered: {path}"
        assert api_routes[path].dependencies == [], (
            f"{path} must not carry Depends(get_current_user) "
            "(auth: false in the manifest must reach the built route)"
        )

    # Boundary: an auth:true manifest route still gets the dependency wired.
    kge_route = api_routes["/{endpoint_id}/knowledge_graph_graphql"]
    assert len(kge_route.dependencies) == 1


def test_full_stack_anonymous_banyan_route_reaches_dispatch(
    fake_perm, monkeypatch
):
    """Anonymous login reaches a REAL manifest route end-to-end (P0 guard).

    Full chain: BanyanPathNormalizer → BanyanAuthorizerBridge (anonymous
    allow) → FlexJWT hand-off → real ``/{endpoint_id}/user_engine_graphql``
    route built from the packaged manifest. With auth:false the anonymous
    request must reach the dispatch; reverting the yaml to auth:true would
    bring back Depends(get_current_user) and fail this test with 401.
    """
    monkeypatch.setenv("ENDPOINT_ID", "banyan")
    monkeypatch.setenv("ADAPTER_STAGE", "beta")
    monkeypatch.setenv("ADAPTER_AREA", "core")
    monkeypatch.delenv("GATEWAY_ROUTES_CONFIG_PATH", raising=False)

    captured = []

    def _fake_dispatch(**params):
        captured.append(params)
        # No "body" key on purpose — _make_sync_handler treats dicts that
        # carry one as Lambda-proxy envelopes.
        return {"dispatched": True}

    # Fake every manifest dispatch (engine packages are absent in dev).
    # app.py's own binding of resolve_dispatch is NOT affected, so startup
    # hooks / config-class injection keep failing ImportError and skipping,
    # exactly as in the other create_app tests.
    monkeypatch.setattr(
        router_builder_module, "resolve_dispatch", lambda spec: _fake_dispatch
    )

    app = create_app(dict(_TEST_SETTING))
    client = TestClient(app)

    # Anonymous login — no Authorization header at all.
    r = client.post(
        "/beta/core/banyan/user_engine_graphql",  # frontend contract path
        headers={"part_id": "nestaging"},
        json={
            "query": "mutation login($input: LoginInput!) { login(input: $input) }",
            "variables": {"input": {"account": "admin@banyanos.dev"}},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] is True  # the engine dispatch really ran

    params = captured[-1]
    assert params["endpoint_id"] == "banyan"
    assert params["part_id"] == "nestaging"
    assert params["partition_key"] == "banyan#nestaging"
    # Anonymous: the bridge marked the request but promoted no user claims.
    assert "user_id" not in params.get("context", {})
    assert "user" not in params.get("context", {})


def test_full_stack_authenticated_banyan_route_promotes_claims(
    fake_perm, monkeypatch
):
    """Authenticated Banyan calls on a REAL route keep Lambda claim parity.

    auth:false removes the route gate but NOT the claim promotion: the bridge
    stores authorizer claims on request.state.user and _inject_user_claims
    lifts user_id / is_admin / roles / tenant_id into dispatch context.
    """
    monkeypatch.setenv("ENDPOINT_ID", "banyan")
    monkeypatch.setenv("ADAPTER_STAGE", "beta")
    monkeypatch.setenv("ADAPTER_AREA", "core")
    monkeypatch.delenv("GATEWAY_ROUTES_CONFIG_PATH", raising=False)

    captured = []

    def _fake_dispatch(**params):
        captured.append(params)
        return {"dispatched": True}

    monkeypatch.setattr(
        router_builder_module, "resolve_dispatch", lambda spec: _fake_dispatch
    )

    fake_perm.claims = dict(_CLAIMS)
    app = create_app(dict(_TEST_SETTING))
    client = TestClient(app)

    r = client.post(
        "/beta/core/banyan/user_engine_graphql",
        headers={
            "Authorization": "Bearer banyan-jwt",  # verified by the (fake) bridge
            "part_id": "nestaging",
        },
        json={"query": "{ me { user_id } }"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] is True

    context = captured[-1]["context"]
    assert context["user"] == _CLAIMS
    assert context["user_id"] == "u-1"
    assert context["tenant_id"] == "t-1"
    assert context["is_admin"] is False
    assert context["roles"] == _CLAIMS["roles"]