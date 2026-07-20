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


# ---------------------------------------------------------------------------
# Custom YAML loader supporting a master manifest that imports child files.
#
# Supported directives (paths are relative to the file containing the tag,
# or absolute):
#
#   modules:
#     - !include routes/kge.yaml          # child contains a single module map
#     - !include routes/rfq.yaml
#
#   # OR a child file may itself be a list of module maps:
#   modules:
#     - !include routes/batch.yaml        # batch.yaml root is a YAML list
#
# A child file referenced by !include must contain either:
#   - a single module map (a mapping with `name:` / `package:` keys), or
#   - a YAML list of such module maps.
# The result is spliced into the parent's `modules:` list in place.
# ---------------------------------------------------------------------------


class _IncludeLoader(yaml.SafeLoader):
    """SafeLoader subclass carrying the directory of the current file.

    Used so ``!include`` paths resolve relative to the including file.
    A new loader instance is built per top-level file; child includes
    construct their own loader with the child's directory.
    """

    def __init__(self, stream):
        # ``stream`` is the open file object; its ``.name`` is the path
        # when loaded via ``yaml.load(stream)``.
        name = getattr(stream, "name", None)
        self._base_dir = (
            Path(name).resolve().parent if name else Path.cwd().resolve()
        )
        super().__init__(stream)


def _load_child(path: Path):
    """Load a child YAML file with its own ``_IncludeLoader`` so nested
    ``!include`` directives resolve relative to the child file."""
    with open(path, encoding="utf-8") as f:
        # Pass the file handle (not raw text) so the loader can read its
        # ``.name`` attribute and resolve further relative includes.
        return yaml.load(f, Loader=_IncludeLoader)


def _include_constructor(loader: _IncludeLoader, node: yaml.Node):
    """``!include <relative/path>`` — load a child file and return its
    top-level value. The child may be a single module map or a list of
    module maps; the parent is responsible for splicing into ``modules:``.
    """
    if isinstance(node, yaml.ScalarNode):
        rel = loader.construct_scalar(node)
    else:
        raise yaml.constructor.ConstructorError(
            None, None, "!include expects a scalar path", node.start_mark
        )

    # Resolve relative to the including file's directory.
    child_path = (loader._base_dir / rel).resolve()
    if not child_path.exists():
        raise FileNotFoundError(
            f"!include target not found: {child_path} (referenced from {loader._base_dir})"
        )

    return _load_child(child_path)


_IncludeLoader.add_constructor("!include", _include_constructor)


def load_route_manifest(config: GatewayConfig) -> List[ModuleSpec]:
    """
    Load route manifest from:
    1. GATEWAY_ROUTES_CONFIG_PATH env var (YAML or JSON file)
    2. routes.yaml packaged with the gateway
    3. Built-in default (KGE only)

    The master manifest may split modules across child files using
    ``!include`` directives (paths relative to the including file)::

        modules:
          - !include routes/kge.yaml
          - !include routes/rfq.yaml
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
            with open(routes_file, encoding="utf-8") as f:
                data = yaml.load(f, Loader=_IncludeLoader)
            modules_raw = (data or {}).get("modules", [])
            # ``modules`` may be a heterogeneous list: each item is either
            # a module map (from a single-module child or inline) or a
            # list of module maps (from a child file whose root is a list).
            flat: list = []
            for item in modules_raw:
                if isinstance(item, list):
                    flat.extend(item)
                elif isinstance(item, dict):
                    flat.append(item)
                else:
                    raise ValueError(
                        f"Unexpected entry type {type(item).__name__} in modules list"
                    )
            return [ModuleSpec(**m) for m in flat]
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
