# -*- coding: utf-8 -*-
"""Auth token routes — login endpoint for local and Cognito."""

from __future__ import print_function

__author__ = "silvaengine"

import base64
import hashlib
import hmac
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


def _get_cognito_token(username: str, password: str) -> Dict[str, Any]:
    resp = GatewayConfig.aws_cognito_idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=GatewayConfig.cognito_app_client_id,
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
            "SECRET_HASH": _secret_hash(username),
        },
    )
    tokens = resp["AuthenticationResult"]
    return {"access_token": tokens["AccessToken"], "token_type": "bearer"}


def _secret_hash(username: str) -> str:
    if not GatewayConfig.cognito_app_client_id or not GatewayConfig.cognito_app_secret:
        raise ValueError("Cognito app client ID and secret must be configured")
    message = (username + GatewayConfig.cognito_app_client_id).encode("utf-8")
    key = GatewayConfig.cognito_app_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()