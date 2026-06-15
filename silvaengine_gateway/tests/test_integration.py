# -*- coding: utf-8 -*-
"""
Integration tests for SilvaEngine Gateway — requires live DynamoDB + Neo4j.

Run with:
    pytest silvaengine_gateway/tests/test_integration.py -v -m integration

These tests use the .env file in tests/ for AWS + Neo4j credentials.
"""

import json
import time

import pytest

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


class TestHealthAndAuth:
    """Basic health/auth integration tests against live server."""

    def test_health_no_auth(self, integration_client):
        """Health endpoint should be accessible without auth."""
        resp = integration_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "silvaengine-gateway"

    def test_auth_token_local(self, integration_client):
        """Local auth should return a valid JWT."""
        resp = integration_client.post(
            "/auth/token",
            data={"username": "admin", "password": "admin123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_me_authenticated(self, integration_client, integration_headers):
        """/me should return user claims with valid token."""
        resp = integration_client.get("/me", headers=integration_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "username" in data

    def test_me_unauthenticated(self, integration_client):
        """/me should reject requests without auth."""
        resp = integration_client.get("/me")
        assert resp.status_code == 401


class TestGraphQLProxy:
    """Test gateway → KGE GraphQL proxying via dispatch functions."""

    def test_graphql_introspection(self, integration_client, integration_headers):
        """GraphQL introspection query should succeed through gateway."""
        query = '{"query": "{ __schema { queryType { name } } }"}'
        resp = integration_client.post(
            "/test-ep/test-part/knowledge_graph_graphql",
            data=query,
            headers={
                **integration_headers,
                "Content-Type": "application/json",
                "Part-Id": "test-part",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should return valid GraphQL response (not a server error)
        assert "data" in data or "errors" in data
        if "data" in data:
            assert data["data"]["__schema"]["queryType"]["name"] == "Query"

    def test_graphql_requires_part_id_header(
        self, integration_client, integration_headers
    ):
        """GraphQL endpoint should require Part-Id header."""
        query = '{"query": "{ __schema { queryType { name } } }"}'
        resp = integration_client.post(
            "/test-ep/test-part/knowledge_graph_graphql",
            data=query,
            headers={
                **integration_headers,
                "Content-Type": "application/json",
                # No Part-Id header
            },
        )
        # Should return 400 (missing Part-Id) not 500
        assert resp.status_code == 400

    def test_graphql_requires_auth(self, integration_client):
        """GraphQL endpoint should reject requests without auth."""
        query = '{"query": "{ __schema { queryType { name } } }"}'
        resp = integration_client.post(
            "/test-ep/test-part/knowledge_graph_graphql",
            data=query,
            headers={
                "Content-Type": "application/json",
                "Part-Id": "test-part",
            },
        )
        assert resp.status_code == 401


class TestExtraction:
    """Test background extraction via dispatch_extract function."""

    def test_extract_submit(self, integration_client, integration_headers):
        """Submit an extraction job."""
        payload = json.dumps({"text": "John Smith works at Acme Corp in New York."})
        resp = integration_client.post(
            "/test-ep/test-part/extract",
            data=payload,
            headers={
                **integration_headers,
                "Content-Type": "application/json",
                "Part-Id": "test-part",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "pending"

    def test_extract_requires_part_id(self, integration_client, integration_headers):
        """Extraction should require Part-Id header."""
        payload = json.dumps({"text": "test"})
        resp = integration_client.post(
            "/test-ep/test-part/extract",
            data=payload,
            headers={
                **integration_headers,
                "Content-Type": "application/json",
                # No Part-Id header
            },
        )
        assert resp.status_code == 400

    def test_extract_requires_auth(self, integration_client):
        """Extraction should reject requests without auth."""
        payload = json.dumps({"text": "test"})
        resp = integration_client.post(
            "/test-ep/test-part/extract",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Part-Id": "test-part",
            },
        )
        assert resp.status_code == 401


class TestTaskStatus:
    """Test the universal task status endpoint."""

    def test_task_status_not_found(self, integration_client, integration_headers):
        """Status check for non-existent task should return 404."""
        resp = integration_client.get(
            "/test-ep/test-part/extract/status/nonexistent-task-id",
            headers=integration_headers,
        )
        assert resp.status_code == 404

    def test_task_status_requires_auth(self, integration_client):
        """Task status should reject requests without auth."""
        resp = integration_client.get(
            "/test-ep/test-part/extract/status/some-task-id",
        )
        assert resp.status_code == 401


class TestRouteManifest:
    """Test that all routes from manifest are registered."""

    def test_all_manifest_routes_registered(self, integration_app):
        """Verify all expected routes from the default manifest are present."""
        paths = [r.path for r in integration_app.routes if hasattr(r, "path")]
        assert "/health" in paths
        assert "/auth/token" in paths
        assert "/me" in paths
        assert "/{endpoint_id}/{part_id}/knowledge_graph_graphql" in paths
        assert "/{endpoint_id}/{part_id}/extract" in paths
        assert "/{endpoint_id}/{part_id}/extract/status/{task_id}" in paths