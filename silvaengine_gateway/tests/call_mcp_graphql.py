#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Call the MCP Daemon Engine GraphQL endpoint through the SilvaEngine Gateway.

Tests MCP Daemon GraphQL queries (tools, resources, prompts listing).

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # List MCP tools (default):
    python -m silvaengine_gateway.tests.call_mcp_graphql

    # List resources:
    python -m silvaengine_gateway.tests.call_mcp_graphql --query resources

    # List prompts:
    python -m silvaengine_gateway.tests.call_mcp_graphql --query prompts

    # Raw GraphQL:
    python -m silvaengine_gateway.tests.call_mcp_graphql --graphql '{"query": "{ mcpTools { name description } }"}'

All connection params (base_url, endpoint_id, part_id, auth credentials) are
read from the .env file in the same directory as this script.
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
_MCP_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent / "mcp_daemon_engine")
for _p in [_PROJECT_ROOT, _MCP_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
from dotenv import load_dotenv


def _promote_editable_finders() -> None:
    import sys as _sys
    from importlib.machinery import PathFinder

    meta_path = _sys.meta_path
    editable = [f for f in meta_path if hasattr(f, "__name__") and f.__name__ == "_EditableFinder"]
    if not editable:
        return
    pf_index = None
    for i, finder in enumerate(meta_path):
        if finder is PathFinder:
            pf_index = i
            break
    if pf_index is None:
        return
    if all(meta_path.index(f) < pf_index for f in editable):
        return
    for f in editable:
        meta_path.remove(f)
    for i, finder in enumerate(meta_path):
        if finder is PathFinder:
            pf_index = i
            break
    for f in reversed(editable):
        meta_path.insert(pf_index, f)


_promote_editable_finders()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call MCP Daemon GraphQL through the SilvaEngine Gateway"
    )
    parser.add_argument("--base-url", type=str, default=None)
    parser.add_argument("--dotenv", type=str, default=None)
    parser.add_argument("--username", type=str, default=None)
    parser.add_argument("--password", type=str, default=None)
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--endpoint-id", type=str, default=None)
    parser.add_argument("--part-id", type=str, default=None)
    parser.add_argument(
        "--query", "-q", type=str, default="tools",
        choices=["tools", "resources", "prompts", "all"],
        help="What to query: tools, resources, prompts, or all (default: tools)",
    )
    parser.add_argument("--graphql", type=str, default=None,
                        help="Raw GraphQL query JSON (overrides --query)")
    parser.add_argument("--raw", action="store_true",
                        help="Print raw JSON response without formatting")
    return parser.parse_args()


def get_token(base_url: str, username: str, password: str) -> str:
    resp = requests.post(f"{base_url}/auth/token", data={"username": username, "password": password}, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def build_graphql_payload(args: argparse.Namespace) -> dict:
    if args.graphql:
        return json.loads(args.graphql)

    fields = []
    if args.query in ("tools", "all"):
        fields.append("mcpTools { name description inputSchema }")
    if args.query in ("resources", "all"):
        fields.append("mcpResources { name description uri mimeType }")
    if args.query in ("prompts", "all"):
        fields.append("mcpPrompts { name description arguments { name description required } }")

    query = "{ " + " ".join(fields) + " }"
    return {"query": query}


def main() -> None:
    args = parse_args()

    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"WARNING: .env file not found at {env_file}")
    else:
        load_dotenv(env_file, override=True)
        print(f"Loaded .env from: {env_file}")

    base_url = args.base_url or os.getenv("BASE_URL", "http://localhost:8765")
    endpoint_id = args.endpoint_id or os.getenv("endpoint_id", "test-ep")
    part_id = args.part_id or os.getenv("part_id", "test-part")

    # Authenticate
    if args.token:
        token = args.token
    else:
        username = args.username or os.getenv("ADMIN_USERNAME", "admin")
        password = args.password or os.getenv("ADMIN_PASSWORD", "admin123")
        print(f"Authenticating as {username}...")
        token = get_token(base_url, username, password)

    # Build request
    graphql_path = f"/{endpoint_id}/{part_id}/mcp_daemon_graphql"
    url = f"{base_url}{graphql_path}"
    payload = build_graphql_payload(args)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    print(f"\n{'='*60}")
    print(f"  MCP Daemon GraphQL — {args.query}")
    print(f"  URL: {url}")
    print(f"{'='*60}\n")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
    except requests.ConnectionError:
        print(f"ERROR: Cannot connect to {base_url}")
        print("Start the gateway with: python -m silvaengine_gateway.tests.run_daemon")
        sys.exit(1)

    if args.raw:
        print(resp.text)
        return

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"Status: {resp.status_code}")
        print(f"Response (non-JSON): {resp.text[:2000]}")
        return

    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error: {json.dumps(data, indent=2)}")
        return

    if "errors" in data:
        print("GraphQL Errors:")
        for err in data["errors"]:
            print(f"  - {err.get('message', err)}")
        return

    result = data.get("data", {})
    for key, value in result.items():
        print(f"\n── {key} ──────────────────────────────")
        if isinstance(value, list):
            for i, item in enumerate(value):
                print(f"  [{i+1}] {json.dumps(item, indent=4, ensure_ascii=False) if isinstance(item, dict) else item}")
        else:
            print(f"  {json.dumps(value, indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    main()