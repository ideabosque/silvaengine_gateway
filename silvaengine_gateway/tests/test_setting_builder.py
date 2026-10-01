# -*- coding: utf-8 -*-
"""Tests for setting_builder's env -> setting coercion, including the
"json" type used by GATEWAY_PROXY_TARGETS (see docs/gateway_proxy_plan.md)
to make the gateway proxy's remote targets a single dynamic env var instead
of one settings.yaml entry per target.
"""

from silvaengine_gateway.setting_builder import _coerce, _resolve_setting


def test_coerce_json_parses_object():
    assert _coerce(
        '{"us": "https://us.example.com"}', "json", "GATEWAY_PROXY_TARGETS"
    ) == {"us": "https://us.example.com"}


def test_coerce_json_invalid_falls_back_to_raw_value():
    assert _coerce("not-json", "json", "GATEWAY_PROXY_TARGETS") == "not-json"


def test_resolve_setting_json_env_var(monkeypatch):
    monkeypatch.setenv("GATEWAY_PROXY_TARGETS", '{"eu": "https://eu.example.com"}')
    spec = {"env": "GATEWAY_PROXY_TARGETS", "type": "json"}
    assert _resolve_setting("GATEWAY_PROXY_TARGETS", spec) == {
        "eu": "https://eu.example.com"
    }


def test_resolve_setting_json_default(monkeypatch):
    monkeypatch.delenv("GATEWAY_PROXY_TARGETS", raising=False)
    spec = {"env": "GATEWAY_PROXY_TARGETS", "type": "json", "default": "{}"}
    assert _resolve_setting("GATEWAY_PROXY_TARGETS", spec) == {}
