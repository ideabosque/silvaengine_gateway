#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deploy an MCP package ZIP to the MCP daemon engine via the SilvaEngine Gateway.

Flow: presign upload URL → PUT ZIP to S3 → processMcpPackage → verify.

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Deploy a package (module name inferred from package name):
    python -m silvaengine_gateway.tests.deploy_mcp_package \\
        --zip /path/to/mcp_hospirfq_processor.zip

    # Explicit module and package names:
    python -m silvaengine_gateway.tests.deploy_mcp_package \\
        --zip /path/to/mcp_resolvepay_connector.zip \\
        --module-name mcp_resolvepay_connector \\
        --package-name mcp_resolvepay_connector

    # Pass connector setting overrides via --variables (JSON):
    python -m silvaengine_gateway.tests.deploy_mcp_package \\
        --zip /path/to/mcp_resolvepay_connector.zip \\
        --variables '{"apiKey": "xxx", "baseUrl": "https://sandbox.example.com/api/"}'

    # Skip the verification query:
    python -m silvaengine_gateway.tests.deploy_mcp_package --zip ... --no-verify

    # Custom base URL / endpoint / partition:
    python -m silvaengine_gateway.tests.deploy_mcp_package \\
        --zip ... --base-url http://localhost:8765 \\
        --endpoint-id gpt --part-id nestaging

Connection defaults: BASE_URL=http://localhost:8765, endpoint_id=gpt,
part_id=nestaging. Values can be overridden by .env or CLI flags.
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
# GraphQL mutations
# ═══════════════════════════════════════════════════════════════════════

_PRESIGN_MUTATION = """
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

_PROCESS_MUTATION = """
mutation ProcessPackage(
    $s3Key: String!,
    $moduleName: String!,
    $packageName: String!,
    $source: String,
    $variables: JSONCamelCase,
    $updatedBy: String!
) {
    processMcpPackage(
        s3Key: $s3Key
        moduleName: $moduleName
        packageName: $packageName
        source: $source
        variables: $variables
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
        description="Deploy an MCP package ZIP to the MCP daemon engine via gateway.",
    )
    parser.add_argument(
        "--zip",
        required=True,
        help="Path to the MCP package ZIP file.",
    )
    parser.add_argument(
        "--module-name",
        default=None,
        help="Module name (defaults to package name).",
    )
    parser.add_argument(
        "--package-name",
        default=None,
        help="Package name (defaults to ZIP stem, e.g. mcp_hospirfq_processor).",
    )
    parser.add_argument(
        "--source",
        default="s3",
        help="Source type for processMcpPackage (default: s3).",
    )
    parser.add_argument(
        "--variables",
        default=None,
        help='Setting overrides as JSON string, e.g. \'{"apiKey": "xxx"}\'.',
    )
    parser.add_argument(
        "--updated-by",
        default="deploy_script",
        help="updatedBy value (default: deploy_script).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-deploy mcpModuleList verification query.",
    )
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

    zip_path = Path(args.zip).resolve()
    if not zip_path.exists():
        print(f"ERROR: ZIP not found at {zip_path}")
        sys.exit(1)

    # Derive names
    package_name = args.package_name or zip_path.stem
    module_name = args.module_name or package_name

    # Parse variables override
    variables_override = None
    if args.variables:
        try:
            variables_override = json.loads(args.variables)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid --variables JSON: {e}")
            sys.exit(1)

    base_url = args.base_url
    endpoint_id = args.endpoint_id
    part_id = args.part_id
    graphql_url = f"{base_url}/{endpoint_id}/mcp_daemon_graphql"

    print(f"{'=' * 60}")
    print(f"  MCP Package Deployment")
    print(f"  Gateway:   {base_url}")
    print(f"  Endpoint:  {endpoint_id} / Partition: {part_id}")
    print(f"  ZIP:       {zip_path}")
    print(f"  Module:    {module_name}")
    print(f"  Package:   {package_name}")
    print(f"  Source:    {args.source}")
    if variables_override:
        print(f"  Variables: {json.dumps(variables_override)}")
    print(f"{'=' * 60}\n")

    # ── 1. Health check ────────────────────────────────────────────
    try:
        health = requests.get(f"{base_url}/health", timeout=5)
        health.raise_for_status()
        print(f"  Health: {health.status_code} ✓")
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
    print(f"  Token acquired ({len(token)} chars) ✓\n")

    # ── 3. Load ZIP ────────────────────────────────────────────────
    zip_bytes = zip_path.read_bytes()
    print(f"  ZIP loaded: {len(zip_bytes):,} bytes")

    # ── 4. Generate presigned upload URL ───────────────────────────
    print(f"\n  [1/3] Generating presigned upload URL...")
    resp = _post_graphql(
        graphql_url,
        token,
        _PRESIGN_MUTATION,
        variables={"packageName": package_name},
        part_id=part_id,
    )
    if resp.status_code != 200:
        print(f"ERROR: Presign request failed: HTTP {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()
    if "errors" in data:
        print(f"ERROR: GraphQL errors: {data['errors']}")
        sys.exit(1)
    presign = data["data"]["generateMcpPackageUploadUrl"]
    if not presign["ok"]:
        print(f"ERROR: Presign failed: {presign.get('message', 'unknown')}")
        print(
            "S3 may not be configured. Check FUNCT_BUCKET_NAME in .env "
            "and that it reaches the daemon through the gateway setting dict."
        )
        sys.exit(1)
    s3_key = presign["s3Key"]
    print(f"  Presign: ok ✓  s3Key={s3_key}")

    # ── 5. PUT ZIP to S3 ───────────────────────────────────────────
    print(f"\n  [2/3] Uploading ZIP to S3...")
    put = requests.put(
        presign["uploadUrl"],
        data=zip_bytes,
        headers={"Content-Type": "application/zip"},
        timeout=60,
    )
    if put.status_code != 200:
        print(f"ERROR: S3 PUT failed: HTTP {put.status_code}")
        print(put.text[:500])
        sys.exit(1)
    print(f"  S3 PUT: {put.status_code} ✓")

    # ── 6. processMcpPackage ───────────────────────────────────────
    print(f"\n  [3/3] Processing MCP package...")
    process_vars = {
        "s3Key": s3_key,
        "moduleName": module_name,
        "packageName": package_name,
        "source": args.source,
        "updatedBy": args.updated_by,
    }
    if variables_override:
        process_vars["variables"] = variables_override

    resp = _post_graphql(
        graphql_url,
        token,
        _PROCESS_MUTATION,
        variables=process_vars,
        part_id=part_id,
    )
    if resp.status_code != 200:
        print(f"ERROR: processMcpPackage request failed: HTTP {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()
    if "errors" in data:
        print(f"ERROR: GraphQL errors: {data['errors']}")
        sys.exit(1)
    process = data["data"]["processMcpPackage"]
    if not process["ok"]:
        print(f"ERROR: processMcpPackage failed: {process.get('message', 'unknown')}")
        sys.exit(1)

    stats = process["stats"]
    print(f"  processMcpPackage: ok ✓")
    print(f"  {stats['tools']} tools, {stats['resources']} resources, "
          f"{stats['prompts']} prompts, {stats['modules']} modules, "
          f"{stats['settings']} settings")

    # ── 7. Verify ──────────────────────────────────────────────────
    if args.no_verify:
        print(f"\n  Verification skipped (--no-verify).")
    else:
        print(f"\n  Verifying deployment...")
        resp = _post_graphql(
            graphql_url,
            token,
            _MODULE_LIST_QUERY,
            part_id=part_id,
        )
        if resp.status_code == 200:
            data = resp.json()
            modules = (
                data.get("data", {})
                .get("mcpModuleList", {})
                .get("mcpModuleList", [])
            )
            deployed = [
                m for m in modules if m["moduleName"] == module_name
            ]
            if deployed:
                print(f"  Module '{module_name}' updatedAt: "
                      f"{deployed[0]['updatedAt']}")
            else:
                print(f"  WARNING: Module '{module_name}' not found in "
                      f"mcpModuleList.")
        else:
            print(f"  WARNING: Verification query returned HTTP "
                  f"{resp.status_code}.")

    print(f"\n{'=' * 60}")
    print(f"  Deployment complete ✓")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()