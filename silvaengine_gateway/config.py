# -*- coding: utf-8 -*-
"""
Gateway configuration — auth settings, server settings, and route manifest path.

These settings were moved from knowledge_graph_engine.handlers.config.
The core Config keeps only infrastructure settings (DynamoDB, Neo4j, LLM, cache).
"""

from __future__ import print_function

__author__ = "silvaengine"

import logging
import os
import threading
from typing import Any, Dict, Optional

import boto3

from .auth.users import LocalUser, load_users


class GatewayConfig:
    """
    Gateway-level configuration singleton.
    Holds auth settings, server settings, and route manifest configuration.
    Thread-safe singleton pattern.
    """

    _initialized: bool = False
    _lock: threading.RLock = threading.RLock()
    _logger: Optional[logging.Logger] = None
    _setting: Dict[str, Any] = {}
    _USERS: Dict[str, LocalUser] = {}

    # Auth settings
    auth_provider: str = "local"  # "local" | "cognito" | "module"
    jwt_secret_key: str = "CHANGEME"
    jwt_algorithm: str = "HS256"
    access_token_exp: int = 15  # minutes
    admin_username: str = ""
    admin_password: str = ""
    admin_static_token: str = ""

    # auth_provider="module" settings (see routes/auth.py::_get_module_auth_token).
    # Not hardcoded to any specific engine — auth_module_dispatch is a
    # "package.module:function" ref (same convention as routes.yaml's own
    # `dispatch`/`on_startup`/etc., resolved via router_builder.resolve_dispatch)
    # pointing at ANY module's dispatch_graphql-style entry point that exposes a
    # `login(input: {email, password}, idempotencyKey)` mutation returning
    # `{authToken, requiresMfa}` — user_engine is just the default/example target.
    # No part_id here: it now has one specific meaning system-wide — the
    # authenticated caller's real tenant_id (see b2b-agent-builder's
    # Part-Id header, set post-login from UserType.tenantId) — which by
    # definition doesn't exist yet at this pre-login call. A static
    # override would just reintroduce a fixed value standing in for
    # something that's supposed to be per-user.
    auth_module_dispatch: str = "user_engine.main:dispatch_graphql"

    # Cognito settings
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_secret: str = ""
    jwks_endpoint: str = ""
    jwks_cache_ttl: int = 3600
    issuer: str = ""
    aws_cognito_idp: Any = None

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    # Route manifest settings
    routes_config_path: Optional[str] = None  # Path to routes.yaml

    # KGE engine reference (set by lifespan when initializing)
    kge: Any = None

    @classmethod
    def initialize(cls, logger: logging.Logger, setting: Dict[str, Any]) -> None:
        with cls._lock:
            if cls._initialized and cls._setting == setting:
                cls._logger = logger
                return

            try:
                if cls._initialized:
                    cls.reset()

                cls._logger = logger
                cls._setting = dict(setting)
                cls._initialize_auth(setting)

                cls._initialized = True
            except Exception as e:
                import sys
                import traceback
                sys.stderr.write(f"GatewayConfig Initialize Error: {e}\n")
                traceback.print_exc(file=sys.stderr)
                logger.exception("Failed to initialize gateway configuration.")
                raise e

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._initialized = False
            cls._logger = None
            cls._setting = {}
            cls._USERS = {}
            cls.kge = None
            cls.auth_provider = "local"
            cls.jwt_secret_key = "CHANGEME"
            cls.jwt_algorithm = "HS256"
            cls.access_token_exp = 15
            cls.admin_username = ""
            cls.admin_password = ""
            cls.admin_static_token = ""
            cls.auth_module_dispatch = "user_engine.main:dispatch_graphql"
            cls.cognito_user_pool_id = ""
            cls.cognito_app_client_id = ""
            cls.cognito_app_secret = ""
            cls.jwks_endpoint = ""
            cls.jwks_cache_ttl = 3600
            cls.issuer = ""
            cls.aws_cognito_idp = None

    @classmethod
    def _initialize_auth(cls, setting: Dict[str, Any]) -> None:
        cls.auth_provider = setting.get("auth_provider", os.getenv("GATEWAY_AUTH_PROVIDER", "local"))
        # Fall back to the platform's shared jwt_secret (the only secret an
        # se-configdata deployment carries) so FlexJWT verifies the engines'
        # service tokens.
        cls.jwt_secret_key = (
            setting.get("jwt_secret_key")
            or setting.get("jwt_secret")
            or os.getenv("JWT_SECRET_KEY", "CHANGEME")
        )
        cls.jwt_algorithm = setting.get("jwt_algorithm", os.getenv("JWT_ALGORITHM", "HS256"))
        cls.access_token_exp = int(setting.get("access_token_exp", os.getenv("ACCESS_TOKEN_EXP", "15")))
        cls.admin_username = setting.get("admin_username", os.getenv("ADMIN_USERNAME", ""))
        cls.admin_password = setting.get("admin_password", os.getenv("ADMIN_PASSWORD", ""))
        cls.admin_static_token = setting.get("admin_static_token", os.getenv("ADMIN_STATIC_TOKEN", ""))
        cls.auth_module_dispatch = setting.get(
            "auth_module_dispatch",
            os.getenv("GATEWAY_AUTH_MODULE_DISPATCH", "user_engine.main:dispatch_graphql"),
        )

        # Server settings
        cls.host = setting.get("host", os.getenv("GATEWAY_HOST", "0.0.0.0"))
        cls.port = int(setting.get("port", os.getenv("GATEWAY_PORT", "8000")))
        cls.workers = int(setting.get("workers", os.getenv("GATEWAY_WORKERS", "1")))

        # Route manifest settings
        cls.routes_config_path = setting.get("routes_config_path", os.getenv("GATEWAY_ROUTES_CONFIG_PATH"))

        if cls.auth_provider == "cognito":
            cls.cognito_user_pool_id = setting.get("cognito_user_pool_id", os.getenv("COGNITO_USER_POOL_ID", ""))
            cls.cognito_app_client_id = setting.get("cognito_app_client_id", os.getenv("COGNITO_APP_CLIENT_ID", ""))
            cls.cognito_app_secret = setting.get("cognito_app_secret", os.getenv("COGNITO_APP_SECRET", ""))
            region = setting.get("region_name", os.getenv("region_name", "us-east-1"))
            cls.issuer = (
                f"https://cognito-idp.{region}.amazonaws.com/{cls.cognito_user_pool_id}"
            )
            cls.jwks_endpoint = (
                setting.get("cognito_jwks_url")
                or os.getenv("COGNITO_JWKS_URL")
                or f"{cls.issuer}/.well-known/jwks.json"
            )
            cls.jwks_cache_ttl = int(setting.get("jwks_cache_ttl", os.getenv("JWKS_CACHE_TTL", "3600")))

            if all(
                setting.get(k) or os.getenv(k)
                for k in ["region_name", "aws_access_key_id", "aws_secret_access_key"]
            ):
                aws_credentials = {
                    "region_name": setting.get("region_name") or os.getenv("region_name"),
                    "aws_access_key_id": setting.get("aws_access_key_id") or os.getenv("aws_access_key_id"),
                    "aws_secret_access_key": setting.get("aws_secret_access_key") or os.getenv("aws_secret_access_key"),
                }
                cls.aws_cognito_idp = boto3.client("cognito-idp", **aws_credentials)

        # Load local users file if configured
        user_file = setting.get("local_user_file", os.getenv("LOCAL_USER_FILE"))
        if user_file:
            cls._load_users(user_file)

    @classmethod
    def _load_users(cls, filepath: str) -> None:
        cls._USERS = load_users(filepath)

    @classmethod
    def get_logger(cls) -> logging.Logger:
        if cls._logger:
            return cls._logger
        return logging.getLogger()

    @classmethod
    def get_setting(cls) -> Dict[str, Any]:
        if not cls._initialized:
            raise RuntimeError("GatewayConfig not initialized")
        return cls._setting
