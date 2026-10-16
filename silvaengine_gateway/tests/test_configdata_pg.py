# -*- coding: utf-8 -*-
"""se-configdata PostgreSQL backend (SE_CONFIGDATA_BACKEND=postgresql).

Exercised against SQLite through SQLAlchemy — the reader only issues a
portable SELECT and decodes JSON text values, so no live PostgreSQL is
needed here.
"""

import json

import pytest
from sqlalchemy import create_engine, text

from silvaengine_gateway import setting_builder


def _seed(url, rows):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE se_configdata (setting_id TEXT, variable TEXT, "
                "value TEXT, PRIMARY KEY (setting_id, variable))"
            )
        )
        for sid, variable, value in rows:
            conn.execute(
                text("INSERT INTO se_configdata VALUES (:s, :v, :val)"),
                {"s": sid, "v": variable, "val": json.dumps(value)},
            )
    engine.dispose()


@pytest.fixture
def pg_env(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'cfg.db'}"
    monkeypatch.setenv("SETTING_SOURCE", "se-configdata")
    monkeypatch.setenv("SE_CONFIGDATA_BACKEND", "postgresql")
    monkeypatch.setenv("SE_CONFIGDATA_DATABASE_URL", url)
    monkeypatch.setenv("ADAPTER_STAGE", "beta")
    monkeypatch.setenv("ADAPTER_AREA", "core")
    monkeypatch.setenv("ENDPOINT_ID", "banyan")
    monkeypatch.delenv("BANYAN_LOOPBACK_BASE_URL", raising=False)
    return url


def test_pg_backend_overlays_record(pg_env):
    _seed(
        pg_env,
        [
            ("beta_core_banyan", "jwt_secret", "s3cret"),
            ("beta_core_banyan", "jwt_config", {"issuer": "banyan", "ttl": 60}),
            ("beta_core_banyan", "plugins", [{"config": {"httpx_llm": {"type": "httpx"}}}]),
            ("other_core_banyan", "jwt_secret", "not-mine"),
        ],
    )
    merged = setting_builder._load_se_configdata_setting({"jwt_secret": "env", "x": 1})
    assert merged["jwt_secret"] == "s3cret"  # record wins over env
    assert merged["jwt_config"] == {"issuer": "banyan", "ttl": 60}
    assert merged["plugins"][0]["config"]["httpx_llm"]["type"] == "httpx"
    assert merged["x"] == 1


def test_pg_backend_missing_record_fails_fast(pg_env):
    _seed(pg_env, [("other_core_banyan", "jwt_secret", "x")])
    with pytest.raises(ValueError, match="Cannot find values"):
        setting_builder._load_se_configdata_setting({})


def test_pg_backend_dsn_from_db_settings(monkeypatch):
    monkeypatch.delenv("SE_CONFIGDATA_DATABASE_URL", raising=False)
    url = setting_builder._configdata_pg_url(
        {"db_host": "h", "db_port": 5433, "db_user": "u", "db_password": "p@ss", "db_schema": "d"}
    )
    assert url == "postgresql+psycopg://u:p%40ss@h:5433/d"
    with pytest.raises(ValueError, match="PG_HOST"):
        setting_builder._configdata_pg_url({})


def test_unknown_backend_rejected(pg_env, monkeypatch):
    monkeypatch.setenv("SE_CONFIGDATA_BACKEND", "mongo")
    with pytest.raises(ValueError, match="Unknown SE_CONFIGDATA_BACKEND"):
        setting_builder._load_se_configdata_setting({})


def test_invalid_table_name_rejected(pg_env, monkeypatch):
    monkeypatch.setenv("SE_CONFIGDATA_PG_TABLE", "se_configdata; drop table x")
    with pytest.raises(ValueError, match="Invalid SE_CONFIGDATA_PG_TABLE"):
        setting_builder._load_se_configdata_setting({})
