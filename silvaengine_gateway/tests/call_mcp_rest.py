#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Call the MCP Daemon Engine REST (JSON-RPC) endpoint through the SilvaEngine Gateway.

Tests MCP JSON-RPC messages: initialize, tools/list, resources/list, prompts/list,
and tool calls.

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Initialize (default):
    python -m silvaengine_gateway.tests.call_mcp_rest

    # List tools:
    python -m silvaengine_gateway.tests.call_mcp_rest --method tools/list

    # Call a tool:
    python -m silvaengine_gateway.tests.call_mcp_rest --method tools/call --params '{"name":"my_tool","arguments":{}}'

    # Raw JSON-RPC:
    python -m silvaengine_gateway.tests.call_mcp_rest --raw-json '{"jsonrpc":"2.0","method":"initialize","id":1}'

All connection params are read from the .env file.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import json
import os
import sys
from pathlib import Path

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call MCP JSON-RPC REST endpoint through the SilvaEngine Gateway"
    )
    parser.add_argument("--base-url", type=str, default=None)
    parser.add_argument("--dotenv", type=str, default=None)
    parser.add_argument("--username", type=str, default=None)
    parser.add_argument("--password", type=str, default=None)
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--endpoint-id", type=str, default=None)
    parser.add_argument("--part-id", type=str, default=None)
    parser.add_argument(
        "--method",
        "-m",
        type=str,
        default="initialize",
        help="JSON-RPC method (default: initialize)",
    )
    parser.add_argument(
        "--params", "-p", type=str, default=None, help="JSON-RPC params as JSON string"
    )
    parser.add_argument(
        "--raw-json",
        type=str,
        default=None,
        help="Raw JSON-RPC message (overrides --method/--params)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="Print raw JSON response without formatting"
    )
    return parser.parse_args()


def get_token(base_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{base_url}/auth/token",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


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

    if args.token:
        token = args.token
    else:
        username = args.username or os.getenv("ADMIN_USERNAME", "admin")
        password = args.password or os.getenv("ADMIN_PASSWORD", "admin123")
        print(f"Authenticating as {username}...")
        token = get_token(base_url, username, password)

    # Build JSON-RPC payload
    if args.raw_json:
        payload = json.loads(args.raw_json)
    else:
        payload = {
            "jsonrpc": "2.0",
            "method": args.method,
            "id": 1,
        }
        if args.params:
            payload["params"] = json.loads(args.params)

    # Send to REST endpoint
    rest_path = f"/{endpoint_id}/{part_id}/mcp"
    url = f"{base_url}{rest_path}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    print(f"\n{'='*60}")
    print(f"  MCP JSON-RPC — {payload.get('method', 'unknown')}")
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
    print(json.dumps(data, indent=2, ensure_ascii=False))

    # Highlight JSON-RPC errors
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        print(f"\nJSON-RPC Error: [{err.get('code')}] {err.get('message')}")


if __name__ == "__main__":
    main()
