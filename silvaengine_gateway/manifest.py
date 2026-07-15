# -*- coding: utf-8 -*-
"""
Route manifest loading for the SilvaEngine Gateway.

Resolves the module/route manifest that drives both dynamic route registration
(``app.create_app``) and the invoker's ``functs_on_local`` map
(``setting_builder.build_setting_from_env``).

This lives in its own module so both consumers can load the manifest without a
circular import between ``app`` and ``setting_builder``.
"""

from __future__ import print_function

__author__ = "silvaengine"

import logging
import os
from pathlib import Path
from typing import List

import yaml

from .config import GatewayConfig
from .router_builder import ModuleSpec, RouteSpec

logger = logging.getLogger(__name__)


def load_route_manifest(config: GatewayConfig) -> List[ModuleSpec]:
    """
    Load route manifest from:
    1. GATEWAY_ROUTES_CONFIG_PATH env var (YAML or JSON file)
    2. routes.yaml packaged with the gateway
    3. Built-in default (KGE only)
    """
    # Priority 1: explicit path
    configured_path = config.routes_config_path or os.environ.get(
        "GATEWAY_ROUTES_CONFIG_PATH"
    )
    routes_file = (
        Path(configured_path)
        if configured_path
        else Path(__file__).parent / "routes.yaml"
    )

    if routes_file.exists():
        try:
            with open(routes_file) as f:
                data = yaml.safe_load(f)
            modules = data.get("modules", [])
            return [ModuleSpec(**m) for m in modules]
        except Exception as e:
            logger.error(f"Failed to load routes from {routes_file}: {e}")
            raise

    # Priority 2: Built-in default (KGE only)
    logger.info("No route manifest found - using built-in default (KGE only)")
    return _default_manifest()


def _default_manifest() -> List[ModuleSpec]:
    """Built-in default route manifest - KGE only."""
    return [
        ModuleSpec(
            name="knowledge_graph_engine",
            package="knowledge_graph_engine",
            transport="graphql",
            routes=[
                RouteSpec(
                    path="/{endpoint_id}/knowledge_graph_graphql",
                    handler_type="graphql",
                    dispatch="knowledge_graph_engine.main:dispatch_graphql",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/extract",
                    handler_type="background",
                    dispatch="knowledge_graph_engine.main:dispatch_extract",
                    methods=["POST"],
                    auth=True,
                ),
                RouteSpec(
                    path="/{endpoint_id}/extract/status/{task_id}",
                    handler_type="task_status",
                    methods=["GET"],
                    auth=True,
                ),
            ],
        )
    ]
