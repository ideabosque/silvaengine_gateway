# -*- coding: utf-8 -*-
"""FlexJWTMiddleware — authenticates requests via local or Cognito JWT."""

from __future__ import print_function

__author__ = "silvaengine"

from typing import Iterable, List

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..config import GatewayConfig
from .jwt_cognito import verify_cognito_jwt
from .jwt_local import verify_local_jwt


class FlexJWTMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, public_paths: Iterable[str] = ()):
        super().__init__(app)
        self.public_paths: List[str] = list(public_paths) + ["/auth"]

    def _is_public(self, path: str) -> bool:
        """Match a public path on segment boundaries.

        ``startswith`` alone would treat ``/authenticate`` as public because it
        begins with ``/auth``, and would let an ``endpoint_id`` named ``health``
        or ``auth`` bypass authentication. Require an exact match or a path
        separator at the boundary.
        """
        for p in self.public_paths:
            if path == p or path.startswith(p + "/"):
                return True
        return False

    async def dispatch(self, request: Request, call_next):
        if self._is_public(request.url.path):
            return await call_next(request)

        auth = request.headers.get("authorization")
        if not (auth and auth.lower().startswith("bearer ")):
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )

        token = auth.split(" ", 1)[1]
        mode = GatewayConfig.auth_provider

        try:
            if mode == "cognito":
                claims = await verify_cognito_jwt(token)
            else:
                claims = verify_local_jwt(token)
            request.state.user = claims
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=e.headers,
            )

        return await call_next(request)


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency that extracts the authenticated user from request state."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user