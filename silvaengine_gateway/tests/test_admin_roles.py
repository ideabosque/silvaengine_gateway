"""Local-admin role configuration and consistent claims; no external services."""

import logging

import pytest
from fastapi.security import OAuth2PasswordRequestForm

from silvaengine_gateway.auth.jwt_local import (
    get_or_create_admin_token,
    verify_local_jwt,
)
from silvaengine_gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def isolated_admin_config(monkeypatch):
    for name in ("ADMIN_ROLE", "ADMIN_ROLES", "ADMIN_STATIC_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    GatewayConfig.reset()
    yield
    GatewayConfig.reset()


def configure(**overrides):
    setting = {
        "auth_provider": "local",
        "jwt_secret_key": "test-admin-role-secret",
        "admin_username": "local-admin",
        "admin_password": "fake-password",
        "admin_static_token": "",
        "local_user_file": "",
    }
    setting.update(overrides)
    GatewayConfig.initialize(logging.getLogger("test-admin-roles"), setting)


def claims():
    return verify_local_jwt(get_or_create_admin_token())


def test_default_admin_login_has_matching_admin_claims():
    from silvaengine_gateway.routes.auth import login

    configure()
    token = login(
        OAuth2PasswordRequestForm(username="local-admin", password="fake-password")
    )["access_token"]
    payload = verify_local_jwt(token)
    assert payload["user_id"] == "local-admin"
    assert payload["role"] == "admin"
    assert payload["roles"] == ["admin"]


@pytest.mark.parametrize(
    "value",
    [
        " policy-operator, qa-lead,policy-operator ",
        '["policy-operator", "qa-lead", "policy-operator"]',
    ],
)
def test_roles_from_environment_csv_or_json(monkeypatch, value):
    monkeypatch.setenv("ADMIN_ROLES", value)
    configure()
    assert claims()["roles"] == ["policy-operator", "qa-lead"]
    assert claims()["role"] == "policy-operator"


def test_primary_role_from_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_ROLE", "policy-operator")
    configure()
    assert claims()["roles"] == ["policy-operator"]
    assert claims()["role"] == "policy-operator"


def test_settings_override_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_ROLE", "qa-lead")
    monkeypatch.setenv("ADMIN_ROLES", "qa-lead")
    configure(admin_role="policy-operator", admin_roles=["admin", "policy-operator"])
    assert claims()["role"] == "policy-operator"
    assert claims()["roles"] == ["admin", "policy-operator"]


@pytest.mark.parametrize("roles", [[], {}, '["admin", 4]', "[bad-json", ", ,"])
def test_malformed_roles_fail_instead_of_granting_default_admin(roles):
    with pytest.raises(ValueError, match="ADMIN_ROLES"):
        configure(admin_roles=roles)


def test_conflicting_primary_and_roles_fail():
    with pytest.raises(ValueError, match="ADMIN_ROLE must also appear"):
        configure(admin_role="admin", admin_roles="qa-lead")


def test_reconfiguration_does_not_reuse_stale_cached_token():
    configure(admin_role="qa-lead")
    old = get_or_create_admin_token()
    configure(admin_role="admin")
    new = get_or_create_admin_token()
    assert old != new
    assert claims()["roles"] == ["admin"]


def test_external_static_token_is_unchanged():
    configure(admin_static_token="external-token")
    assert get_or_create_admin_token() == "external-token"


def test_internal_mcp_admin_token_uses_same_role_configuration(monkeypatch):
    from silvaengine_gateway.setting_builder import _generate_local_internal_mcp_token

    monkeypatch.setenv("ADMIN_USERNAME", "local-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "fake-password")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-admin-role-secret")
    configure(admin_role="policy-operator", admin_roles="policy-operator,qa-lead")
    payload = verify_local_jwt(
        _generate_local_internal_mcp_token("local-admin", "fake-password")
    )
    assert payload["user_id"] == "local-admin"
    assert payload["role"] == "policy-operator"
    assert payload["roles"] == ["policy-operator", "qa-lead"]


def test_local_users_keep_their_own_roles(monkeypatch):
    from silvaengine_gateway.auth.users import LocalUser
    from silvaengine_gateway.routes.auth import login

    configure()
    user = LocalUser("agent:test", "unused", ["agent"])
    monkeypatch.setattr(user, "verify", lambda password: True)
    GatewayConfig._USERS[user.username] = user
    token = login(OAuth2PasswordRequestForm(username=user.username, password="fake"))[
        "access_token"
    ]
    assert verify_local_jwt(token)["roles"] == ["agent"]


def test_role_environment_keys_are_registered_in_setting_spec(monkeypatch):
    from silvaengine_gateway.setting_builder import _load_setting_spec, _resolve_setting

    monkeypatch.setenv("ADMIN_ROLE", "policy-operator")
    monkeypatch.setenv("ADMIN_ROLES", "admin,policy-operator")
    spec = _load_setting_spec()
    configure(
        **{
            key: _resolve_setting(key, spec[key])
            for key in ("admin_role", "admin_roles")
        }
    )
    assert claims()["role"] == "policy-operator"
    assert claims()["roles"] == ["admin", "policy-operator"]


def test_generated_admin_token_is_not_written_to_logs(caplog):
    configure()
    with caplog.at_level(logging.INFO):
        token = get_or_create_admin_token()
    assert token not in caplog.text


@pytest.mark.parametrize(
    "primary, roles, expected",
    [
        (None, None, {"admin_role": "admin", "admin_roles": ["admin"]}),
        (
            "policy-operator",
            None,
            {"admin_role": "policy-operator", "admin_roles": ["policy-operator"]},
        ),
        (
            None,
            "qa-lead,policy-operator",
            {"admin_role": "qa-lead", "admin_roles": ["qa-lead", "policy-operator"]},
        ),
    ],
)
def test_yaml_role_defaults_and_independent_env_overrides(
    monkeypatch, primary, roles, expected
):
    from silvaengine_gateway.setting_builder import (
        _load_setting_spec,
        _resolve_admin_role_settings,
    )

    if primary is not None:
        monkeypatch.setenv("ADMIN_ROLE", primary)
    if roles is not None:
        monkeypatch.setenv("ADMIN_ROLES", roles)
    spec = _load_setting_spec()
    assert spec["admin_role"]["default"] == "admin"
    assert spec["admin_roles"]["default"] == ["admin"]
    assert _resolve_admin_role_settings(spec) == expected
