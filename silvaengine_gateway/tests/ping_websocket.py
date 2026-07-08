#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket ping client for the SilvaEngine Gateway.

Connects to a running gateway, authenticates, and invokes the gateway-level
``ping`` on a WebSocket route (default: ai_agent_core_engine's
``ai_agent_core_ws``). The gateway answers each ping with a ``pong`` immediately,
without a model dispatch — useful as a liveness check or keepalive.

    Client → {"action": "ping"}  →  Gateway
    Client ← {"type": "pong", "message": "Hello at <time>!!", ...}

Prerequisites:
    1. Gateway running:  python -m silvaengine_gateway.tests.run_daemon
    2. .env configured (endpoint_id, part_id, admin credentials)
    3. websockets library:  pip install websockets

Usage:
    # Single ping (uses .env for endpoint_id / part_id / admin creds)
    python -m silvaengine_gateway.tests.ping_websocket

    # Repeat as a keepalive: 5 pings, 2s apart
    python -m silvaengine_gateway.tests.ping_websocket --count 5 --interval 2

    # Use the "type" envelope instead of "action"
    python -m silvaengine_gateway.tests.ping_websocket --style type

    # Different route / connection params / token
    python -m silvaengine_gateway.tests.ping_websocket \\
        --gateway-url ws://localhost:8765 --endpoint-id gpt --part-id nestaging \\
        --route ai_agent_core_ws --token "$ADMIN_STATIC_TOKEN"

    # Remote instance — ALWAYS include the port (the gateway is not on :80)
    python -m silvaengine_gateway.tests.ping_websocket --gateway-url ws://34.208.34.202:8765

    # Remote over TLS (bare IP / self-signed cert → --insecure)
    python -m silvaengine_gateway.tests.ping_websocket --gateway-url wss://34.208.34.202 --insecure
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _apply_default_port(base_url: str, port: str) -> str:
    """Append the gateway port to a URL that omits one (like call_websocket.py).

    - ``ws://HOST``  → ``ws://HOST:<port>``  (the gateway is not on :80)
    - ``ws://HOST:9000`` → unchanged (explicit port respected)
    - ``wss://HOST`` → unchanged (TLS defaults to :443, typical for a proxy)
    """
    parts = urlsplit(base_url)
    if parts.port is not None or parts.scheme == "wss" or not parts.hostname:
        return base_url
    netloc = f"{parts.hostname}:{port}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' library not installed. Run: pip install websockets")
    sys.exit(1)


def _ssl_context(base_url: str, insecure: bool):
    """Return an SSL context for wss:// URLs (None for plain ws://).

    ``insecure`` disables certificate/hostname verification — needed when
    connecting to a bare IP or a self-signed cert (common for remote instances
    reached by address rather than DNS name).
    """
    if not base_url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_URL_HINTS = (
    "Hints:\n"
    "  - include the gateway port, e.g.  --gateway-url ws://HOST:8765\n"
    "  - for TLS use  wss://HOST  (add --insecure for a bare-IP/self-signed cert)\n"
    "  - or pass a token to skip /auth/token:  --token <jwt>"
)


def load_env() -> None:
    """Load .env from the tests/ directory (without overriding real env)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            # Strip inline comments (e.g. KEY=value  # comment)
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            if key and key not in os.environ:
                os.environ[key] = value


async def get_auth_token(
    base_url: str, username: str, password: str, ssl_ctx=None
) -> str:
    """Get a JWT from the gateway's /auth/token endpoint."""
    import urllib.request

    http_url = base_url.replace("ws://", "http://").replace("wss://", "https://")
    data = f"username={username}&password={password}".encode()
    req = urllib.request.Request(
        f"{http_url}/auth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            return json.loads(resp.read())["access_token"]
    except (socket.timeout, TimeoutError):
        print(f"ERROR: Timed out reaching {http_url}/auth/token")
        print(_URL_HINTS)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to get auth token from {http_url}/auth/token: {e}")
        print(_URL_HINTS)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ping a gateway WebSocket route")
    p.add_argument(
        "--gateway-url",
        default=os.getenv("BASE_URL", "ws://localhost"),
        help="Gateway base URL (ws://|wss://|http://|https://). "
        "If it omits a port, --gateway-port is appended. Default: ws://localhost",
    )
    p.add_argument(
        "--gateway-port",
        default=os.getenv("GATEWAY_PORT", "8765"),
        help="Port appended when --gateway-url has none (default: 8765). "
        "Ignored for wss:// (TLS defaults to 443) and explicit-port URLs.",
    )
    p.add_argument("--endpoint-id", default=os.getenv("endpoint_id", "gpt"))
    p.add_argument("--part-id", default=os.getenv("part_id", "nestaging"))
    p.add_argument(
        "--route",
        default="ai_agent_core_ws",
        help="WebSocket route name (default: ai_agent_core_ws)",
    )
    p.add_argument("--token", default=None, help="JWT (skips /auth/token)")
    p.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "admin"))
    p.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", "admin123"))
    p.add_argument("--count", type=int, default=1, help="Number of pings (default: 1)")
    p.add_argument(
        "--interval", type=float, default=1.0, help="Seconds between pings (default: 1)"
    )
    p.add_argument(
        "--style",
        choices=["action", "type"],
        default="action",
        help='Ping envelope: {"action":"ping"} or {"type":"ping"} (default: action)',
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="For wss:// — skip TLS cert/hostname verification "
        "(bare IP or self-signed cert)",
    )
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    # Normalize to a ws:// base URL, then append the default port if omitted.
    base = args.gateway_url.replace("http://", "ws://").replace("https://", "wss://")
    base = _apply_default_port(base.rstrip("/"), args.gateway_port)
    ssl_ctx = _ssl_context(base, args.insecure)

    token = args.token or await get_auth_token(
        base, args.username, args.password, ssl_ctx=ssl_ctx
    )
    url = f"{base}/{args.endpoint_id}/{args.route}?token={token}&part_id={args.part_id}"

    print(f"{'=' * 60}")
    print(f"  WebSocket ping")
    print(f"  URL:   {base}/{args.endpoint_id}/{args.route}?token=<jwt>&part_id={args.part_id}")
    print(f"  Pings: {args.count} (interval {args.interval}s, style={args.style})")
    print(f"{'=' * 60}")

    try:
        async with websockets.connect(
            url, max_size=None, ssl=ssl_ctx, open_timeout=15
        ) as ws:
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            print(f"  connection_ack: {ack.get('connection_id', '?')}")

            ok = True
            for i in range(1, args.count + 1):
                ping = {args.style: "ping", "id": f"ping-{i}"}
                await ws.send(json.dumps(ping))
                pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

                is_pong = pong.get("type") == "pong" and pong.get("id") == f"ping-{i}"
                ok = ok and is_pong
                mark = "OK" if is_pong else "UNEXPECTED"
                print(f"  [{i}/{args.count}] {mark}: {json.dumps(pong)}")

                if i < args.count:
                    await asyncio.sleep(args.interval)

            print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1
    except ConnectionRefusedError:
        print(f"ERROR: Connection refused at {base}.")
        print("  Local? start it:  python -m silvaengine_gateway.tests.run_daemon")
        print(_URL_HINTS)
        return 1
    except (asyncio.TimeoutError, socket.timeout, TimeoutError):
        print(f"ERROR: Timed out connecting to {base}.")
        print(_URL_HINTS)
        return 1
    except ssl.SSLError as e:
        print(f"ERROR: TLS handshake failed for {base}: {e}")
        print("  For a bare IP or self-signed cert, add --insecure.")
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        print(_URL_HINTS)
        return 1


def main() -> None:
    load_env()
    args = parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
