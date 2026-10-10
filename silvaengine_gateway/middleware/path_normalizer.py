# -*- coding: utf-8 -*-
"""ASGI middleware that rewrites the Banyan frontend path contract.

The Banyan frontend calls ``{baseUrl}/{stage}/{area}/{endpointId}/{function}``
(e.g. ``/beta/core/banyan/user_engine_graphql``), mirroring the AWS API
Gateway resource layout the Lambda chain exposes. The gateway's native
contract is ``/{endpoint_id}/{function}`` (tenant partition in the
``Part-Id``/``part_id`` header). This middleware strips the configured
``/{stage}/{area}`` prefix so the native router serves the frontend with
zero changes — both contracts stay live simultaneously.

Implementation notes:

- Pure ASGI, deliberately NOT ``BaseHTTPMiddleware`` — WebSocket scope
  upgrades and raw streaming pass through untouched.
- The ``scope`` dict is mutated in place (the same technique Starlette's
  own root_path handling uses); the router sees the rewritten path for
  both route matching and ``path_params``.
- Only exact ``/{stage}/{area}/...`` prefixes are stripped. A native
  route whose ``endpoint_id`` happens to equal the stage value (e.g.
  ``/beta/something``) is unaffected because the second segment must
  match the area; a wrong stage (``/prod/core/...`` when configured for
  ``beta``) passes through and 404s naturally instead of silently
  rewriting to a different tenant's shape.
"""

from __future__ import print_function

__author__ = "silvaengine"

import os
from typing import Optional

STAGE_ENV = "ADAPTER_STAGE"
AREA_ENV = "ADAPTER_AREA"


class BanyanPathNormalizer:
    """Strip ``/{stage}/{area}`` from Banyan frontend paths (pure ASGI)."""

    def __init__(
        self,
        app,
        stage: Optional[str] = None,
        area: Optional[str] = None,
    ) -> None:
        self.app = app
        # ``or "beta"``/``or "core"`` guards: os.getenv is typed str | None
        # even with a default, and strip() must never see None.
        self.stage = (stage or os.getenv(STAGE_ENV, "beta") or "beta").strip() or "beta"
        self.area = (area or os.getenv(AREA_ENV, "core") or "core").strip() or "core"

    def _normalized_path(self, path: str) -> Optional[str]:
        """Return the rewritten path, or None to pass through unchanged.

        Requires at least three non-empty segments so the rewrite never
        mangles two-segment native paths or single-segment health/auth
        routes.
        """
        segments = [seg for seg in path.split("/") if seg]
        if len(segments) < 3:
            return None
        if segments[0] != self.stage or segments[1] != self.area:
            return None
        return "/" + "/".join(segments[2:])

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") in ("http", "websocket"):
            new_path = self._normalized_path(scope.get("path", "") or "")
            if new_path is not None:
                scope["path"] = new_path
                raw_path = scope.get("raw_path")
                if raw_path is not None:
                    scope["raw_path"] = new_path.encode("utf-8")
        await self.app(scope, receive, send)