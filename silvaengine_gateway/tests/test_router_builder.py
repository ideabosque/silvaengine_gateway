# -*- coding: utf-8 -*-
"""Tests for router_builder — manifest parsing, dispatch resolution, and dynamic routing."""

import pytest
from silvaengine_gateway.router_builder import (
    ModuleSpec,
    RouteSpec,
    build_router_from_manifest,
    resolve_dispatch,
    validate_manifest,
)
from silvaengine_gateway.tasks import (
    InMemoryTaskBackend,
    get_task_backend,
    set_task_backend,
)


def test_task_backend_can_be_replaced():
    """Test the public backend installation API used by deployments."""
    backend = InMemoryTaskBackend()
    set_task_backend(backend)
    assert get_task_backend() is backend


def test_module_spec_validation():
    """Test ModuleSpec and RouteSpec Pydantic models."""
    module = ModuleSpec(
        name="knowledge_graph_engine",
        package="knowledge_graph_engine",
        transport="graphql",
        routes=[
            RouteSpec(
                path="/{endpoint_id}/{part_id}/knowledge_graph_graphql",
                dispatch="knowledge_graph_engine.main:dispatch_graphql",
                methods=["POST"],
                auth=True,
            )
        ],
    )
    assert module.name == "knowledge_graph_engine"
    assert len(module.routes) == 1
    assert module.routes[0].auth is True
    assert module.routes[0].dispatch == "knowledge_graph_engine.main:dispatch_graphql"
    assert module.routes[0].handler_type == "graphql"


def test_resolve_dispatch_invalid_module():
    """Test that resolve_dispatch raises ImportError for non-existent modules."""
    with pytest.raises(ImportError):
        resolve_dispatch("nonexistent.module:function")


def test_validate_manifest_empty():
    """Test that an empty manifest validates without warnings."""
    warnings = validate_manifest([])
    assert warnings == []


def test_validate_manifest_duplicate_paths():
    """Test that duplicate route paths produce warnings."""
    modules = [
        ModuleSpec(
            name="mod1",
            package="mod1",
            routes=[
                RouteSpec(path="/test", dispatch="mod1:handler", methods=["POST"]),
            ],
        ),
        ModuleSpec(
            name="mod2",
            package="mod2",
            routes=[
                RouteSpec(path="/test", dispatch="mod2:handler", methods=["POST"]),
            ],
        ),
    ]
    warnings = validate_manifest(modules)
    assert any("Duplicate route path" in w for w in warnings)


def test_validate_manifest_invalid_transport():
    """Test that invalid transport value produces warning."""
    modules = [
        ModuleSpec(
            name="mod1",
            package="mod1",
            transport="invalid",
            routes=[],
        ),
    ]
    warnings = validate_manifest(modules)
    assert any("invalid transport" in w for w in warnings)


def test_validate_manifest_invalid_adapter():
    """Test that unresolvable adapter produces warning."""
    modules = [
        ModuleSpec(
            name="mod1",
            package="mod1",
            routes=[
                RouteSpec(
                    path="/test",
                    dispatch="nonexistent.module:handler",
                    methods=["POST"],
                ),
            ],
        ),
    ]
    warnings = validate_manifest(modules)
    assert any("cannot be resolved" in w for w in warnings)


def test_resolve_dispatch_with_real_module():
    """Test resolve_dispatch with a real callable."""
    fn = resolve_dispatch("os.path:join")
    assert callable(fn)


def test_resolve_dispatch_dot_notation():
    """Test resolve_dispatch with dot notation (no colon)."""
    fn = resolve_dispatch("os.path.join")
    assert callable(fn)


def test_resolve_dispatch_non_callable():
    """Test that resolve_dispatch raises TypeError for non-callable."""
    with pytest.raises(TypeError):
        resolve_dispatch("os:path")  # os.path is a module, not callable
