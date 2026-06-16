#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manual SSE caller for the MCP Daemon route through SilvaEngine Gateway.

This mirrors the SSE coverage in test_mcp_e2e.py:
    - test_16_sse_post_tools_call_search_customers
    - test_29_sse_get_connect
    - test_30_sse_post_initialize
    - test_31_sse_post_tools_list

The gateway exposes the same /sse path for streaming GET requests and JSON-RPC
POST messages. Use --send to exercise the POST path, then the script opens the
stream so you can inspect connected and heartbeat events.

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Listen to SSE stream (default: 30 seconds):
    python -m silvaengine_gateway.tests.call_mcp_sse

    # Listen for 60 seconds:
    python -m silvaengine_gateway.tests.call_mcp_sse --timeout 60

    # Send a message and listen for the response:
    python -m silvaengine_gateway.tests.call_mcp_sse --send initialize

    # Send a tools/list request:
    python -m silvaengine_gateway.tests.call_mcp_sse --send tools/list

    # Call the ResolvePay search_customers tool used by test_mcp_e2e.py:
    python -m silvaengine_gateway.tests.call_mcp_sse --send tools/call --params '{"name":"search_customers","arguments":{"business_ap_email":"bibo72@outlook.com"}}'

Connection defaults match test_mcp_e2e.py: BASE_URL=http://localhost:8765,
endpoint_id=gpt, part_id=nestaging. Values can be overridden by .env or CLI.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import json
import os
import sys
import time
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
        description=(
            "Call the MCP SSE endpoint through SilvaEngine Gateway "
            "(manual companion to test_mcp_e2e.py)"
        )
    )
    parser.add_argument("--base-url", type=str, default=None)
    parser.add_argument("--dotenv", type=str, default=None)
    parser.add_argument("--username", type=str, default=None)
    parser.add_argument("--password", type=str, default=None)
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--endpoint-id", type=str, default=None)
    parser.add_argument("--part-id", type=str, default=None)
    parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=30,
        help="SSE listen timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--send",
        "-s",
        type=str,
        default=None,
        help=(
            "JSON-RPC method to POST to /sse before listening "
            "(for example: initialize, tools/list, tools/call)"
        ),
    )
    parser.add_argument(
        "--params",
        "-p",
        type=str,
        default=None,
        help="JSON-RPC params as a JSON string for --send",
    )
    parser.add_argument(
        "--raw", action="store_true", help="Print raw SSE events without formatting"
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


def listen_sse(
    base_url: str, endpoint_id: str, part_id: str, token: str, timeout: int, raw: bool
) -> None:
    """Connect to SSE stream and print events."""
    sse_path = f"/{endpoint_id}/{part_id}/sse"
    url = f"{base_url}{sse_path}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }

    print(f"\n{'='*60}")
    print(f"  SSE Connection")
    print(f"  URL: {url}")
    print(f"  Listening for {timeout}s...")
    print(f"{'='*60}\n")

    try:
        deadline = time.monotonic() + timeout
        resp = requests.get(url, headers=headers, stream=True, timeout=timeout + 5)
        resp.raise_for_status()

        event_type = None
        for line in resp.iter_lines(decode_unicode=True):
            if time.monotonic() >= deadline:
                print(f"\n  SSE stream timed out after {timeout}s (this is normal)")
                break

            if line is None:
                continue

            if line.startswith("event:"):
                event_type = line[6:].strip()
                if not raw:
                    continue

            if line.startswith("data:"):
                data_str = line[5:].strip()

                if raw:
                    print(f"[{event_type or 'message'}] {data_str}")
                else:
                    try:
                        data = json.loads(data_str)
                        if event_type == "heartbeat":
                            print(f"  ❤ heartbeat: {data.get('timestamp', '?')}")
                        elif event_type == "connected":
                            print(
                                f"  ✅ connected: client_id={data.get('client_id', '?')}"
                            )
                        else:
                            print(f"  📨 [{event_type or 'message'}]")
                            print(
                                f"     {json.dumps(data, indent=2, ensure_ascii=False)[:500]}"
                            )
                    except json.JSONDecodeError:
                        print(f"  📨 [{event_type or 'message'}] {data_str[:200]}")

            if line == "":
                event_type = None

    except requests.ConnectionError:
        print(f"ERROR: Cannot connect to {base_url}")
        print("Start the gateway with: python -m silvaengine_gateway.tests.run_daemon")
        sys.exit(1)
    except requests.Timeout:
        print(f"\n  SSE stream timed out after {timeout}s (this is normal)")
    except KeyboardInterrupt:
        print("\n  SSE stream interrupted by user")


def send_message(
    base_url: str,
    endpoint_id: str,
    part_id: str,
    token: str,
    method: str,
    params: dict,
    raw: bool,
) -> None:
    """Send a JSON-RPC message via the SSE POST endpoint."""
    sse_post_path = f"/{endpoint_id}/{part_id}/sse"
    url = f"{base_url}{sse_post_path}"

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": 1,
    }
    if params:
        payload["params"] = params

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    print(f"\n  Sending: {method}")
    print(f"     POST {url}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        print(f"  Status: {resp.status_code}")
        if raw:
            print(f"  Response: {resp.text[:500]}")
        else:
            try:
                data = resp.json()
                print(
                    f"  Response: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}"
                )
            except json.JSONDecodeError:
                print(f"  Response: {resp.text[:500]}")
    except requests.ConnectionError:
        print(f"ERROR: Cannot connect to {base_url}")


def main() -> None:
    args = parse_args()

    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"WARNING: .env file not found at {env_file}")
    else:
        load_dotenv(env_file, override=True)
        print(f"Loaded .env from: {env_file}")

    base_url = args.base_url or os.getenv("BASE_URL", "http://localhost:8765")
    endpoint_id = args.endpoint_id or os.getenv("endpoint_id", "gpt")
    part_id = args.part_id or os.getenv("part_id", "nestaging")

    if args.token:
        token = args.token
    else:
        username = args.username or os.getenv("ADMIN_USERNAME", "admin")
        password = args.password or os.getenv("ADMIN_PASSWORD", "admin123")
        print(f"Authenticating as {username}...")
        token = get_token(base_url, username, password)

    # Match test_mcp_e2e.py: POST the JSON-RPC message to /sse, then open GET /sse.
    if args.send:
        params = json.loads(args.params) if args.params else {}
        send_message(
            base_url, endpoint_id, part_id, token, args.send, params, args.raw
        )
        print()

    # Always listen to SSE stream
    listen_sse(base_url, endpoint_id, part_id, token, args.timeout, args.raw)


if __name__ == "__main__":
    main()
