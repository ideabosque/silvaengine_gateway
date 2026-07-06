# -*- coding: utf-8 -*-
"""Tests for WebSocket route registration and auth behavior."""

import logging
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from silvaengine_gateway.config import GatewayConfig
from silvaengine_gateway.router_builder import (
    ModuleSpec,
    RouteSpec,
    build_router_from_manifest,
)
from silvaengine_gateway.websocket_manager import ConnectionManager
from silvaengine_gateway.auth.jwt_local import create_local_jwt


@pytest.fixture(autouse=True)
def setup_gateway_config():
    """Initialize GatewayConfig for all tests in this module."""
    GatewayConfig.initialize(
        logging.getLogger("test"),
        {
            "jwt_secret_key": "test-secret-key",
            "jwt_algorithm": "HS256",
            "auth_provider": "local",
            "access_token_exp": 60,
        },
    )


def test_route_spec_allows_websocket_without_dispatch():
    """WebSocket route validates without dispatch."""
    route = RouteSpec(
        path="/{endpoint_id}/ws",
        handler_type="websocket",
        auth=True,
    )
    assert route.handler_type == "websocket"
    assert route.dispatch is None


def test_route_spec_allows_websocket_with_dispatch():
    """WebSocket route can optionally have dispatch."""
    route = RouteSpec(
        path="/{endpoint_id}/ws",
        handler_type="websocket",
        dispatch="some.module:dispatch_fn",
        auth=True,
    )
    assert route.dispatch == "some.module:dispatch_fn"


def test_route_spec_requires_dispatch_for_http_handlers():
    """GraphQL/rest/background still require dispatch."""
    with pytest.raises(Exception):
        RouteSpec(
            path="/{endpoint_id}/graphql",
            handler_type="graphql",
            auth=True,
        )

    with pytest.raises(Exception):
        RouteSpec(
            path="/{endpoint_id}/rest",
            handler_type="rest",
            auth=True,
        )

    with pytest.raises(Exception):
        RouteSpec(
            path="/{endpoint_id}/background",
            handler_type="background",
            auth=True,
        )


def test_websocket_missing_token():
    """Connection closes with 4001 when no token is provided."""
    cm = ConnectionManager()

    def fake_dispatch(**params):
        return {"status": "ok"}

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch=None,
                auth=True,
            ),
        ],
    )

    app = FastAPI()
    router = build_router_from_manifest(
        [module],
        connection_manager=cm,
        auth_provider="local",
    )
    app.include_router(router)

    with TestClient(app) as client:
        # Connect without token — should be rejected
        with pytest.raises(Exception):
            with client.websocket_connect("/gpt/ws?part_id=tenant1"):
                pass


def test_websocket_missing_part_id():
    """Connection closes with 4002 when no part_id is provided."""
    cm = ConnectionManager()

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch=None,
                auth=True,
            ),
        ],
    )

    app = FastAPI()
    router = build_router_from_manifest(
        [module],
        connection_manager=cm,
        auth_provider="local",
    )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(f"/gpt/ws?token={token}"):
                pass


def test_websocket_context_injection():
    """Dispatch receives partition, user, and connection context."""
    captured_params = {}

    def fake_dispatch(**params):
        captured_params.update(params)
        return {"result": "done"}

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch="test_module.main:fake_dispatch",
                auth=True,
            ),
        ],
    )

    cm = ConnectionManager()
    app = FastAPI()

    # Patch resolve_dispatch to return our fake dispatch instead of importing
    with patch(
        "silvaengine_gateway.router_builder.resolve_dispatch",
        return_value=fake_dispatch,
    ):
        router = build_router_from_manifest(
            [module],
            connection_manager=cm,
            auth_provider="local",
        )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/gpt/ws?token={token}&part_id=tenant1"
        ) as ws:
            ack = ws.receive_json()
            assert ack["type"] == "connection_ack"
            connection_id = ack["connection_id"]
            assert connection_id is not None

            # cm should have registered the connection
            assert cm.connection_count == 1

            ws.send_json({"action": "ask_model", "arguments": {"prompt": "hi"}})

            # Receive the dispatch result
            response = ws.receive_json()
            assert response["result"] == "done"

    # After disconnect, the connection should be unregistered
    assert cm.connection_count == 0

    # Verify context injection
    assert captured_params.get("endpoint_id") == "gpt"
    assert captured_params.get("part_id") == "tenant1"
    assert captured_params.get("partition_key") == "gpt#tenant1"
    assert captured_params.get("connection_id") == connection_id
    assert "user" in captured_params.get("context", {})
    assert "partition_key" in captured_params.get("context", {})
    assert "connection_id" in captured_params.get("context", {})

def test_websocket_uses_part_id_header_for_partition():
    """Dispatch receives partition context from a real Part-Id header."""
    captured_params = {}

    def fake_dispatch(**params):
        captured_params.update(params)
        return {"result": "done"}

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch="test_module.main:fake_dispatch",
                auth=True,
            ),
        ],
    )

    cm = ConnectionManager()
    app = FastAPI()
    with patch(
        "silvaengine_gateway.router_builder.resolve_dispatch",
        return_value=fake_dispatch,
    ):
        router = build_router_from_manifest(
            [module],
            connection_manager=cm,
            auth_provider="local",
        )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/gpt/ws?token={token}",
            headers={"Part-Id": "tenant-from-header"},
        ) as ws:
            ws.receive_json()
            ws.send_json({"action": "ask_model"})
            assert ws.receive_json()["result"] == "done"

    assert captured_params["part_id"] == "tenant-from-header"
    assert captured_params["partition_key"] == "gpt#tenant-from-header"


def test_websocket_part_id_mismatch_rejected():
    """Connection closes with 4002 when partition sources disagree."""
    cm = ConnectionManager()
    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch=None,
                auth=True,
            ),
        ],
    )

    app = FastAPI()
    router = build_router_from_manifest(
        [module],
        connection_manager=cm,
        auth_provider="local",
    )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                f"/gpt/ws?token={token}&part_id=query-tenant",
                headers={"Part-Id": "header-tenant"},
            ):
                pass


def test_websocket_drains_stream_chunks_before_dispatch_result():
    """Streaming chunks, including the end marker, arrive before result."""
    cm = ConnectionManager()

    def fake_dispatch(**params):
        connection_id = params["connection_id"]
        assert cm.send_to_connection(
            connection_id,
            {
                "chunk_delta": "hello ",
                "data_format": "text",
                "is_message_end": False,
                "index": 0,
            },
        ) is True
        assert cm.send_to_connection(
            connection_id,
            {
                "chunk_delta": "world",
                "data_format": "text",
                "is_message_end": True,
                "index": 1,
            },
        ) is True
        return {"status": "ok"}

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch="test_module.main:fake_dispatch",
                auth=True,
            ),
        ],
    )

    @asynccontextmanager
    async def lifespan(app):
        import asyncio

        cm.set_event_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(lifespan=lifespan)
    with patch(
        "silvaengine_gateway.router_builder.resolve_dispatch",
        return_value=fake_dispatch,
    ):
        router = build_router_from_manifest(
            [module],
            connection_manager=cm,
            auth_provider="local",
        )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/gpt/ws?token={token}&part_id=tenant1"
        ) as ws:
            assert ws.receive_json()["type"] == "connection_ack"
            ws.send_json({"action": "ask_model", "arguments": {"prompt": "hi"}})

            first = ws.receive_json()
            second = ws.receive_json()
            result = ws.receive_json()

    assert first["chunk_delta"] == "hello "
    assert first["is_message_end"] is False
    assert second["chunk_delta"] == "world"
    assert second["is_message_end"] is True
    assert result == {"status": "ok"}


def test_websocket_ping_returns_pong_without_dispatch():
    """A ping message is answered with a pong at the gateway, no model dispatch."""
    dispatch_calls = []

    def fake_dispatch(**params):
        dispatch_calls.append(params)
        return {"result": "done"}

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch="test_module.main:fake_dispatch",
                auth=True,
            ),
        ],
    )

    cm = ConnectionManager()
    app = FastAPI()
    with patch(
        "silvaengine_gateway.router_builder.resolve_dispatch",
        return_value=fake_dispatch,
    ):
        router = build_router_from_manifest(
            [module],
            connection_manager=cm,
            auth_provider="local",
        )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/gpt/ws?token={token}&part_id=tenant1"
        ) as ws:
            assert ws.receive_json()["type"] == "connection_ack"

            # action-style ping, with a correlation id echoed back
            ws.send_json({"action": "ping", "id": "abc"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"
            assert pong["id"] == "abc"
            assert pong["message"].startswith("Hello at")
            assert "connection_id" in pong

            # type-style ping is also accepted
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

    # ping must never trigger a model dispatch
    assert dispatch_calls == []


def test_websocket_ping_invokes_module_graphql_ping():
    """When ping_dispatch is set, ping runs the module's GraphQL `{ ping }`."""
    ask_calls = []
    graphql_calls = []

    def fake_ask(**params):
        ask_calls.append(params)
        return {"result": "done"}

    def fake_graphql(**params):
        graphql_calls.append(params)
        return {"data": {"ping": "Hello at 12:00:00!!"}}

    def resolve(path):
        return fake_graphql if path.endswith("dispatch_graphql") else fake_ask

    module = ModuleSpec(
        name="test_module",
        package="test_module",
        transport="hybrid",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/ws",
                handler_type="websocket",
                dispatch="test_module.main:dispatch_ask_model",
                ping_dispatch="test_module.main:dispatch_graphql",
                auth=True,
            ),
        ],
    )

    cm = ConnectionManager()
    app = FastAPI()
    with patch(
        "silvaengine_gateway.router_builder.resolve_dispatch",
        side_effect=resolve,
    ):
        router = build_router_from_manifest(
            [module],
            connection_manager=cm,
            auth_provider="local",
        )
    app.include_router(router)

    token = create_local_jwt({"username": "testuser"})

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/gpt/ws?token={token}&part_id=tenant1"
        ) as ws:
            assert ws.receive_json()["type"] == "connection_ack"

            ws.send_json({"action": "ping", "id": "p1"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"
            assert pong["id"] == "p1"
            # Full GraphQL envelope is returned, plus the extracted ping message
            assert pong["result"] == {"data": {"ping": "Hello at 12:00:00!!"}}
            assert pong["message"] == "Hello at 12:00:00!!"

    # ping ran the GraphQL dispatch, never the model dispatch
    assert len(graphql_calls) == 1
    assert graphql_calls[0]["query"] == "{ ping }"
    assert graphql_calls[0]["partition_key"] == "gpt#tenant1"
    assert ask_calls == []

