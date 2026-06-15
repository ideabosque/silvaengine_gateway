# -*- coding: utf-8 -*-
"""
ResolveInfo shimming — builds Graphene-compatible info objects from REST context.

Core dispatch functions expect a ``ResolveInfo`` (or compatible) object with
``info.context`` carrying partition_key, endpoint_id, part_id, etc.
This module builds a lightweight shim so the gateway can call dispatch functions
without requiring Graphene.
"""

from __future__ import print_function

__author__ = "silvaengine"

from types import SimpleNamespace
from typing import Any, Dict, Optional


def build_resolve_info(
    endpoint_id: str,
    part_id: str,
    user: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    logger: Any = None,
) -> Any:
    """
    Build a ResolveInfo-compatible object for calling core dispatch functions.

    Args:
        endpoint_id: Tenant endpoint identifier
        part_id: Tenant partition identifier
        user: Authenticated user claims (from JWT)
        settings: Core config settings dict
        logger: Logger instance

    Returns:
        SimpleNamespace with .context dict containing partition_key,
        endpoint_id, part_id, and optional user/settings.
    """
    context: Dict[str, Any] = {
        "endpoint_id": endpoint_id,
        "part_id": part_id,
        "partition_key": f"{endpoint_id}#{part_id}",
    }

    if user:
        context["user"] = user

    if settings:
        context.update(settings)

    info = SimpleNamespace(context=context)

    if logger:
        info.context["logger"] = logger

    return info