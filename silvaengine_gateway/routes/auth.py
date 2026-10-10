# -*- coding: utf-8 -*-
"""Auth token routes — login endpoint for local and Cognito."""

from __future__ import print_function

__author__ = "silvaengine"

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from ..config import GatewayConfig
from ..auth.jwt_local import create_local_jwt, get_or_create_admin_token

router = APIRouter(prefix="/auth", tags=["auth"])


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/token", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends()) -> Dict[str, Any]:
    if GatewayConfig.auth_provider == "cognito":
        return _get_cognito_token(form.username, form.password)
    elif GatewayConfig.auth_provider == "module":
        return _get_module_auth_token(form.username, form.password)
    else:
        return _get_local_token(form.username, form.password)


def _get_local_token(username: str, password: str) -> Dict[str, Any]:
    if (
        GatewayConfig.admin_username
        and GatewayConfig.admin_password
        and username == GatewayConfig.admin_username
        and password == GatewayConfig.admin_password
    ):
        return {"access_token": get_or_create_admin_token(), "token_type": "bearer"}

    user = GatewayConfig._USERS.get(username)
    if not user or not user.verify(password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_local_jwt({"username": user.username, "roles": user.roles})
    return {"access_token": token, "token_type": "bearer"}


#: Minimal Login mutation — only the fields this endpoint consumes. Assumes
#: the configured target (GatewayConfig.auth_module_dispatch) exposes a
#: `login(input: {email, password}, idempotencyKey)` mutation returning
#: `{authToken, requiresMfa}` — the de facto login-mutation shape, not
#: specific to any one engine.
_MODULE_LOGIN_MUTATION = """
mutation Login($input: LoginInput!, $idempotencyKey: ID!) {
  login(input: $input, idempotencyKey: $idempotencyKey) {
    authToken
    requiresMfa
  }
}
"""


def _get_module_auth_token(username: str, password: str) -> Dict[str, Any]:
    """Issue a token by delegating credential checks to a configured module.

    The target is **not hardcoded** — ``GatewayConfig.auth_module_dispatch``
    is a ``"package.module:function"`` ref (default
    ``"user_engine.main:dispatch_graphql"``), resolved via
    ``router_builder.resolve_dispatch`` — the exact same importlib-based
    resolution ``routes.yaml``'s own ``dispatch``/``on_startup``/etc. fields
    already use. A deployment can point this at any module exposing an
    equivalent ``dispatch_graphql``-style entry point and a matching
    ``login`` mutation.

    Called in-process rather than over HTTP — ``/auth/token`` cannot itself
    require an Authorization header to issue one. The target module's own
    token minting is expected to sign with the same shared ``jwt_secret``/
    algorithm ``verify_local_jwt`` already checks (true for every engine in
    this monorepo, confirmed for user_engine this session), so no
    verification-side change is needed.

    No part_id is passed: it now has one specific meaning system-wide —
    the authenticated caller's real tenant_id — which doesn't exist yet at
    this pre-login call (confirmed for user_engine — partition_key was
    retired there in favor of tenant_id/merchant_id claims, so its own
    Login resolver is global-by-email and never reads it).

    Login dispatch receives endpoint_id=None: credential checks must work
    before endpoint selection. A custom target requiring a deployment
    endpoint must supply it inside its own dispatch adapter.
    """
    from ..router_builder import resolve_dispatch

    try:
        dispatch_fn = resolve_dispatch(GatewayConfig.auth_module_dispatch)
    except (ImportError, AttributeError, TypeError) as e:
        raise HTTPException(
            status_code=500,
            detail=f"auth_module_dispatch '{GatewayConfig.auth_module_dispatch}' failed to resolve: {e}",
        )

    response = dispatch_fn(
        query=_MODULE_LOGIN_MUTATION,
        variables={
            "input": {"email": username, "password": password},
            "idempotencyKey": str(uuid.uuid4()),
        },
        context={},
        endpoint_id=None,
        connection_id=None,
        headers={},
    )

    # dispatch_graphql-style entry points return a Lambda-proxy-style
    # envelope ({"statusCode", "headers", "body": "<JSON string>"} — see
    # silvaengine_utility.Graphql.execute/HttpResponse.format_response), not
    # a bare {data, errors} dict. Unwrap it the same way router_builder.py
    # does for every other dispatch call.
    body = response.get("body") if isinstance(response, dict) else response
    result = json.loads(body) if isinstance(body, str) else (body or {})

    if result.get("errors"):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    login_result = (result.get("data") or {}).get("login") or {}
    if login_result.get("requiresMfa"):
        raise HTTPException(
            status_code=403,
            detail=(
                "MFA required; /auth/token does not support MFA — use the "
                "target module's full GraphQL Login mutation directly"
            ),
        )

    auth_token = login_result.get("authToken")
    if not auth_token:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {"access_token": auth_token, "token_type": "bearer"}


def _get_cognito_token(username: str, password: str) -> Dict[str, Any]:
    from botocore.exceptions import ClientError

    try:
        resp = GatewayConfig.aws_cognito_idp.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=GatewayConfig.cognito_app_client_id,
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": _secret_hash(username),
            },
        )
    except ClientError as e:
        # Map Cognito auth failures to proper HTTP codes instead of a 500.
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if code == "UserNotConfirmedException":
            raise HTTPException(status_code=403, detail="User is not confirmed")
        raise HTTPException(status_code=502, detail=f"Cognito auth error: {code}")

    # A challenge (e.g. NEW_PASSWORD_REQUIRED) returns no tokens.
    tokens = resp.get("AuthenticationResult")
    if not tokens:
        raise HTTPException(
            status_code=401,
            detail=f"Auth challenge required: {resp.get('ChallengeName', 'unknown')}",
        )
    return {"access_token": tokens["AccessToken"], "token_type": "bearer"}


def _secret_hash(username: str) -> str:
    if not GatewayConfig.cognito_app_client_id or not GatewayConfig.cognito_app_secret:
        raise ValueError("Cognito app client ID and secret must be configured")
    message = (username + GatewayConfig.cognito_app_client_id).encode("utf-8")
    key = GatewayConfig.cognito_app_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
