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
    def __init__(self, app, public_paths: Iterable[str] = (),
                 public_suffixes: Iterable[str] = ()):
        super().__init__(app)
        self.public_paths: List[str] = list(public_paths) + ["/auth"]
        # Path suffixes that are public regardless of the leading endpoint_id
        # segment — e.g. the A2A Agent Card discovery endpoint
        # "/{endpoint_id}/.well-known/agent-card.json", which the A2A spec
        # requires to be reachable without authentication.
        self.public_suffixes: List[str] = list(public_suffixes)

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
        for suffix in self.public_suffixes:
            if path.endswith(suffix):
                return True
        return False

    async def dispatch(self, request: Request, call_next):
        # Never authenticate CORS preflight — OPTIONS requests carry no
        # Authorization header and must reach the CORS middleware to get
        # Access-Control-* headers.
        if request.method == "OPTIONS":
            return await call_next(request)

        is_public = self._is_public(request.url.path)

        auth = request.headers.get("authorization")
        has_bearer = bool(auth and auth.lower().startswith("bearer "))

        if not has_bearer:
            if is_public:
                return await call_next(request)
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )

        # A Bearer token is present — decode it regardless of whether the
        # route is public. public_suffixes/public_paths only mean "don't
        # *require* a token" (e.g. merchant_engine's/user_engine's anonymous
        # Signup/registerUser mutations share their route with every other,
        # identity-requiring operation on that same module). Skipping
        # decoding here left request.state.user permanently unset for every
        # call to such a route, so authenticated requests to it silently
        # never got operator_id/tenant_id — failing closed with "缺少操作者
        # 身份" regardless of a valid token.
        token = auth.split(" ", 1)[1]
        mode = GatewayConfig.auth_provider

        try:
            if mode == "cognito":
                claims = await verify_cognito_jwt(token)
            else:
                claims = verify_local_jwt(token)
            request.state.user = claims
        except HTTPException as e:
            if is_public:
                # An invalid/expired token on a route that also serves
                # anonymous callers shouldn't block the request — proceed
                # without identity rather than rejecting outright.
                return await call_next(request)
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