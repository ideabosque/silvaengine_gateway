# -*- coding: utf-8 -*-
"""
Request context extraction utilities.

Gateway route handlers use these to pull partition_key, user, and
other context from FastAPI Request objects before calling core dispatch.
"""

from __future__ import print_function

__author__ = "silvaengine"

import json
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request


async def extract_request_context(
    request: Request, endpoint_id: str
) -> Tuple[Dict[str, Any], str, str]:
    """
    Extract and build params dict from a FastAPI Request.

    Returns (params, partition_key, part_id).

    Raises:
        HTTPException(400): If Part-Id header is missing.
    """
    params = await request.json() if request.method == "POST" else {}

    # Partition key from Part-Id header
    part_id = request.headers.get("Part-Id") or request.headers.get("Part-ID")
    if not part_id:
        raise HTTPException(
            status_code=400,
            detail="Part-Id header is required to construct partition_key",
        )
    partition_key = f"{endpoint_id}#{part_id}"

    # Ensure context dict exists
    if not params.get("context"):
        params["context"] = {}
    params["context"]["partition_key"] = partition_key
    params["context"]["part_id"] = part_id
    params["endpoint_id"] = endpoint_id
    params["part_id"] = part_id

    # Inject authenticated user from middleware
    user = getattr(request.state, "user", None)
    if user:
        params["context"]["user"] = user

    return params, partition_key, part_id