#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Register (deploy) an external HTTP MCP server with the MCP daemon engine via
the SilvaEngine Gateway.

Unlike deploy_mcp_package.py (which uploads a Python tool-package ZIP), this
script points the daemon at an already-running remote MCP endpoint. The daemon
connects to it, reads its tools/resources/prompts inventory, and persists proxy
rows (source="external", class="ExternalMCPProxy") so the tools are callable
through the gateway like any other MCP tool.

Flow: syncExternalMcpServer → verify (mcpModuleList + mcpFunctionList).

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Register an external MCP server (all values from tests/.env defaults):
    python -m silvaengine_gateway.tests.deploy_external_mcp

    # Explicit endpoint:
    python -m silvaengine_gateway.tests.deploy_external_mcp \\
        --server-name shopify_demo \\
        --external-url https://nestaging.myshopify.com/api/mcp \\
        --name-prefix shopify_

    # With an upstream bearer token and extra headers (JSON):
    python -m silvaengine_gateway.tests.deploy_external_mcp \\
        --server-name weatherco \\
        --external-url https://mcp.weather.example.com \\
        --bearer-token SECRET \\
        --headers '{"X-Api-Key": "xxx"}'

    # Skip the post-deploy verification queries:
    python -m silvaengine_gateway.tests.deploy_external_mcp --no-verify

    # Remote gateway / different tenant:
    python -m silvaengine_gateway.tests.deploy_external_mcp \\
        --base-url http://34.208.34.202:8765 \\
        --endpoint-id gpt --part-id nestaging \\
        --username bibo72@outlook.com --password 12345Abc!

Connection defaults: BASE_URL=http://localhost:8765, endpoint_id=gpt,
part_id=nestaging. The external-server defaults come from the
MCP_TEST_EXTERNAL_* keys in tests/.env. All are overridable by CLI flags.

Note: server_name must match ^[A-Za-z][A-Za-z0-9_]*$ (letters, digits,
underscore; not starting with a digit) and external-url must be http/https.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import json
import os
import sys
from pathlib import Path

# ── Ensure project roots are on sys.path ───────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_MCP_ROOT = str(
    Path(__file__).resolve().parent.parent.parent.parent / "mcp_daemon_engine"
)
for _p in [_PROJECT_ROOT, _MCP_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
from dotenv import load_dotenv


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
    pf_index = next(
        (i for i, f in enumerate(meta_path) if f is PathFinder), None
    )
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

# ── Load .env ──────────────────────────────────────────────────────
_ENV_FILE = str(Path(__file__).resolve().parent / ".env")
if Path(_ENV_FILE).exists():
    load_dotenv(_ENV_FILE, override=True)


# ═══════════════════════════════════════════════════════════════════════
# GraphQL operations
# ═══════════════════════════════════════════════════════════════════════

_SYNC_MUTATION = """
mutation SyncExternalMcpServer(
    $serverName: String!,
    $baseUrl: String!,
    $bearerToken: String,
    $headers: JSONSnakeCase,
    $namePrefix: String,
    $updatedBy: String!
) {
    syncExternalMcpServer(
        serverName: $serverName
        baseUrl: $baseUrl
        bearerToken: $bearerToken
        headers: $headers
        namePrefix: $namePrefix
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

_MODULE_LIST_QUERY = (
    "{ mcpModuleList { mcpModuleList { moduleName source updatedAt } total } }"
)

_FUNCTION_LIST_QUERY = """
query FunctionsByModule($moduleName: String!) {
    mcpFunctionList(moduleName: $moduleName, limit: 100) {
        total
        mcpFunctionList {
            name
            mcpType
            functionName
            className
        }
    }
}
"""


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _auth_headers(token, part_id):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }


def _get_token(base_url, username, password):
    resp = requests.post(
        f"{base_url}/auth/token",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _post_graphql(url, token, query, variables=None, part_id=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        url,
        json=payload,
        headers=_auth_headers(token, part_id),
        timeout=120,
    )
    return resp


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Register an external HTTP MCP server with the MCP daemon engine "
            "via the SilvaEngine Gateway (syncExternalMcpServer)."
        ),
    )
    # ── External server (what to register) ─────────────────────────
    parser.add_argument(
        "--server-name",
        default=os.getenv("MCP_TEST_EXTERNAL_SERVER_NAME", "test_external"),
        help=(
            "Local external-server identifier (^[A-Za-z][A-Za-z0-9_]*$). "
            "Default: MCP_TEST_EXTERNAL_SERVER_NAME or 'test_external'."
        ),
    )
    parser.add_argument(
        "--external-url",
        default=os.getenv("MCP_TEST_EXTERNAL_BASE_URL"),
        help=(
            "Remote MCP endpoint URL (http/https) the daemon connects to. "
            "Default: MCP_TEST_EXTERNAL_BASE_URL."
        ),
    )
    parser.add_argument(
        "--bearer-token",
        default=os.getenv("MCP_TEST_EXTERNAL_BEARER") or None,
        help=(
            "Optional bearer token forwarded to the upstream server. "
            "Default: MCP_TEST_EXTERNAL_BEARER."
        ),
    )
    parser.add_argument(
        "--headers",
        default=None,
        help=(
            "Optional extra upstream HTTP headers as JSON, "
            'e.g. \'{"X-Api-Key": "xxx"}\'.'
        ),
    )
    parser.add_argument(
        "--name-prefix",
        default=os.getenv("MCP_TEST_EXTERNAL_NAME_PREFIX") or None,
        help=(
            "Optional prefix applied to local function names to avoid "
            "collisions. Default: MCP_TEST_EXTERNAL_NAME_PREFIX."
        ),
    )
    parser.add_argument(
        "--updated-by",
        default="deploy_script",
        help="updatedBy audit value (default: deploy_script).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-deploy verification queries.",
    )
    # ── Gateway connection (how to reach the daemon) ───────────────
    parser.add_argument(
        "--base-url",
        default=os.getenv("BASE_URL", "http://localhost:8765"),
        help="Gateway base URL (default: http://localhost:8765).",
    )
    parser.add_argument(
        "--endpoint-id",
        default=os.getenv("endpoint_id", "gpt"),
        help="Endpoint ID (default: gpt).",
    )
    parser.add_argument(
        "--part-id",
        default=os.getenv("part_id", "nestaging"),
        help="Partition ID, sent via Part-Id header (default: nestaging).",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("ADMIN_USERNAME", "admin"),
        help="Admin username (default: admin).",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD", "admin123"),
        help="Admin password (default: admin123).",
    )
    args = parser.parse_args()

    # ── Validate inputs ────────────────────────────────────────────
    if not args.external_url:
        print(
            "ERROR: no external MCP URL. Pass --external-url or set "
            "MCP_TEST_EXTERNAL_BASE_URL in tests/.env."
        )
        sys.exit(1)
    if not (
        args.external_url.startswith("http://")
        or args.external_url.startswith("https://")
    ):
        print(f"ERROR: --external-url must be http/https, got {args.external_url!r}")
        sys.exit(1)

    headers_override = None
    if args.headers:
        try:
            headers_override = json.loads(args.headers)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid --headers JSON: {e}")
            sys.exit(1)
        if not isinstance(headers_override, dict):
            print("ERROR: --headers JSON must be an object of header:value pairs.")
            sys.exit(1)

    base_url = args.base_url
    endpoint_id = args.endpoint_id
    part_id = args.part_id
    graphql_url = f"{base_url}/{endpoint_id}/mcp_daemon_graphql"

    print(f"{'=' * 60}")
    print(f"  External MCP Server Registration")
    print(f"  Gateway:    {base_url}")
    print(f"  Endpoint:   {endpoint_id} / Partition: {part_id}")
    print(f"  Server:     {args.server_name}")
    print(f"  Remote URL: {args.external_url}")
    if args.name_prefix:
        print(f"  Prefix:     {args.name_prefix}")
    if args.bearer_token:
        print(f"  Bearer:     (set, {len(args.bearer_token)} chars)")
    if headers_override:
        print(f"  Headers:    {json.dumps(headers_override)}")
    print(f"{'=' * 60}\n")

    # ── 1. Health check ────────────────────────────────────────────
    try:
        health = requests.get(f"{base_url}/health", timeout=5)
        health.raise_for_status()
        print(f"  Health: {health.status_code} OK")
    except Exception as e:
        print(f"ERROR: Gateway not reachable at {base_url}: {e}")
        print("Start it with: python -m silvaengine_gateway.tests.run_daemon")
        sys.exit(1)

    # ── 2. Authenticate ────────────────────────────────────────────
    print(f"  Authenticating as {args.username}...")
    try:
        token = _get_token(base_url, args.username, args.password)
    except Exception as e:
        print(f"ERROR: Authentication failed: {e}")
        sys.exit(1)
    print(f"  Token acquired ({len(token)} chars) OK\n")

    # ── 3. syncExternalMcpServer ───────────────────────────────────
    print(f"  [1/1] Syncing external MCP server...")
    sync_vars = {
        "serverName": args.server_name,
        "baseUrl": args.external_url,
        "updatedBy": args.updated_by,
    }
    if args.bearer_token:
        sync_vars["bearerToken"] = args.bearer_token
    if headers_override:
        sync_vars["headers"] = headers_override
    if args.name_prefix:
        sync_vars["namePrefix"] = args.name_prefix

    resp = _post_graphql(
        graphql_url,
        token,
        _SYNC_MUTATION,
        variables=sync_vars,
        part_id=part_id,
    )
    if resp.status_code != 200:
        print(f"ERROR: syncExternalMcpServer request failed: HTTP {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()
    if "errors" in data:
        print(f"ERROR: GraphQL errors: {data['errors']}")
        sys.exit(1)
    sync = data["data"]["syncExternalMcpServer"]
    if not sync["ok"]:
        print(f"ERROR: sync failed: {sync.get('message', 'unknown')}")
        print(
            "Check that the remote MCP endpoint is reachable FROM the daemon "
            "host, that any required bearer token/headers are set, and that "
            "mcp-http-client is installed in the daemon environment."
        )
        sys.exit(1)

    stats = sync["stats"]
    print(f"  syncExternalMcpServer: ok OK")
    print(f"  {stats['tools']} tools, {stats['resources']} resources, "
          f"{stats['prompts']} prompts, {stats['modules']} modules, "
          f"{stats['settings']} settings")

    # ── 4. Verify ──────────────────────────────────────────────────
    if args.no_verify:
        print(f"\n  Verification skipped (--no-verify).")
    else:
        print(f"\n  Verifying registration...")
        # 4a. Module row exists with source="external"
        resp = _post_graphql(
            graphql_url, token, _MODULE_LIST_QUERY, part_id=part_id
        )
        if resp.status_code == 200 and "errors" not in resp.json():
            modules = (
                resp.json()
                .get("data", {})
                .get("mcpModuleList", {})
                .get("mcpModuleList", [])
            )
            deployed = [
                m for m in modules if m["moduleName"] == args.server_name
            ]
            if deployed:
                print(f"  Module '{args.server_name}' source="
                      f"{deployed[0]['source']} updatedAt="
                      f"{deployed[0]['updatedAt']}")
            else:
                print(f"  WARNING: Module '{args.server_name}' not found in "
                      f"mcpModuleList.")
        else:
            print(f"  WARNING: module verification query returned HTTP "
                  f"{resp.status_code}.")

        # 4b. Synced functions
        resp = _post_graphql(
            graphql_url,
            token,
            _FUNCTION_LIST_QUERY,
            variables={"moduleName": args.server_name},
            part_id=part_id,
        )
        if resp.status_code == 200 and "errors" not in resp.json():
            fl = (
                resp.json()
                .get("data", {})
                .get("mcpFunctionList", {})
            )
            functions = fl.get("mcpFunctionList", []) or []
            print(f"  {fl.get('total', len(functions))} function(s) registered:")
            for fn in functions[:25]:
                print(f"    - {fn['name']}  [{fn.get('mcpType')}] "
                      f"-> {fn.get('functionName')}")
            if len(functions) > 25:
                print(f"    ... and {len(functions) - 25} more")
        else:
            print(f"  WARNING: function verification query returned HTTP "
                  f"{resp.status_code}.")

    print(f"\n{'=' * 60}")
    print(f"  External MCP registration complete")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
