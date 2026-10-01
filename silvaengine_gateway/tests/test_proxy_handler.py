# -*- coding: utf-8 -*-
"""Tests for handler_type: "proxy" — reverse-proxying to another SilvaEngine
Gateway instance. See docs/gateway_proxy_plan.md for the design.
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from silvaengine_gateway.router_builder import (
    ModuleSpec,
    RouteSpec,
    _resolve_setting_ref,
    _resolve_timeout,
    build_router_from_manifest,
    validate_manifest,
)


def _make_proxy_module(proxy_targets, proxy_timeout=5.0, auth=False):
    return ModuleSpec(
        name="remote_gateway_proxy",
        package="remote_gateway_proxy",
        transport="rest",
        proxy_targets=proxy_targets,
        proxy_timeout=proxy_timeout,
        routes=[
            RouteSpec(
                path="/{endpoint_id}/remote/{target_id}/{proxy_path:path}",
                handler_type="proxy",
                methods=["GET", "POST"],
                auth=auth,
            )
        ],
    )


def _build_app(modules, http_client, proxy_allowlist=None, setting=None):
    app = FastAPI()
    router = build_router_from_manifest(
        modules,
        http_client=http_client,
        proxy_allowlist=proxy_allowlist,
        setting=setting or {},
    )
    app.include_router(router)
    return app


def _capture_transport(captured, response_body=b'{"ok": true}', status_code=200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["content"] = request.content
        return httpx.Response(
            status_code, content=response_body, headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_route_spec_allows_proxy_without_dispatch():
    route = RouteSpec(
        path="/{endpoint_id}/remote/{target_id}/{proxy_path:path}",
        handler_type="proxy",
        auth=True,
    )
    assert route.handler_type == "proxy"
    assert route.dispatch is None


def test_validate_manifest_warns_on_proxy_without_targets():
    modules = [
        ModuleSpec(
            name="mod1",
            package="mod1",
            routes=[
                RouteSpec(
                    path="/{endpoint_id}/remote/{target_id}/{proxy_path:path}",
                    handler_type="proxy",
                    methods=["GET"],
                )
            ],
        )
    ]
    warnings = validate_manifest(modules)
    assert any("handler_type='proxy'" in w for w in warnings)


def test_validate_manifest_no_warning_when_targets_configured():
    modules = [_make_proxy_module({"us": "https://us.example.com"})]
    warnings = validate_manifest(modules)
    assert not any("handler_type='proxy'" in w for w in warnings)


# ---------------------------------------------------------------------------
# _resolve_setting_ref / _resolve_timeout
# ---------------------------------------------------------------------------


def test_resolve_setting_ref_passthrough_for_literal():
    assert _resolve_setting_ref("https://us.example.com", {}) == "https://us.example.com"


def test_resolve_setting_ref_resolves_reference():
    assert _resolve_setting_ref("{setting:KEY}", {"KEY": "value"}) == "value"


def test_resolve_setting_ref_unset_reference_is_none():
    assert _resolve_setting_ref("{setting:KEY}", {}) is None


def test_resolve_timeout_setting_reference():
    assert _resolve_timeout("{setting:GATEWAY_PROXY_TIMEOUT}", {"GATEWAY_PROXY_TIMEOUT": 45}) == 45.0


def test_resolve_timeout_falls_back_to_default_on_bad_value():
    assert _resolve_timeout("{setting:MISSING}", {}, default=30.0) == 30.0


# ---------------------------------------------------------------------------
# End-to-end forwarding
# ---------------------------------------------------------------------------


def test_proxy_forwards_method_path_headers_body_and_query():
    captured = {}
    client = httpx.AsyncClient(transport=_capture_transport(captured))
    module = _make_proxy_module({"us": "https://us.example.com"})
    app = _build_app([module], client, proxy_allowlist=["us.example.com"])

    with TestClient(app) as tc:
        resp = tc.post(
            "/acme/remote/us/knowledge_graph_graphql?foo=bar",
            json={"query": "{ ping }"},
            headers={"Authorization": "Bearer xyz", "Part-Id": "tenant1"},
        )

    assert resp.status_code == 200
    assert captured["method"] == "POST"
    assert captured["url"] == "https://us.example.com/acme/knowledge_graph_graphql?foo=bar"
    # Authorization/Part-Id pass through untouched; the local gateway's own
    # Host header is not forwarded (httpx sets its own for the upstream URL).
    assert captured["headers"]["authorization"] == "Bearer xyz"
    assert captured["headers"]["part-id"] == "tenant1"
    assert captured["headers"]["host"] == "us.example.com"
    assert captured["content"] == b'{"query":"{ ping }"}'


def test_proxy_resolves_target_via_setting_indirection():
    captured = {}
    client = httpx.AsyncClient(transport=_capture_transport(captured))
    module = _make_proxy_module(
        {"us": "{setting:REMOTE_GATEWAY_US_URL}"},
        proxy_timeout="{setting:GATEWAY_PROXY_TIMEOUT}",
    )
    setting = {"REMOTE_GATEWAY_US_URL": "https://us.example.com", "GATEWAY_PROXY_TIMEOUT": 15}
    app = _build_app([module], client, proxy_allowlist=["us.example.com"], setting=setting)

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/ping")

    assert resp.status_code == 200
    assert captured["url"] == "https://us.example.com/acme/ping"


def test_proxy_unknown_target_returns_404():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    module = _make_proxy_module({"us": "https://us.example.com"})
    app = _build_app([module], client, proxy_allowlist=["us.example.com"])

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/eu/some/path")

    assert resp.status_code == 404


def test_proxy_allowlist_rejects_unlisted_host_without_calling_upstream():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    module = _make_proxy_module({"us": "https://us.example.com"})
    # Allowlist only permits eu.example.com — the "us" target's host is rejected.
    app = _build_app([module], client, proxy_allowlist=["eu.example.com"])

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 502
    assert called["n"] == 0


def test_proxy_timeout_returns_504():
    def handler(request):
        raise httpx.ConnectTimeout("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    module = _make_proxy_module({"us": "https://us.example.com"})
    app = _build_app([module], client, proxy_allowlist=["us.example.com"])

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 504


def test_proxy_connection_error_returns_502():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    module = _make_proxy_module({"us": "https://us.example.com"})
    app = _build_app([module], client, proxy_allowlist=["us.example.com"])

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 502


def test_proxy_route_skipped_without_http_client():
    """No shared http_client configured: the route is never registered, so
    a request 404s with FastAPI's default "not found" — not the handler's
    own "Unknown proxy target" 404.
    """
    module = _make_proxy_module({"us": "https://us.example.com"})
    app = _build_app([module], http_client=None, proxy_allowlist=["us.example.com"])

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 404
    assert resp.json().get("detail") == "Not Found"


def test_proxy_resolves_whole_map_via_setting_indirection():
    """Recommended form: proxy_targets is a single "{setting:KEY}" reference
    pointing at GATEWAY_PROXY_TARGETS, a JSON-encoded target_id -> base_url
    map — the dynamic config path (see docs/gateway_proxy_plan.md).
    """
    captured = {}
    client = httpx.AsyncClient(transport=_capture_transport(captured))
    module = ModuleSpec(
        name="remote_gateway_proxy",
        package="remote_gateway_proxy",
        transport="rest",
        proxy_targets="{setting:GATEWAY_PROXY_TARGETS}",
        proxy_timeout=5.0,
        routes=[
            RouteSpec(
                path="/{endpoint_id}/remote/{target_id}/{proxy_path:path}",
                handler_type="proxy",
                methods=["GET", "POST"],
            )
        ],
    )
    setting = {"GATEWAY_PROXY_TARGETS": {"us": "https://us.example.com"}}
    app = _build_app([module], client, proxy_allowlist=["us.example.com"], setting=setting)

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/ping")

    assert resp.status_code == 200
    assert captured["url"] == "https://us.example.com/acme/ping"


def test_proxy_whole_map_reference_resolving_to_non_dict_is_treated_as_empty():
    """A misconfigured GATEWAY_PROXY_TARGETS (not a JSON object) must not
    crash router construction — the route is simply skipped.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    module = ModuleSpec(
        name="remote_gateway_proxy",
        package="remote_gateway_proxy",
        transport="rest",
        proxy_targets="{setting:GATEWAY_PROXY_TARGETS}",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/remote/{target_id}/{proxy_path:path}",
                handler_type="proxy",
                methods=["GET"],
            )
        ],
    )
    setting = {"GATEWAY_PROXY_TARGETS": "not-a-dict"}
    app = _build_app([module], client, proxy_allowlist=[], setting=setting)

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 404


def test_proxy_route_skipped_when_targets_resolve_empty():
    """A proxy_targets map whose only entry is an unset {setting:KEY}
    reference resolves to nothing usable, so the route is skipped entirely.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    module = _make_proxy_module({"us": "{setting:UNSET_KEY}"})
    app = _build_app([module], client, proxy_allowlist=[], setting={})

    with TestClient(app) as tc:
        resp = tc.get("/acme/remote/us/path")

    assert resp.status_code == 404
    assert resp.json().get("detail") == "Not Found"
