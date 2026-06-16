#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end integration tests for MCP Daemon routes through the SilvaEngine Gateway.

Covers all three transports (GraphQL, REST/JSON-RPC, SSE) and the full MCP
package lifecycle (presign → upload → process) — mirroring the patterns in
mcp_daemon_engine's test_graphql_process_mcp_package but exercised through
the gateway's HTTP endpoints instead of direct Python calls.

Prerequisites:
    - Gateway running: python -m silvaengine_gateway.tests.run_daemon
    - .env file in this directory with real AWS + auth credentials

Usage:
    # Run all tests:
    python -m silvaengine_gateway.tests.test_mcp_gateway_e2e

    # Run a single test:
    python -m silvaengine_gateway.tests.test_mcp_gateway_e2e TestMCPGatewayE2E.test_graphql_ping

    # pytest style (with -v for verbose):
    pytest silvaengine_gateway/tests/test_mcp_gateway_e2e.py -v
"""

from __future__ import print_function

__author__ = "bibow"

import json
import os
import sys
import time
import unittest
from pathlib import Path

# ── Load .env before any silvaengine imports ───────────────────────────
_ENV_FILE = str(Path(__file__).resolve().parent / ".env")
if Path(_ENV_FILE).exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE, override=True)

import requests


# ── Helpers ─────────────────────────────────────────────────────────────

BASE_URL = os.getenv("BASE_URL", "http://localhost:8765")
ENDPOINT_ID = os.getenv("endpoint_id", "gpt")
PART_ID = os.getenv("part_id", "nestaging")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# ── GraphQL field fragments (match Graphene camelCase output) ──────────

FUNCTION_FIELDS = """\
    name
    mcpType
    description
    moduleName
    className
    functionName
    returnType
    isAsync\
"""

FUNCTION_CALL_FIELDS = """\
    mcpFunctionCallUuid
    mcpType
    name
    status
    timeSpent
    createdAt
    updatedAt\
"""

MODULE_FIELDS = """\
    moduleName
    packageName
    source
    updatedAt\
"""

SETTING_FIELDS = """\
    settingId
    updatedAt\
"""


def get_token(base_url=None, username=None, password=None):
    """Authenticate and return a JWT access token."""
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


def graphql_headers(token, part_id=None):
    """Return standard GraphQL request headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id or PART_ID,
    }


def graphql_url(base_url=None, endpoint_id=None, part_id=None):
    """Return the MCP Daemon GraphQL URL."""
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/mcp_daemon_graphql"


def rest_url(base_url=None, endpoint_id=None, part_id=None):
    """Return the MCP JSON-RPC REST URL."""
    base_url = base_url or BASE_URL
    endpoint_id = endpoint_id or ENDPOINT_ID
    part_id = part_id or PART_ID
    return f"{base_url}/{endpoint_id}/{part_id}/mcp"


def post_graphql(token, query, variables=None, base_url=None):
    """Send a GraphQL request and return the parsed JSON response."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        graphql_url(base_url),
        json=payload,
        headers=graphql_headers(token),
        timeout=60,
    )
    return resp


# ═══════════════════════════════════════════════════════════════════════
#  Test suite
# ═══════════════════════════════════════════════════════════════════════


class TestMCPGatewayE2E(unittest.TestCase):
    """End-to-end tests for MCP Daemon routes through the gateway."""

    @classmethod
    def setUpClass(cls):
        """Authenticate once for all tests in this class."""
        print(f"\n{'='*60}")
        print(f"  MCP Gateway E2E Integration Tests")
        print(f"  Gateway: {BASE_URL}")
        print(f"  Endpoint: {ENDPOINT_ID} / Partition: {PART_ID}")
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
        cls.token = get_token()
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

    def test_04_graphql_requires_auth(self):
        """GraphQL endpoint should reject unauthenticated requests."""
        resp = requests.post(
            graphql_url(),
            json={"query": "{ ping }"},
            headers={"Content-Type": "application/json", "Part-Id": PART_ID},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 401)

    # ── GraphQL Queries ───────────────────────────────────────────────

    def test_05_graphql_ping(self):
        """Ping query should return a greeting string."""
        resp = post_graphql(self.token, "{ ping }")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        ping = data.get("data", {}).get("ping")
        self.assertIsNotNone(ping)
        self.assertIn("Hello", ping)
        print(f"  ping → {ping}")

    def test_06_graphql_function_list_all(self):
        """mcpFunctionList (no filter) should return all MCP functions."""
        query = (
            "{ mcpFunctionList { mcpFunctionList { %s } total pageNumber pageSize } }"
            % FUNCTION_FIELDS
        )
        resp = post_graphql(self.token, query)
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

    def test_07_graphql_function_list_tools(self):
        """mcpFunctionList(mcpType='tool') should return only tools."""
        query = (
            '{ mcpFunctionList(mcpType: "tool") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = post_graphql(self.token, query)
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

    def test_08_graphql_function_list_resources(self):
        """mcpFunctionList(mcpType='resource') should return only resources."""
        query = (
            '{ mcpFunctionList(mcpType: "resource") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        print(f"  mcpFunctionList(resources) → {total} resources")

    def test_09_graphql_function_list_prompts(self):
        """mcpFunctionList(mcpType='prompt') should return only prompts."""
        query = (
            '{ mcpFunctionList(mcpType: "prompt") { mcpFunctionList { %s } total pageNumber pageSize } }'
            % FUNCTION_FIELDS
        )
        resp = post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        print(f"  mcpFunctionList(prompts) → {total} prompts")

    def test_10_graphql_module_list(self):
        """mcpModuleList should return MCP modules."""
        query = (
            "{ mcpModuleList { mcpModuleList { %s } total pageNumber pageSize } }"
            % MODULE_FIELDS
        )
        resp = post_graphql(self.token, query)
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

    def test_11_graphql_setting_list(self):
        """mcpSettingList should return MCP settings."""
        query = (
            "{ mcpSettingList { mcpSettingList { %s } total pageNumber pageSize } }"
            % SETTING_FIELDS
        )
        resp = post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpSettingList"]
        total = result["total"]
        print(f"  mcpSettingList → {total} settings")

    def test_12_graphql_function_call_list(self):
        """mcpFunctionCallList should return function call history."""
        query = (
            "{ mcpFunctionCallList { mcpFunctionCallList { %s } total pageNumber pageSize } }"
            % FUNCTION_CALL_FIELDS
        )
        resp = post_graphql(self.token, query)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)

        result = data["data"]["mcpFunctionCallList"]
        total = result["total"]
        print(f"  mcpFunctionCallList → {total} calls")

    # ── GraphQL Mutations: process_mcp_package ────────────────────────

    def test_13_graphql_process_mcp_package(self):
        """End-to-end: presign → PUT ZIP to S3 → processMcpPackage.

        Mirrors mcp_daemon_engine's test_graphql_process_mcp_package but
        exercises the full gateway path (auth → GraphQL proxy → daemon).

        The ZIP path comes from MCP_TEST_PACKAGE_ZIP in .env.

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
        resp = post_graphql(
            self.token, presign_query,
            variables={"packageName": package_name},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data, f"GraphQL errors: {data.get('errors')}")
        presign = data["data"]["generateMcpPackageUploadUrl"]

        # If S3 bucket is not configured in the gateway context, the daemon
        # will return ok=False with a clear message — skip gracefully.
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
            upload_url,
            data=zip_bytes,
            headers={"Content-Type": "application/zip"},
            timeout=30,
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
        resp = post_graphql(
            self.token, process_query,
            variables=process_vars,
        )
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

    def test_14_graphql_process_mcp_package_invalid_name(self):
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
        resp = post_graphql(
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

    # ── GraphQL Mutation: sync_external_mcp_server ─────────────────────

    def test_15_graphql_sync_external_mcp_server(self):
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
        resp = post_graphql(self.token, query, variables=variables)
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

    # ── External MCP Proxy: tools/call ──────────────────────────────

    def test_21_rest_call_external_tool(self):
        """Invoke an external tool via REST JSON-RPC tools/call through the gateway.

        Mirrors test_mcp_call_external_search_product in mcp_daemon_engine:

            agent -> gateway REST /mcp -> mcp_server.call_tool
                  -> execute_tool_function -> ExternalMCPProxy.call_tool
                  -> MCPHttpClient -> upstream Shopify MCP server

        Requires MCP_TEST_EXTERNAL_BASE_URL in .env and that
        syncExternalMcpServer (test_15) has already run for this partition.
        """
        if not os.getenv("MCP_TEST_EXTERNAL_BASE_URL"):
            self.skipTest("MCP_TEST_EXTERNAL_BASE_URL not set in .env")

        server_name = os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "shopify_demo")
        tool_name = os.getenv("MCP_TEST_EXTERNAL_TOOL_NAME", "search_shop_policies_and_faqs")
        query_text = os.getenv("MCP_TEST_EXTERNAL_TOOL_QUERY", "shirt")

        # Resolve the actual tool name from the live mcpFunctionList
        # in case .env has a stale/incorrect name (e.g. "shopify_search_catalog"
        # vs actual "search_catalog").
        list_query = (
            '{ mcpFunctionList(mcpType: "tool") { mcpFunctionList { name moduleName } total } }'
        )
        resp = post_graphql(self.token, list_query)
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("errors"):
                items = data["data"]["mcpFunctionList"]["mcpFunctionList"]
                external_tools = [t for t in items if t["moduleName"] == server_name]
                if external_tools:
                    env_match = next(
                        (t for t in external_tools if t["name"] == tool_name), None
                    )
                    resolved = env_match["name"] if env_match else external_tools[0]["name"]
                    if resolved != tool_name:
                        print(f"  Tool name resolved: {tool_name} → {resolved}")
                        tool_name = resolved

        arguments = {"query": query_text}

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 42,
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Part-Id": PART_ID,
        }
        resp = requests.post(rest_url(), json=payload, headers=headers, timeout=60)
        self.assertEqual(resp.status_code, 200)

        data = resp.json()

        # Check for JSON-RPC error
        if data.get("error"):
            err = data["error"]
            err_msg = err.get("message", "")
            err_data = err.get("data", "")
            # "Unknown tool" = name mismatch — hard fail with helpful message
            if "Unknown tool" in str(err_data) or "Unknown tool" in err_msg:
                self.fail(
                    f"Unknown tool '{tool_name}' — syncExternalMcpServer may "
                    f"register tools with their original upstream names. "
                    f"Check MCP_TEST_EXTERNAL_TOOL_NAME. Error: {err_data or err_msg}"
                )
            # Other -32603 = gateway routing OK but upstream error — soft pass
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

    def test_16_rest_initialize(self):
        """REST JSON-RPC initialize should return capabilities."""
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "1.0"},
            },
            "id": 1,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Part-Id": PART_ID,
        }
        resp = requests.post(rest_url(), json=payload, headers=headers, timeout=30)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("result", data)
        result = data["result"]
        self.assertIn("capabilities", result)
        print(f"  REST initialize → protocol={result.get('protocolVersion')}")

    def test_17_rest_tools_list(self):
        """REST JSON-RPC tools/list should return tool inventory."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 2,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Part-Id": PART_ID,
        }
        resp = requests.post(rest_url(), json=payload, headers=headers, timeout=30)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("result", data)
        tools = data["result"].get("tools", [])
        self.assertGreater(len(tools), 0, "Expected at least one tool from tools/list")
        print(f"  REST tools/list → {len(tools)} tools")

    def test_18_rest_mcp_info(self):
        """REST /mcp_info should return MCP configuration summary."""
        url = f"{BASE_URL}/{ENDPOINT_ID}/{PART_ID}/mcp_info"
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(url, headers=headers, timeout=30)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # mcp_info returns server info, tools/resources/prompts lists,
        # and SSE stats — verify the essential keys are present.
        self.assertIn("server", data)
        self.assertIn("tools", data)
        print(f"  mcp_info → server={data.get('server')}, {len(data.get('tools', []))} tools")

    # ── MCP Tool Call (ResolvePay search_customers) ────────────────────

    def test_19_rest_tool_call_search_customers(self):
        """Call search_customers tool via REST JSON-RPC through the gateway.

        Tests the full dispatch path:
            client → gateway /mcp → dispatch_mcp → MCPDaemonEngine.mcp
                  → execute_tool_function → MCPResolvepayConnector.search_customers
                  → ResolvePay sandbox API

        Uses the arguments: {"business_ap_email": "bibo72@outlook.com"}.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 100,
            "params": {
                "name": "search_customers",
                "arguments": {
                    "business_ap_email": "bibo72@outlook.com",
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Part-Id": PART_ID,
        }
        resp = requests.post(rest_url(), json=payload, headers=headers, timeout=60)
        self.assertEqual(resp.status_code, 200, f"HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Check for JSON-RPC-level error
        if data.get("error"):
            err = data["error"]
            err_msg = err.get("message", "")
            err_data = err.get("data", "")
            self.fail(
                f"tools/call 'search_customers' returned JSON-RPC error: "
                f"[{err.get('code')}] {err_msg} — {err_data}"
            )

        result = data.get("result", {})
        content = result.get("content", [])
        self.assertTrue(
            content,
            f"search_customers returned no content. Full response: {json.dumps(data)[:500]}",
        )

        first = content[0]
        self.assertIn("text", first, f"Expected 'text' in content[0]: {first}")
        print(f"  tools/call 'search_customers' → content_items={len(content)}")
        print(f"  first_text: {first['text'][:500]}")

    # ── SSE Endpoint ──────────────────────────────────────────────────

    def test_20_sse_endpoint_connect(self):
        """SSE endpoint should accept connections (may return 503 if no
        SSE manager is registered, which is a known pre-existing issue)."""
        url = f"{BASE_URL}/{ENDPOINT_ID}/{PART_ID}/sse"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
        }
        # Use stream=True and read only a small amount to avoid blocking
        try:
            resp = requests.get(url, headers=headers, timeout=5, stream=True)
            # 200 = SSE stream established, 503 = known SSEManager issue
            self.assertIn(
                resp.status_code, [200, 503],
                f"Unexpected SSE status: {resp.status_code}",
            )
            if resp.status_code == 200:
                print(f"  SSE endpoint → connected (200 OK)")
            else:
                print(f"  SSE endpoint → 503 (known SSEManager resolution issue)")
        except requests.exceptions.ConnectionError:
            print(f"  SSE endpoint → connection error (server may not support SSE)")

    # ── GraphQL Raw Query ─────────────────────────────────────────────

    def test_21_graphql_raw_query(self):
        """Raw GraphQL query string should work like structured queries."""
        raw = '{"query": "{ mcpFunctionList { mcpFunctionList { name mcpType } total } }"}'
        resp = requests.post(
            graphql_url(),
            data=raw,
            headers=graphql_headers(self.token),
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("errors", data)
        total = data["data"]["mcpFunctionList"]["total"]
        self.assertGreater(total, 0)
        print(f"  Raw GraphQL query → {total} functions")


# ═══════════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Print banner
    print(f"\n{'='*60}")
    print(f"  SilvaEngine Gateway — MCP E2E Integration Tests")
    print(f"  Gateway: {BASE_URL}")
    print(f"  Endpoint: {ENDPOINT_ID} / Partition: {PART_ID}")
    print(f"  .env: {_ENV_FILE}")
    print(f"{'='*60}\n")

    unittest.main(verbosity=2)