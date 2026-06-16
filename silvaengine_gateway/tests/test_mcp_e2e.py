#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end integration tests for MCP Daemon Engine through the SilvaEngine Gateway.

Tests the full MCP lifecycle across all three transports (GraphQL, REST/JSON-RPC,
SSE): health, auth, GraphQL queries (ping, functions, tools, resources, prompts,
modules, settings, calls), mutations (loadMcpConfiguration, processMcpPackage,
syncExternalMcpServer), REST JSON-RPC (initialize, tools/list, tools/call,
mcp_info), SSE (GET connect, POST initialize, POST tools/list, POST tools/call),
and auth edge cases.

Prerequisites:
    - Gateway running: python -m silvaengine_gateway.tests.run_daemon
    - DynamoDB tables exist (initialize_tables=1 in .env)
    - MCP configuration loaded (or test will attempt loadMcpConfiguration)

Usage:
    # pytest (recommended):
    pytest silvaengine_gateway/tests/test_mcp_e2e.py -v

    # Run specific test groups:
    python -m silvaengine_gateway.tests.test_mcp_e2e --only graphql
    python -m silvaengine_gateway.tests.test_mcp_e2e --only rest
    python -m silvaengine_gateway.tests.test_mcp_e2e --only sse
    python -m silvaengine_gateway.tests.test_mcp_e2e --only mutation
    python -m silvaengine_gateway.tests.test_mcp_e2e --only external

    # Run with custom base URL:
    python -m silvaengine_gateway.tests.test_mcp_e2e --base-url http://localhost:8765

    # Verbose output:
    python -m silvaengine_gateway.tests.test_mcp_e2e -v

All connection params are read from the .env file in the same directory.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Ensure project roots are on sys.path ───────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_MCP_ROOT = str(
    Path(__file__).resolve().parent.parent.parent.parent / "mcp_daemon_engine"
)
for _p in [_PROJECT_ROOT, _MCP_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Load .env before any silvaengine imports ───────────────────────────
_ENV_FILE = str(Path(__file__).resolve().parent / ".env")
if Path(_ENV_FILE).exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE, override=True)

import requests


def _promote_editable_finders() -> None:
    """Move _EditableFinder entries above PathFinder in sys.meta_path."""
    import sys as _sys
    from importlib.machinery import PathFinder

    meta_path = _sys.meta_path
    editable = [
        f
        for f in meta_path
        if hasattr(f, "__name__") and f.__name__ == "_EditableFinder"
    ]
    if not editable:
        return
    pf_index = next((i for i, f in enumerate(meta_path) if f is PathFinder), None)
    if pf_index is None:
        return
    if all(meta_path.index(f) < pf_index for f in editable):
        return
    for f in editable:
        meta_path.remove(f)
    for i, f in enumerate(meta_path):
        if f is PathFinder:
            pf_index = i
            break
    for f in reversed(editable):
        meta_path.insert(pf_index, f)


_promote_editable_finders()


# ═══════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════

BASE_URL = os.getenv("BASE_URL", "http://localhost:8765")
ENDPOINT_ID = os.getenv("endpoint_id", "gpt")
PART_ID = os.getenv("part_id", "nestaging")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# ── GraphQL field fragments (match Graphene camelCase output) ──────────

FUNCTION_FIELDS = "name mcpType description moduleName className functionName returnType isAsync"

FUNCTION_CALL_FIELDS = "mcpFunctionCallUuid mcpType name status timeSpent createdAt updatedAt"

MODULE_FIELDS = "moduleName packageName source updatedAt"

SETTING_FIELDS = "settingId updatedAt"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _graphql_url(base_url=None, endpoint_id=None, part_id=None):
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/mcp_daemon_graphql"


def _rest_url(base_url=None, endpoint_id=None, part_id=None):
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/mcp"


def _sse_url(base_url=None, endpoint_id=None, part_id=None):
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/sse"


def _mcp_info_url(base_url=None, endpoint_id=None, part_id=None):
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/mcp_info"


def _auth_headers(token, part_id=None):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id or PART_ID,
    }


def _get_token(base_url=None, username=None, password=None):
    base_url = base_url or BASE_URL
    username = username or ADMIN_USERNAME
    password = password or ADMIN_PASSWORD
    resp = requests.post(
        f"{base_url}/auth/token",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _post_graphql(token, query, variables=None, base_url=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        _graphql_url(base_url),
        json=payload,
        headers=_auth_headers(token),
        timeout=60,
    )
    return resp


def _rest_rpc(token, method, params=None, rpc_id=1, timeout=30):
    payload = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params:
        payload["params"] = params
    resp = requests.post(
        _rest_url(), json=payload, headers=_auth_headers(token), timeout=timeout,
    )
    return resp


def _sse_post(token, method, params=None, rpc_id=1, timeout=30):
    payload = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params:
        payload["params"] = params
    resp = requests.post(
        _sse_url(), json=payload, headers=_auth_headers(token), timeout=timeout,
    )
    return resp


# ═══════════════════════════════════════════════════════════════════════
#  Test suite (unittest / pytest compatible)
# ═══════════════════════════════════════════════════════════════════════


class TestMCPE2E(unittest.TestCase):
    """End-to-end tests for MCP Daemon routes through the gateway.

    Covers all three transports: GraphQL, REST/JSON-RPC, SSE.
    """

    @classmethod
    def setUpClass(cls):
        print(f"\n{'='*60}")
        print(f"  MCP E2E Integration Tests")
        print(f"  Gateway: {BASE_URL}")
        print(f"  Endpoint: {ENDPOINT_ID} / Partition: {PART_ID}")
        print(f"  .env: {_ENV_FILE}")
        print(f"{'='*60}\n")

        # Verify gateway is reachable
        try:
            health = requests.get(f"{BASE_URL}/health", timeout=5)
            health.raise_for_status()
            print(f"  Health check: {health.json()}")
        except Exception as e:
            raise RuntimeError(
                f"Gateway not reachable at {BASE_URL}. "
                f"Start it with: python -m silvaengine_gateway.tests.run_daemon\n{e}"
            )

        # Authenticate
        print(f"  Authenticating as {ADMIN_USERNAME}...")
        cls.token = _get_token()
        print(f"  Token acquired ({len(cls.token)} chars)\n")

    # ── Health & Auth ─────────────────────────────────────────────────

    def test_01_health_no_auth(self):
        """Health endpoint should be accessible without auth."""
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "silvaengine-gateway")

    def test_02_auth_token(self):
        """Local auth should return a valid JWT."""
        resp = requests.post(
            f"{BASE_URL}/auth/token",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("access_token", data)
        self.assertEqual(data["token_type"], "bearer")

    def test_03_auth_invalid_password(self):
        """Invalid password should return 401."""
        resp = requests.post(
            f"{BASE_URL}/auth/token",
            data={"username": ADMIN_USERNAME, "password": "wrong"},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 401)

    def test_04_me_endpoint(self):
        """/me endpoint returns user claims."""
        resp = requests.get(
            f"{BASE_URL}/me",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("username", resp.json())

    def test_05_graphql_requires_auth(self):
        """GraphQL endpoint should reject unauthenticated requests."""
        resp = requests.post(
            _graphql_url(),
            json={"query": "{ ping }"},
            headers={"Content-Type": "application/json", "Part-Id": PART_ID},
            timeout=10,
        )
        self.assertIn(resp.status_code, [401, 403])

    # ── GraphQL Queries ───────────────────────────────────────────────

    def test_06_graphql_ping(self):
        """Ping query should return a greeting string."""
        resp = _post_graphql(self.token, "{ ping }")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        ping = data.get("data", {}).get("ping")
        self.assertIsNotNone(ping)
        self.assertIn("Hello", ping)
        print(f"  ping → {ping}")

    def test_07_graphql_function_list_all(self):
        """mcpFunctionList (no filter) should return all MCP functions."""
        query = (
            "{ mcpFunctionList { mcpFunctionList { %s } total pageNumber pageSize } }"
            % FUNCTION_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")

        result = data["data"]["mcpFunctionList"]
        functions = result["mcpFunctionList"]
        total = result["total"]
        self.assertGreater(total, 0, "Expected at least one MCP function")
        print(f"  mcpFunctionList → {total} functions")
        for fn in functions[:3]:
            print(f"    [{fn['name']}] type={fn['mcpType']} module={fn['moduleName']}")

    def test_08_graphql_function_list_tools(self):
        """mcpFunctionList(mcpType='tool') should return only tools."""
        query = (
            '{ mcpFunctionList(mcpType: "tool") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpFunctionList"]
        functions = result["mcpFunctionList"]
        total = result["total"]
        self.assertGreater(total, 0, "Expected at least one tool")
        for fn in functions:
            self.assertEqual(fn["mcpType"], "tool",
                            f"Expected mcpType='tool' but got '{fn['mcpType']}' for {fn['name']}")
        print(f"  mcpFunctionList(tools) → {total} tools")

    def test_09_graphql_function_list_resources(self):
        """mcpFunctionList(mcpType='resource') should return only resources."""
        query = (
            '{ mcpFunctionList(mcpType: "resource") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        print(f"  mcpFunctionList(resources) → {total} resources")

    def test_10_graphql_function_list_prompts(self):
        """mcpFunctionList(mcpType='prompt') should return only prompts."""
        query = (
            '{ mcpFunctionList(mcpType: "prompt") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        print(f"  mcpFunctionList(prompts) → {total} prompts")

    def test_11_graphql_module_list(self):
        """mcpModuleList should return MCP modules."""
        query = (
            "{ mcpModuleList { mcpModuleList { %s } total pageNumber pageSize } }"
            % MODULE_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpModuleList"]
        modules = result["mcpModuleList"]
        total = result["total"]
        self.assertGreater(total, 0, "Expected at least one MCP module")
        print(f"  mcpModuleList → {total} modules")
        for m in modules:
            print(f"    [{m['moduleName']}] source={m.get('source', 'local')}")

    def test_12_graphql_setting_list(self):
        """mcpSettingList should return MCP settings."""
        query = (
            "{ mcpSettingList { mcpSettingList { %s } total pageNumber pageSize } }"
            % SETTING_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpSettingList"]
        total = result["total"]
        print(f"  mcpSettingList → {total} settings")

    def test_13_graphql_function_call_list(self):
        """mcpFunctionCallList should return function call history."""
        query = (
            "{ mcpFunctionCallList { mcpFunctionCallList { %s } total pageNumber pageSize } }"
            % FUNCTION_CALL_FIELDS
        )
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpFunctionCallList"]
        total = result["total"]
        print(f"  mcpFunctionCallList → {total} calls")

    def test_14_graphql_bad_query(self):
        """Malformed GraphQL query should return errors, not 500."""
        resp = _post_graphql(self.token, "{ thisFieldDoesNotExist }")
        if resp.status_code == 200:
            errors = resp.json().get("errors")
            self.assertTrue(errors is not None and len(errors) > 0,
                           "Expected errors for malformed query")
        else:
            self.assertEqual(resp.status_code, 400,
                            f"Expected 200-with-errors or 400, got {resp.status_code}")
        print(f"  graphql bad query → handled gracefully")

    # ── GraphQL Mutations: loadMcpConfiguration ───────────────────────

    def test_17_graphql_load_mcp_configuration(self):
        """loadMcpConfiguration with moduleName to reload existing config from DynamoDB.

        This is safe — it re-reads the existing MCP configuration from the DB
        and rebuilds the in-process cache, so it's idempotent for already-loaded modules.
        """
        # Find a non-external module name from mcpModuleList
        list_query = "{ mcpModuleList { mcpModuleList { moduleName source } } }"
        resp = _post_graphql(self.token, list_query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"Pre-query errors: {data.get('errors')}")

        modules = data.get("data", {}).get("mcpModuleList", {}).get("mcpModuleList", [])
        if not modules:
            self.skipTest("No modules found — empty DB is valid but nothing to reload")

        # Prefer a local (non-external) module for reload
        local_modules = [m for m in modules if m.get("source") != "external"]
        if local_modules:
            module_name = local_modules[0]["moduleName"]
            is_external = False
        else:
            module_name = modules[0]["moduleName"]
            is_external = modules[0].get("source") == "external"

        mutation = """mutation {
            loadMcpConfiguration(
                moduleName: "%s",
                updatedBy: "e2e_test"
            ) {
                ok
                message
                stats {
                    tools resources prompts modules settings
                }
            }
        }""" % module_name

        resp = _post_graphql(self.token, mutation)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")

        result = data.get("data", {}).get("loadMcpConfiguration", {})
        ok = result.get("ok")
        message = result.get("message", "")

        # External modules can't be imported by moduleName — expected, not failure
        if not ok and is_external and "No module named" in message:
            print(f"  loadMcpConfiguration: ok=false (expected: external module '{module_name}' not importable)")
            return

        self.assertTrue(ok, f"loadMcpConfiguration failed: ok={ok}, message={message}")
        stats = result.get("stats")
        if stats:
            print(f"  loadMcpConfiguration: ok=true, stats={json.dumps(stats)}")
        else:
            print(f"  loadMcpConfiguration: ok=true, message={message}")

    # ── GraphQL Mutations: processMcpPackage ──────────────────────────

    def test_18_graphql_process_mcp_package(self):
        """End-to-end: presign → PUT ZIP to S3 → processMcpPackage.

        Skips if S3 bucket is not configured in the gateway context
        (FUNCT_BUCKET_NAME must reach the daemon through the gateway config).
        """
        package_name = "mcp_resolvepay_connector"
        module_name = "mcp_resolvepay_connector"
        zip_path = os.getenv("MCP_TEST_PACKAGE_ZIP")

        if not zip_path:
            self.skipTest("MCP_TEST_PACKAGE_ZIP not set in .env")

        zip_path = Path(zip_path)
        if not zip_path.exists():
            self.skipTest(f"ZIP not found at {zip_path}")

        zip_bytes = zip_path.read_bytes()
        print(f"  Loaded ZIP: {zip_path} ({len(zip_bytes)} bytes)")

        # 1) generateMcpPackageUploadUrl
        presign_query = """
        mutation GenerateUploadUrl($packageName: String!) {
            generateMcpPackageUploadUrl(packageName: $packageName) {
                ok
                message
                uploadUrl
                s3Key
                expiresAt
            }
        }
        """
        resp = _post_graphql(self.token, presign_query, variables={"packageName": package_name})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")
        presign = data["data"]["generateMcpPackageUploadUrl"]

        if not presign["ok"]:
            self.skipTest(
                f"Presign failed (S3 not configured through gateway context): "
                f"{presign.get('message', 'unknown')}"
            )

        upload_url = presign["uploadUrl"]
        s3_key = presign["s3Key"]
        print(f"  Presigned URL: s3Key={s3_key}")

        # 2) PUT ZIP to S3
        put = requests.put(
            upload_url, data=zip_bytes, headers={"Content-Type": "application/zip"}, timeout=30,
        )
        self.assertEqual(put.status_code, 200, f"S3 PUT failed: {put.status_code} {put.text[:500]}")
        print(f"  S3 PUT: {put.status_code}")

        # 3) processMcpPackage
        process_query = """
        mutation ProcessPackage(
            $s3Key: String!,
            $moduleName: String!,
            $packageName: String!,
            $source: String,
            $variables: JSONCamelCase,
            $updatedBy: String!
        ) {
            processMcpPackage(
                s3Key: $s3Key,
                moduleName: $moduleName,
                packageName: $packageName,
                source: $source,
                variables: $variables,
                updatedBy: $updatedBy
            ) {
                ok
                message
                stats {
                    tools
                    resources
                    prompts
                    modules
                    settings
                }
            }
        }
        """
        process_vars = {
            "s3Key": s3_key,
            "moduleName": module_name,
            "packageName": package_name,
            "source": "s3",
            "variables": {
                "api_key": "6FGeehtyssGTxBFJqWeaxit0ahZlSMCN",
                "base_url": "https://app-sandbox.resolvepay.com/api/",
                "merchant_id": "newedgeaisandbox",
            },
            "updatedBy": "e2e_test",
        }
        resp = _post_graphql(self.token, process_query, variables=process_vars)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")

        process = data["data"]["processMcpPackage"]
        self.assertTrue(process["ok"], f"processMcpPackage failed: {process}")
        self.assertIsNotNone(process.get("stats"))

        stats = process["stats"]
        print(
            f"  processMcpPackage OK: "
            f"{stats['tools']} tools, {stats['resources']} resources, "
            f"{stats['prompts']} prompts, {stats['modules']} modules, "
            f"{stats['settings']} settings"
        )

    def test_19_graphql_process_mcp_package_invalid_name(self):
        """Negative path: invalid package name must be rejected."""
        query = """
        mutation ProcessPackage(
            $s3Key: String!,
            $moduleName: String!,
            $packageName: String!,
            $updatedBy: String!
        ) {
            processMcpPackage(
                s3Key: $s3Key,
                moduleName: $moduleName,
                packageName: $packageName,
                updatedBy: $updatedBy
            ) {
                ok
                message
            }
        }
        """
        resp = _post_graphql(
            self.token, query,
            variables={
                "s3Key": "bad/name.zip",
                "moduleName": "bad/name",
                "packageName": "bad/name",
                "updatedBy": "e2e_test",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        parsed = data["data"]["processMcpPackage"]
        self.assertFalse(parsed["ok"])
        self.assertIn("Invalid package name", parsed["message"])
        print(f"  Invalid name rejected: {parsed['message']}")

    # ── GraphQL Mutation: syncExternalMcpServer ──────────────────────

    def test_20_graphql_sync_external_mcp_server(self):
        """Sync a remote MCP HTTP server via the gateway GraphQL proxy.

        Requires MCP_TEST_EXTERNAL_BASE_URL in .env.
        """
        base_url_env = os.getenv("MCP_TEST_EXTERNAL_BASE_URL")
        if not base_url_env:
            self.skipTest("MCP_TEST_EXTERNAL_BASE_URL not set in .env")

        server_name = os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "test_external")
        bearer_token = os.getenv("MCP_TEST_EXTERNAL_BEARER") or None
        name_prefix = os.getenv("MCP_TEST_EXTERNAL_NAME_PREFIX") or None

        variables = {
            "serverName": server_name,
            "baseUrl": base_url_env,
            "updatedBy": "e2e_test",
        }
        if bearer_token:
            variables["bearerToken"] = bearer_token
        if name_prefix:
            variables["namePrefix"] = name_prefix

        query = """
        mutation SyncExternal(
            $serverName: String!,
            $baseUrl: String!,
            $updatedBy: String!,
            $bearerToken: String,
            $namePrefix: String
        ) {
            syncExternalMcpServer(
                serverName: $serverName,
                baseUrl: $baseUrl,
                updatedBy: $updatedBy,
                bearerToken: $bearerToken,
                namePrefix: $namePrefix
            ) {
                ok
                message
                stats {
                    tools
                    resources
                    prompts
                    modules
                    settings
                }
            }
        }
        """
        resp = _post_graphql(self.token, query, variables=variables)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")

        parsed = data["data"]["syncExternalMcpServer"]
        self.assertTrue(parsed["ok"], f"syncExternalMcpServer failed: {parsed}")
        self.assertIsNotNone(parsed.get("stats"))

        stats = parsed["stats"]
        total_items = stats["tools"] + stats["resources"] + stats["prompts"]
        self.assertGreater(
            total_items, 0,
            "Remote server returned an empty inventory — check MCP_TEST_EXTERNAL_BASE_URL",
        )
        print(
            f"  Synced {server_name}: {stats['tools']} tools, "
            f"{stats['resources']} resources, {stats['prompts']} prompts, "
            f"{stats['modules']} modules, {stats['settings']} settings"
        )

    def test_21_sync_external_invalid_name(self):
        """Negative test: syncExternalMcpServer rejects invalid server name."""
        query = """
        mutation SyncExternal($serverName: String!, $baseUrl: String!,
                              $updatedBy: String!) {
            syncExternalMcpServer(
                serverName: $serverName
                baseUrl: $baseUrl
                updatedBy: $updatedBy
            ) {
                ok message
            }
        }
        """
        variables = {
            "serverName": "bad-name",
            "baseUrl": "https://example.com/mcp",
            "updatedBy": "e2e_test",
        }
        resp = _post_graphql(self.token, query, variables=variables)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        result = data.get("data", {}).get("syncExternalMcpServer", {})
        ok = result.get("ok", True)
        message = result.get("message", "")
        # We expect ok=False with "Invalid server name" in message
        self.assertFalse(ok, f"Expected ok=False for invalid name, got ok={ok}")
        self.assertIn("Invalid server name", message)
        print(f"  Invalid server name rejected: {message[:200]}")

    # ── External MCP Proxy: verify registration ──────────────────────

    def test_22_verify_external_tools_registered(self):
        """After syncExternalMcpServer, verify external tools appear in mcpFunctionList."""
        server_name = os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "shopify_demo")
        if not os.getenv("MCP_TEST_EXTERNAL_BASE_URL"):
            self.skipTest("MCP_TEST_EXTERNAL_BASE_URL not set")

        query = '{ mcpFunctionList(mcpType: "tool") { mcpFunctionList { name moduleName mcpType } total } }'
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        items = data["data"]["mcpFunctionList"]["mcpFunctionList"]
        external_tools = [t for t in items if t.get("moduleName") == server_name]

        if not external_tools:
            self.fail(f"No tools found for module '{server_name}'")

        self.assertTrue(all(t.get("mcpType") == "tool" for t in external_tools))
        print(f"  External tools registered: {len(external_tools)} for module '{server_name}'")

    def test_23_verify_external_module_registered(self):
        """After syncExternalMcpServer, verify module appears with source=external."""
        server_name = os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "shopify_demo")
        if not os.getenv("MCP_TEST_EXTERNAL_BASE_URL"):
            self.skipTest("MCP_TEST_EXTERNAL_BASE_URL not set")

        query = '{ mcpModuleList { mcpModuleList { moduleName packageName source updatedAt } total } }'
        resp = _post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        modules = data["data"]["mcpModuleList"]["mcpModuleList"]
        external = [m for m in modules if m.get("moduleName") == server_name]

        if not external:
            self.fail(f"Module '{server_name}' not found in module list")

        self.assertEqual(external[0].get("source"), "external")
        print(f"  External module '{server_name}' registered with source=external")

    # ── External MCP Proxy: tools/call ──────────────────────────────

    def test_24_rest_call_external_tool(self):
        """Invoke an external tool via REST JSON-RPC tools/call through the gateway.

        Mirrors test_mcp_call_external_search_product in mcp_daemon_engine:
            agent -> gateway REST /mcp -> mcp_server.call_tool
                  -> execute_tool_function -> ExternalMCPProxy.call_tool
                  -> MCPHttpClient -> upstream Shopify MCP server

        Requires MCP_TEST_EXTERNAL_BASE_URL in .env and that
        syncExternalMcpServer (test_20) has already run for this partition.
        """
        if not os.getenv("MCP_TEST_EXTERNAL_BASE_URL"):
            self.skipTest("MCP_TEST_EXTERNAL_BASE_URL not set in .env")

        server_name = os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "shopify_demo")
        tool_name = os.getenv("MCP_TEST_EXTERNAL_TOOL_NAME", "search_shop_policies_and_faqs")
        query_text = os.getenv("MCP_TEST_EXTERNAL_TOOL_QUERY", "shirt")

        # Resolve the actual tool name from the live mcpFunctionList
        list_query = (
            '{ mcpFunctionList(mcpType: "tool") { mcpFunctionList { name moduleName } total } }'
        )
        resp = _post_graphql(self.token, list_query)
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("errors"):
                items = data["data"]["mcpFunctionList"]["mcpFunctionList"]
                external_tools = [t for t in items if t["moduleName"] == server_name]
                if external_tools:
                    env_match = next(
                        (t for t in external_tools if t["name"] == tool_name), None
                    )
                    # If no exact match, try to resolve from REST tools/list schemas
                    if not env_match:
                        try:
                            tools_resp = _rest_rpc(self.token, "tools/list", rpc_id=99)
                            all_tools = tools_resp.json().get("result", {}).get("tools", [])
                            for t in all_tools:
                                schema = t.get("inputSchema", {})
                                props = schema.get("properties", {})
                                if "query" in props and any(
                                    e["name"] == t["name"] for e in external_tools
                                ):
                                    tool_name = t["name"]
                                    break
                        except Exception:
                            pass
                    else:
                        tool_name = env_match["name"]

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 42,
            "params": {
                "name": tool_name,
                "arguments": {"query": query_text},
            },
        }
        resp = requests.post(_rest_url(), json=payload, headers=_auth_headers(self.token), timeout=60)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()

        # Check for JSON-RPC error
        if data.get("error"):
            err = data["error"]
            err_msg = err.get("message", "")
            err_data = err.get("data", "")
            if "Unknown tool" in str(err_data) or "Unknown tool" in err_msg:
                self.fail(
                    f"Unknown tool '{tool_name}' — syncExternalMcpServer may "
                    f"register tools with their original upstream names. "
                    f"Check MCP_TEST_EXTERNAL_TOOL_NAME. Error: {err_data or err_msg}"
                )
            # Other -32603 = gateway routing OK but upstream error
            self.skipTest(
                f"Gateway routing OK but upstream returned error: "
                f"[{err.get('code')}] {err_msg}"
            )

        result = data.get("result", {})
        content = result.get("content", [])
        self.assertTrue(
            content,
            f"Upstream returned no content. Full response: {json.dumps(data)[:500]}",
        )

        first = content[0]
        self.assertIn("text", first, f"Expected 'text' in content[0]: {first}")
        print(f"  tools/call '{tool_name}' → content_items={len(content)}")
        print(f"  first_text: {first['text'][:300]}")

    # ── REST (JSON-RPC) Endpoints ─────────────────────────────────────

    def test_25_rest_initialize(self):
        """REST JSON-RPC initialize should return capabilities."""
        resp = _rest_rpc(
            self.token, "initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "1.0"},
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("result", data)
        result = data["result"]
        self.assertIn("capabilities", result)
        print(f"  REST initialize → protocol={result.get('protocolVersion')}")

    def test_26_rest_tools_list(self):
        """REST JSON-RPC tools/list should return tool inventory."""
        resp = _rest_rpc(self.token, "tools/list")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("result", data)
        tools = data["result"].get("tools", [])
        self.assertGreater(len(tools), 0, "Expected at least one tool from tools/list")
        print(f"  REST tools/list → {len(tools)} tools")

    def test_27_rest_tools_list_has_schemas(self):
        """Each tool from tools/list should have inputSchema."""
        resp = _rest_rpc(self.token, "tools/list")
        self.assertEqual(resp.status_code, 200)
        tools = resp.json().get("result", {}).get("tools", [])
        if not tools:
            self.skipTest("No tools returned — nothing to check schemas for")

        missing_schema = [t.get("name", "?") for t in tools if "inputSchema" not in t]
        self.assertEqual(
            len(missing_schema), 0,
            f"Tools missing inputSchema: {missing_schema}",
        )
        print(f"  All {len(tools)} tools have inputSchema")

    def test_28_rest_mcp_info(self):
        """REST /mcp_info should return MCP configuration summary."""
        url = _mcp_info_url()
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(url, headers=headers, timeout=30)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("server", data)
        self.assertIn("tools", data)
        print(f"  mcp_info → server={data.get('server')}, {len(data.get('tools', []))} tools")

    # ── MCP Tool Call (ResolvePay search_customers) ────────────────────

    def test_15_rest_tool_call_search_customers(self):
        """Call search_customers tool via REST JSON-RPC through the gateway.

        Tests the full dispatch path:
            client → gateway /mcp → dispatch_mcp → MCPDaemonEngine.mcp
                  → execute_tool_function → MCPResolvepayConnector.search_customers
                  → ResolvePay sandbox API

        Uses the arguments: {"business_ap_email": "bibo72@outlook.com"}.

        The connector is loaded from S3 (funct_bucket_name) and instantiated
        with settings from DynamoDB (normalized via _normalize_setting_keys
        to convert camelCase → snake_case). If the external ResolvePay API
        is unreachable (DNS/network), the tool still returns a valid JSON
        result with success=false rather than a JSON-RPC error.
        """
        resp = _rest_rpc(
            self.token, "tools/call",
            params={
                "name": "search_customers",
                "arguments": {"business_ap_email": "bibo72@outlook.com"},
            },
            rpc_id=100,
            timeout=60,
        )
        self.assertEqual(resp.status_code, 200, f"HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        if data.get("error"):
            err = data["error"]
            err_msg = f"[{err.get('code')}] {err.get('message')} — {err.get('data')}"
            self.fail(
                f"tools/call 'search_customers' returned JSON-RPC error: {err_msg}"
            )

        result = data.get("result", {})
        content = result.get("content", [])
        self.assertTrue(
            content,
            f"search_customers returned no content. Full response: {json.dumps(data)[:500]}",
        )

        first = content[0]
        self.assertIn("text", first, f"Expected 'text' in content[0]: {first}")
        # The connector returns a JSON body even on external API failure
        body = json.loads(first["text"]) if first.get("text") else {}
        print(f"  tools/call 'search_customers' → success={body.get('success')}, "
              f"total={body.get('total', 'N/A')}")
        if not body.get("success") and body.get("error"):
            print(f"  (external API error: {body['error'][:100]})")

    # ── SSE Endpoints ─────────────────────────────────────────────────

    def test_29_sse_get_connect(self):
        """SSE GET endpoint should establish a streaming connection and
        deliver an initial 'connected' event.

        This was previously a 503 (SSEManager instance rejected by
        resolve_dispatch which required callables). Fixed by adding
        _resolve_ref(require_callable=False) in router_builder.
        """
        url = _sse_url()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
        }
        # Use stream=True with a very short read timeout to grab the first
        # frame without blocking the test runner on the long-lived stream.
        resp = requests.get(url, headers=headers, timeout=(5, 3), stream=True)
        self.assertEqual(
            resp.status_code, 200,
            f"SSE GET should return 200, got {resp.status_code}",
        )

        # Read the first SSE frame — must be 'event: connected'
        first_line = next(resp.iter_lines(decode_unicode=True), None)
        resp.close()  # Close stream immediately after reading first event
        self.assertIsNotNone(first_line, "SSE stream emitted no data")
        self.assertIn(
            "event: connected", first_line,
            f"First SSE event should be 'connected', got: {first_line}",
        )
        print(f"  SSE GET → 200 connected")

    def test_30_sse_post_initialize(self):
        """SSE POST (JSON-RPC) initialize via /sse POST endpoint.

        Tests the dispatch path:
            client → gateway POST /sse → dispatch_sse_message
                  → MCPDaemonEngine.sse_message → mcp_server.handle_initialize
        """
        resp = _sse_post(
            self.token, "initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-sse-test", "version": "1.0"},
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("error", data, f"JSON-RPC error: {data.get('error')}")
        result = data["result"]
        self.assertIn("capabilities", result)
        self.assertIn("protocolVersion", result)
        print(f"  SSE POST initialize → protocol={result.get('protocolVersion')}")

    def test_31_sse_post_tools_list(self):
        """SSE POST (JSON-RPC) tools/list via /sse POST endpoint."""
        resp = _sse_post(self.token, "tools/list")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("error", data, f"JSON-RPC error: {data.get('error')}")
        tools = data["result"].get("tools", [])
        self.assertGreater(len(tools), 0, "Expected at least one tool")
        print(f"  SSE POST tools/list → {len(tools)} tools")

    def test_16_sse_post_tools_call_search_customers(self):
        """SSE POST (JSON-RPC) tools/call search_customers via /sse POST endpoint.

        Tests the full dispatch path:
            client → gateway POST /sse → dispatch_sse_message
                  → MCPDaemonEngine.sse_message → execute_tool_function
                  → MCPResolvepayConnector.search_customers
                  → ResolvePay sandbox API

        Uses the arguments: {"business_ap_email": "bibo72@outlook.com"}.

        The connector is loaded from S3 (funct_bucket_name) and instantiated
        with settings from DynamoDB (normalized via _normalize_setting_keys
        to convert camelCase → snake_case). If the external ResolvePay API
        is unreachable (DNS/network), the tool still returns a valid JSON
        result with success=false rather than a JSON-RPC error.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 100,
            "params": {
                "name": "search_customers",
                "arguments": {"business_ap_email": "bibo72@outlook.com"},
            },
        }
        resp = requests.post(_sse_url(), json=payload, headers=_auth_headers(self.token), timeout=60)
        self.assertEqual(resp.status_code, 200, f"HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        if data.get("error"):
            err = data["error"]
            err_msg = f"[{err.get('code')}] {err.get('message')} — {err.get('data')}"
            self.fail(
                f"tools/call 'search_customers' via SSE returned JSON-RPC error: {err_msg}"
            )

        result = data.get("result", {})
        content = result.get("content", [])
        self.assertTrue(
            content,
            f"search_customers via SSE returned no content. Full: {json.dumps(data)[:500]}",
        )
        first = content[0]
        self.assertIn("text", first, f"Expected 'text' in content[0]: {first}")
        # The connector returns a JSON body even on external API failure
        body = json.loads(first["text"]) if first.get("text") else {}
        print(f"  SSE POST tools/call 'search_customers' → success={body.get('success')}, "
              f"total={body.get('total', 'N/A')}")
        if not body.get("success") and body.get("error"):
            print(f"  (external API error: {body['error'][:100]})")

    # ── GraphQL Raw Query ─────────────────────────────────────────────

    def test_32_graphql_raw_query(self):
        """Raw GraphQL query string should work like structured queries."""
        raw = '{"query": "{ mcpFunctionList { mcpFunctionList { name mcpType } total } }"}'
        resp = requests.post(
            _graphql_url(),
            data=raw,
            headers=_auth_headers(self.token),
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        self.assertGreater(total, 0)
        print(f"  Raw GraphQL query → {total} functions")


# ═══════════════════════════════════════════════════════════════════════
# CLI entry point (preserves --only group filtering)
# ═══════════════════════════════════════════════════════════════════════

# Map --only group names to test method name prefixes
_GROUP_PREFIXES = {
    "health":   ["test_01", "test_02", "test_03", "test_04", "test_05"],
    "auth":     ["test_02", "test_03", "test_04", "test_05"],
    "graphql":  ["test_06", "test_07", "test_08", "test_09", "test_10",
                 "test_11", "test_12", "test_13", "test_14", "test_32"],
    "mutation": ["test_17", "test_18", "test_19", "test_20", "test_21"],
    "rest":     ["test_15", "test_25", "test_26", "test_27", "test_28"],
    "external": ["test_20", "test_21", "test_22", "test_23", "test_24"],
    "sse":      ["test_16", "test_29", "test_30", "test_31"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end integration tests for MCP Daemon through SilvaEngine Gateway"
    )
    parser.add_argument(
        "--base-url", type=str, default=None,
        help="Gateway base URL (default: from .env or http://localhost:8765)",
    )
    parser.add_argument(
        "--dotenv", type=str, default=None,
        help="Path to .env file (default: <this_script_dir>/.env)",
    )
    parser.add_argument(
        "--username", type=str, default=None,
        help="Auth username (default: from .env ADMIN_USERNAME)",
    )
    parser.add_argument(
        "--password", type=str, default=None,
        help="Auth password (default: from .env ADMIN_PASSWORD)",
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Pre-existing JWT token (skips auth)",
    )
    parser.add_argument(
        "--endpoint-id", type=str, default=None,
        help="Endpoint ID (default: from .env endpoint_id)",
    )
    parser.add_argument(
        "--part-id", type=str, default=None,
        help="Partition ID (default: from .env part_id)",
    )
    parser.add_argument(
        "--only", type=str, nargs="+",
        choices=["health", "auth", "graphql", "mutation", "rest", "external", "sse"],
        default=None,
        help="Run only specified test groups",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── Load .env ────────────────────────────────────────────────────
    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"WARNING: .env file not found at {env_file}")
        print("Some values will use hardcoded defaults.")
        print("Copy .env.example to .env and fill in real values.\n")
    else:
        load_dotenv(env_file, override=True)
        print(f"Loaded .env from: {env_file}")

    # ── Apply CLI overrides to module-level config ───────────────────
    global BASE_URL, ENDPOINT_ID, PART_ID, ADMIN_USERNAME, ADMIN_PASSWORD
    if args.base_url:
        BASE_URL = args.base_url
    else:
        BASE_URL = os.getenv("BASE_URL", BASE_URL)
    if args.endpoint_id:
        ENDPOINT_ID = args.endpoint_id
    else:
        ENDPOINT_ID = os.getenv("endpoint_id", ENDPOINT_ID)
    if args.part_id:
        PART_ID = args.part_id
    else:
        PART_ID = os.getenv("part_id", PART_ID)
    if args.username:
        ADMIN_USERNAME = args.username
    else:
        ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", ADMIN_USERNAME)
    if args.password:
        ADMIN_PASSWORD = args.password
    else:
        ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", ADMIN_PASSWORD)

    if args.token:
        TestMCPE2E.token = args.token

    # ── Build test suite ─────────────────────────────────────────────
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    if args.only:
        # Filter tests by group
        prefixes = set()
        for group in args.only:
            prefixes.update(_GROUP_PREFIXES.get(group, []))
        # Always include auth prerequisites
        prefixes.update(_GROUP_PREFIXES["auth"])
        prefixes.update(_GROUP_PREFIXES["health"])

        all_tests = loader.loadTestsFromTestCase(TestMCPE2E)
        for test_group in all_tests:
            for test in test_group:
                method_name = test._testMethodName
                if any(method_name.startswith(p) for p in prefixes):
                    suite.addTest(test)
    else:
        suite = loader.loadTestsFromTestCase(TestMCPE2E)

    verbosity = 2 if args.verbose else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()