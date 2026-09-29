# -*- coding: utf-8 -*-
"""Gateway auth middleware — FlexJWT (local/Cognito) + Banyan authorizer bridge."""

from __future__ import print_function

__author__ = "silvaengine"

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..config import GatewayConfig
from .jwt_cognito import verify_cognito_jwt
from .jwt_local import verify_local_jwt

logger = logging.getLogger(__name__)


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

        # Upstream middleware (Banyan PermAuthorizer bridge) may have
        # authenticated this request already: Banyan tokens are not verifiable
        # by the local/Cognito verifiers, and anonymous-whitelisted Banyan
        # operations carry no Authorization header at all.
        if (
            getattr(request.state, "user", None) is not None
            or getattr(request.state, AUTH_CHECKED_ATTR, False)
        ):
            return await call_next(request)

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


# ---------------------------------------------------------------------------
# Banyan authorizer bridge — perm_engine.PermAuthorizer as gateway auth
# ---------------------------------------------------------------------------
# In the Lambda chain the API Gateway hands every request to the
# ``perm_engine.authorizer.PermAuthorizer.verify_permission`` Lambda
# authorizer before any engine runs: it verifies the Banyan JWT
# (HS256, issuer/audience from ``jwt_config``, secret from ``jwt_secret``),
# loads the caller's roles from PostgreSQL, decides platform-admin status,
# and injects the claims into ``event["requestContext"]["authorizer"]``.
# Requests without a token pass only for the ``ANONYMOUS_OPS`` whitelist
# (login, registerUser, ...).
#
# This bridge gives the gateway the same gate without duplicating any of
# that logic: for requests whose first path segment is the configured
# Banyan endpoint_id it synthesizes a Lambda-style proxy event, runs the
# REAL ``PermAuthorizer.verify_permission`` in a thread executor (its role
# queries are blocking), and stores the outcome on the request state:
#
# - ``request.state.user`` — the authorizer claims dict
#   (``user_id``/``sub``/``is_admin``/``tenant_id``/``merchant_id``/
#   ``roles``) when the caller is authenticated. The dispatch handler then
#   promotes them into the GraphQL context at the same top-level keys the
#   resolvers read (Lambda parity, see router_builder._inject_user_claims).
# - ``request.state.banyan_auth_checked`` — set whenever verify_permission
#   ALLOWED the request (authenticated or anonymous-whitelisted), so the
#   downstream FlexJWT middleware hands off instead of re-rejecting.
#
# Failure semantics (fail-closed, mirroring the Lambda chain):
# ``AuthenticationError`` → 401, other ``PermissionError`` → 403, missing
# perm_engine import → 500. A bridge error never results in an
# unauthenticated request reaching an engine.
#
# Engines' short-lived cross-engine service tokens
# (orchestration_engine.utils.service_token) carry the same
# ``sub``/``iss``/``aud`` claims as user tokens, so loopback calls pass
# this bridge exactly like frontend calls — no internal bypass exists.

#: Request-state marker set once PermAuthorizer allowed the request.
AUTH_CHECKED_ATTR = "banyan_auth_checked"

#: Methods whose body may carry the GraphQL query (ANONYMOUS_OPS matching
#: parses it); bodies of other methods are not forwarded to the authorizer.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Lazily loaded ``(PermAuthorizer, AuthenticationError)``. perm_engine is
#: only importable when the deployment image includes the Banyan engines;
#: tests substitute a fake through this module-level cache. The second
#: element is the base class of every error verify_permission raises for
#: auth failures, so except clauses stay type-safe.
_perm_components: Optional[Tuple[Type[Any], Type[PermissionError]]] = None


def _load_perm_components() -> Tuple[Type[Any], Type[PermissionError]]:
    """Import PermAuthorizer and its AuthenticationError (cached).

    Returns:
        ``(PermAuthorizer class, AuthenticationError class)``.

    Raises:
        ImportError: when perm_engine is not installed — the caller must
            fail closed (500), never pass an unauthenticated request.
    """
    global _perm_components
    if _perm_components is None:
        import importlib

        authorizer_mod = importlib.import_module("perm_engine.authorizer")
        exceptions_mod = importlib.import_module("perm_engine.exceptions")
        _perm_components = (
            authorizer_mod.PermAuthorizer,
            exceptions_mod.AuthenticationError,
        )
    return _perm_components


class BanyanAuthorizerBridge(BaseHTTPMiddleware):
    """Authenticate Banyan-domain requests via perm_engine.PermAuthorizer."""

    def __init__(self, app, endpoint_id: str, setting: Dict[str, Any]) -> None:
        super().__init__(app)
        self.endpoint_id = str(endpoint_id).strip()
        # Gateway setting dict — carries jwt_secret / jwt_config (from the
        # se-configdata overlay) exactly like the Lambda chain's merged
        # connection setting that the framework passes to the authorizer.
        self.setting = dict(setting or {})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_banyan_path(self, path: str) -> bool:
        """True when the (already normalized) path targets the Banyan app.

        Banyan paths are ``/{endpoint_id}/{function}`` after the path
        normalizer stripped ``/{stage}/{area}``; everything else belongs
        to the gateway's own auth (FlexJWT).
        """
        segments = [seg for seg in path.split("/") if seg]
        return bool(segments) and segments[0] == self.endpoint_id

    def _build_proxy_event(self, request: Request) -> Dict[str, Any]:
        """Synthesize a Lambda-style proxy event for verify_permission.

        Mirrors api-runtime's adapter and the API Gateway proxy shape the
        authorizer was written against: lowercased headers (Starlette
        already lowercases), body as a JSON string, requestContext stub
        the authorizer fills in with claims.
        """
        # The body cannot be read here (building the event is sync); dispatch()
        # fills event["body"] after awaiting request.body(). BaseHTTPMiddleware
        # replays a consumed body to the downstream handler (Starlette's
        # _CachedRequest), so buffering it there is safe.
        return {
            "httpMethod": request.method,
            "path": request.url.path,
            "headers": {k: v for k, v in request.headers.items()},
            "body": None,
            "requestContext": {"http": {"method": request.method}},
        }

    # ------------------------------------------------------------------
    # Middleware entry
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next):
        # CORS preflight carries no Authorization header; let the CORS
        # middleware answer it.
        if request.method == "OPTIONS":
            return await call_next(request)

        if not self._is_banyan_path(request.url.path):
            return await call_next(request)

        try:
            authorizer_cls, authentication_error = _load_perm_components()
        except ImportError as e:
            logger.error(
                "perm_engine is not importable — cannot authenticate Banyan "
                "requests (fail closed): %s",
                e,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Banyan authorizer unavailable"},
            )

        raw_body: Optional[bytes] = None
        if request.method in _BODY_METHODS:
            raw_body = await request.body()
        event = self._build_proxy_event(request)
        if raw_body:
            event["body"] = raw_body.decode("utf-8", errors="replace")
        # Guardrail (approved): log only the PRESENCE of x-api-key, never its
        # value — the Lambda chain enforces usage plans on it; the container
        # chain passes it through unvalidated (P0) while engines supply it via
        # their pools' default headers.
        logger.debug("x-api-key present: %s", "x-api-key" in event["headers"])

        authorizer = authorizer_cls(
            logging.getLogger("silvaengine_gateway.banyan"), **self.setting
        )

        def _verify() -> Any:
            # Context is unused by verify_permission — pass None.
            return authorizer.verify_permission(event, None)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _verify)
        except authentication_error as e:  # must precede PermissionError
            return JSONResponse(
                status_code=401,
                content={"detail": str(e) or "Authentication required"},
            )
        except PermissionError as e:
            return JSONResponse(
                status_code=403,
                content={"detail": str(e) or "Forbidden"},
            )

        # verify_permission returned the (allowed) event. Mark the handoff
        # so FlexJWT does not re-reject an approved anonymous request, and
        # promote claims when the caller is authenticated.
        setattr(request.state, AUTH_CHECKED_ATTR, True)
        claims = ((result or {}).get("requestContext") or {}).get("authorizer")
        if isinstance(claims, dict) and claims:
            request.state.user = claims

        return await call_next(request)