# -*- coding: utf-8 -*-
"""
FastAPI dependencies for SilvaEngine Gateway route handlers.

These are injected by the router_builder when registering dispatch targets
from the route manifest. They handle HTTP-specific concerns (headers, auth
context) and produce the params dict that gets passed to core dispatch functions.
"""

from __future__ import print_function

__author__ = "silvaengine"

import json
import logging
from typing import Any, Dict, Tuple

from fastapi import HTTPException, Request

from knowledge_graph_engine.utils.partition_key import build_partition_key_from_headers

logger = logging.getLogger(__name__)


async def extract_request_context(
    endpoint_id: str, request: Request
) -> Dict[str, Any]:
    """Build the params dict from an incoming HTTP request.

    Extracts:
    - JSON body
    - partition_key + part_id from Part-Id header
    - endpoint_id from path parameter
    - Authenticated user from request.state (set by FlexJWTMiddleware)

    Returns:
        Dict ready to pass to a core dispatch function.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    if not isinstance(body, dict):
        body = {"query": body}

    # Build partition key from headers
    try:
        partition_key, part_id = build_partition_key_from_headers(
            endpoint_id, dict(request.headers)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Inject context
    if not body.get("context"):
        body["context"] = {}
    body["context"]["partition_key"] = partition_key
    body["context"]["part_id"] = part_id
    body["endpoint_id"] = endpoint_id
    body["part_id"] = part_id

    # Inject authenticated user
    user = getattr(request.state, "user", None)
    if user:
        body["context"]["user"] = user

    return body


def require_partition_key(endpoint_id: str, request: Request) -> Tuple[str, str]:
    """FastAPI dependency that validates Part-Id header and returns (partition_key, part_id).

    Raises HTTPException(400) if Part-Id is missing.
    """
    try:
        return build_partition_key_from_headers(endpoint_id, dict(request.headers))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))