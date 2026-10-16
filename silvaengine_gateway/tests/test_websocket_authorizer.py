# -*- coding: utf-8 -*-
"""Tests for the generic "authorizer" WebSocket handshake provider
(auth/websocket.py) — a Lambda-style authorizer class configured by route
options and ws_* gateway settings."""

import logging
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from silvaengine_gateway.auth.websocket import resolve_authorizer_options
from silvaengine_gateway.config import GatewayConfig
from silvaengine_gateway.router_builder import (
    ModuleSpec,
    RouteSpec,
    build_router_from_manifest,
)
from silvaengine_gateway.websocket_manager import ConnectionManager

GOOD_TOKEN = "good-token"
AUTHORIZER_PATH = "fake_authz.mod:FakeAuthorizer"


class FakeAuthorizer:
    """Reads the token only from the Authorization header, like
    perm_engine.PermAuthorizer."""

    seen = []

    def __init__(self, logger, **setting):
        self.setting = setting

    def verify_permission(self, event, context):
        FakeAuthorizer.seen.append((event, self.setting))
        if (event.get("headers") or {}).get("authorization") != f"Bearer {GOOD_TOKEN}":
            raise PermissionError("bad token")
        event["requestContext"]["authorizer"] = {
            "user_id": "u1",
            "is_admin": False,
            "tenant_id": "tenant-a",
        }
        return event

    def check(self, event, context):
        return self.verify_permission(event, context)


@pytest.fixture(autouse=True)
def fake_authorizer_module(monkeypatch):
    GatewayConfig.initialize(
        logging.getLogger("test"),
        {"jwt_secret_key": "x", "jwt_algorithm": "HS256", "auth_provider": "local"},
    )
    pkg = types.ModuleType("fake_authz")
    mod = types.ModuleType("fake_authz.mod")
    mod.FakeAuthorizer = FakeAuthorizer
    pkg.mod = mod
    monkeypatch.setitem(sys.modules, "fake_authz", pkg)
    monkeypatch.setitem(sys.modules, "fake_authz.mod", mod)
    FakeAuthorizer.seen = []


def _client(route_authorizer=None, setting=None):
    module = ModuleSpec(
        name="ws_mod",
        package="ws_mod",
        transport="websocket",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                auth_provider="authorizer",
                authorizer=route_authorizer,
            )
        ],
    )
    app = FastAPI()
    app.include_router(
        build_router_from_manifest(
            [module],
            connection_manager=ConnectionManager(),
            auth_provider="local",
            setting=setting if setting is not None else {"jwt_secret": "s"},
        )
    )
    return TestClient(app)


def _connect_code(client, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    return exc.value.code


def test_route_config_accepts_valid_token_and_defaults_part_id_to_claim():
    with _client({"class": AUTHORIZER_PATH}) as client:
        with client.websocket_connect(f"/e1/ws?token={GOOD_TOKEN}") as ws:
            assert ws.receive_json()["type"] == "connection_ack"
    event, setting = FakeAuthorizer.seen[0]
    assert event["headers"]["authorization"] == f"Bearer {GOOD_TOKEN}"
    assert "token" not in event["queryStringParameters"]
    assert setting == {"jwt_secret": "s"}  # gateway setting reaches the class


def test_settings_only_config():
    setting = {"ws_authorizer": AUTHORIZER_PATH, "ws_authorizer_method": "check"}
    with _client(None, setting) as client:
        with client.websocket_connect(f"/e1/ws?token={GOOD_TOKEN}") as ws:
            assert ws.receive_json()["type"] == "connection_ack"


def test_rejects_invalid_and_missing_token():
    with _client({"class": AUTHORIZER_PATH}) as client:
        assert _connect_code(client, "/e1/ws?token=bad") == 4001
        assert _connect_code(client, "/e1/ws") == 4001


def test_unconfigured_authorizer_fails_closed():
    with _client(None, {}) as client:
        assert _connect_code(client, f"/e1/ws?token={GOOD_TOKEN}") == 1011


def test_part_id_bound_to_partition_claim():
    with _client({"class": AUTHORIZER_PATH}) as client:
        assert (
            _connect_code(client, f"/e1/ws?token={GOOD_TOKEN}&part_id=tenant-b")
            == 4002
        )
        with client.websocket_connect(
            f"/e1/ws?token={GOOD_TOKEN}&part_id=tenant-a"
        ) as ws:
            assert ws.receive_json()["type"] == "connection_ack"


def test_partition_binding_can_be_disabled():
    with _client({"class": AUTHORIZER_PATH, "partition_claim": "none"}) as client:
        with client.websocket_connect(
            f"/e1/ws?token={GOOD_TOKEN}&part_id=tenant-b"
        ) as ws:
            assert ws.receive_json()["type"] == "connection_ack"
        # Without binding the client must name the partition itself.
        assert _connect_code(client, f"/e1/ws?token={GOOD_TOKEN}") == 4002


def test_route_options_override_settings():
    options = resolve_authorizer_options(
        {"class": "a.b:C", "method": "m"},
        {"ws_authorizer": "x.y:Z", "ws_authorizer_method": "n",
         "ws_partition_claim": "merchant_id"},
    )
    assert options["class"] == "a.b:C"
    assert options["method"] == "m"
    assert options["partition_claim"] == "merchant_id"
    assert options["admin_claim"] == "is_admin"
