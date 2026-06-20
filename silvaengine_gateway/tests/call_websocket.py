#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live WebSocket integration client for the SilvaEngine Gateway.

Connects to a running gateway instance, authenticates, and sends an
ask_model request to test the full streaming pipeline:

    Client → WebSocket → Gateway → dispatch_ask_model → ai_agent_core_engine
        → execute_ask_model → LLM streaming → send_data_to_stream
        → ConnectionManager.send_to_connection → Client receives chunks

Prerequisites:
    1. Gateway running:  python -m silvaengine_gateway
       (or:  silvaengine-gateway)
    2. ai_agent_core_engine installed:  pip install -e ../ai_agent_core_engine
    3. .env configured with AWS, Neo4j, LLM credentials
    4. A valid agent_uuid and thread_uuid in DynamoDB

Usage:
    # Basic — uses .env for endpoint_id and part_id
    python silvaengine_gateway/tests/call_websocket.py

    # Specify agent and thread
    python silvaengine_gateway/tests/call_websocket.py \\
        --agent-uuid "agent-xxx" \\
        --thread-uuid "thread-xxx" \\
        --prompt "Hello, what can you do?"

    # Custom gateway URL
    python silvaengine_gateway/tests/call_websocket.py \\
        --gateway-url ws://localhost:8765 \\
        --endpoint-id gpt \\
        --part-id nestaging

    # Use admin static token (skip /auth/token)
    python silvaengine_gateway/tests/call_websocket.py \\
        --token "$ADMIN_STATIC_TOKEN" \\
        --agent-uuid "agent-xxx" \\
        --prompt "Hello"

Install websockets library:
    pip install websockets
"""

from __future__ import print_function

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' library not installed. Run: pip install websockets")
    sys.exit(1)


def load_env():
    """Load .env from tests/ directory."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value


async def get_auth_token(base_url: str, username: str, password: str) -> str:
    """Get a JWT token from the gateway's /auth/token endpoint."""
    import urllib.request

    http_url = base_url.replace("ws://", "http://").replace("wss://", "https://")
    data = f"username={username}&password={password}".encode()
    req = urllib.request.Request(
        f"{http_url}/auth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return body["access_token"]
    except Exception as e:
        print(f"ERROR: Failed to get auth token: {e}")
        sys.exit(1)


def _find_reasoning_marker(message):
    """Return a reasoning marker such as rs#1 when present in frame metadata."""
    for key in ("suffix", "message_group_id", "data_format", "type"):
        value = str(message.get(key) or "")
        lowered = value.lower()
        marker_index = lowered.find("rs#")
        if marker_index >= 0:
            marker = value[marker_index:].split("-", 1)[0].split("/", 1)[0]
            return marker.strip()
        if "reason" in lowered:
            return value.strip() or "reasoning"

    for key, value in message.items():
        if key == "chunk_delta":
            continue
        text = str(value)
        lowered = text.lower()
        marker_index = lowered.find("rs#")
        if marker_index >= 0:
            marker = text[marker_index:].split("-", 1)[0].split("/", 1)[0]
            return marker.strip()
        if "reason" in lowered:
            return text.strip() or "reasoning"

    return ""


def is_reasoning_chunk(message):
    """Return True when a stream frame appears to carry reasoning tokens."""
    return bool(_find_reasoning_marker(message))


def reasoning_label(message):
    """Build a compact label for reasoning stream chunks."""
    marker = _find_reasoning_marker(message)
    return f"REASONING {marker}" if marker else "REASONING"


def print_stream_boundary(message, stream_state):
    """Print a visible lane switch when reasoning starts or answer resumes."""
    is_reasoning = is_reasoning_chunk(message)
    marker = _find_reasoning_marker(message) if is_reasoning else ""
    previous = stream_state.get("marker")
    current = (is_reasoning, marker)

    if current == previous:
        return

    if is_reasoning:
        print(f"\n>>> {reasoning_label(message)}> ", end="", flush=True)
    elif previous and previous[0]:
        print("\n>>> ANSWER> ", end="", flush=True)

    stream_state["marker"] = current


async def connect_and_stream(
    ws_uri: str,
    token: str,
    agent_uuid: str,
    thread_uuid: str,
    prompt: str,
    updated_by: str = "test-user",
    input_files: list = None,
    stream: bool = True,
    timeout: int = 120,
    verbose: bool = False,
):
    """
    Connect to the WebSocket endpoint, send an ask_model request,
    and receive streaming chunks until is_message_end=True.

    Returns the full response text and timing info.
    """
    print(f"\nConnecting to: {ws_uri.split('?')[0]}...")

    async with websockets.connect(ws_uri, max_size=2**20) as ws:
        # 1. Receive connection_ack
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ack.get("type") != "connection_ack":
            print(f"ERROR: Expected connection_ack, got: {ack}")
            return None

        connection_id = ack["connection_id"]
        print(f"Connected. connection_id={connection_id}")

        # 2. Send ask_model request
        request = {
            "action": "ask_model",
            "arguments": {
                "agent_uuid": agent_uuid,
                "thread_uuid": thread_uuid,
                "user_query": prompt,
                "updated_by": updated_by,
                "stream": stream,
            },
        }
        if input_files:
            request["arguments"]["input_files"] = input_files

        print(f"\nSending ask_model request:")
        print(f"  agent_uuid: {agent_uuid}")
        print(f"  thread_uuid: {thread_uuid}")
        print(f"  prompt: {prompt}")
        print()

        await ws.send(json.dumps(request))

        # 3. Receive streaming chunks
        chunks = []
        full_text = ""
        started = time.time()
        chunk_count = 0
        last_chunk_time = None
        idle_timeout = 8  # seconds without a chunk → stream complete
        stream_state = {}

        try:
            while True:
                recv_timeout = idle_timeout if last_chunk_time else timeout
                raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                message = json.loads(raw)

                # Check if this is a streaming chunk (has chunk_delta)
                if "chunk_delta" in message:
                    chunk_count += 1
                    last_chunk_time = time.time()
                    delta = message["chunk_delta"]
                    data_format = message.get("data_format", "text")
                    is_end = message.get("is_message_end", False)
                    index = message.get("index", -1)
                    group_id = message.get("message_group_id", "")

                    if verbose:
                        metadata = {
                            key: value
                            for key, value in message.items()
                            if key != "chunk_delta"
                        }
                        print(f"\n[chunk metadata] {json.dumps(metadata, default=str)}", flush=True)

                    if data_format == "text":
                        full_text += delta
                        # Print chunk inline for real-time feedback
                        print_stream_boundary(message, stream_state)
                        print(delta, end="", flush=True)
                    elif data_format == "xml":
                        full_text += delta
                        print_stream_boundary(message, stream_state)
                        print(f"[XML chunk {index}]: {delta[:100]}...", flush=True)
                    else:
                        full_text += delta
                        print_stream_boundary(message, stream_state)
                        print(
                            f"[{data_format} chunk {index}]: {delta[:100]}...",
                            flush=True,
                        )

                    chunks.append(message)

                    if is_end:
                        elapsed = time.time() - started
                        print(f"\n[is_message_end=True — stream complete]")
                        print(f"\n--- Stream complete ---")
                        print(f"  Chunks received: {chunk_count}")
                        print(f"  Total text length: {len(full_text)} chars")
                        print(f"  Elapsed: {elapsed:.2f}s")
                        print(f"  message_group_id: {group_id}")
                        # Drain the trailing dispatch result (short timeout)
                        try:
                            await asyncio.wait_for(ws.recv(), timeout=5)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        break

                elif message.get("type") == "error":
                    print(f"\nERROR from server: {message.get('detail')}")
                    break

                elif chunk_count > 0 and ("result" in message or "status" in message):
                    # Dispatch result arrived without is_message_end
                    break

                else:
                    print(f"\n[Unknown message type]: {json.dumps(message, default=str)[:200]}")

        except asyncio.TimeoutError:
            if chunk_count > 0:
                # Idle timeout after chunks — stream is likely complete
                elapsed = time.time() - started
                print(f"\n[is_message_end not received — idle timeout]")
                print(f"\n--- Stream complete (idle timeout) ---")
                print(f"  Chunks received: {chunk_count}")
                print(f"  Total text length: {len(full_text)} chars")
                print(f"  Elapsed: {elapsed:.2f}s")
                # Drain the trailing dispatch result
                try:
                    await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
                except (asyncio.TimeoutError, Exception):
                    pass
            else:
                elapsed = time.time() - started
                print(f"\n\nTIMEOUT after {elapsed:.1f}s — no response received")

    return {
        "connection_id": connection_id,
        "chunks": chunks,
        "full_text": full_text,
        "chunk_count": chunk_count,
        "elapsed": time.time() - started,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Live WebSocket integration test client for SilvaEngine Gateway"
    )
    parser.add_argument(
        "--gateway-url",
        default=None,
        help="Gateway WebSocket base URL (default: ws://localhost)",
    )
    parser.add_argument(
        "--gateway-port",
        default=None,
        help="Gateway port (default: from .env GATEWAY_PORT or 8765)",
    )
    parser.add_argument(
        "--endpoint-id",
        default=os.getenv("endpoint_id", "gpt"),
        help="Endpoint ID (default: from .env or 'gpt')",
    )
    parser.add_argument(
        "--part-id",
        default=os.getenv("part_id", "nestaging"),
        help="Partition/tenant ID (default: from .env or 'nestaging')",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="JWT token (skip /auth/token if provided)",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("ADMIN_USERNAME", "admin"),
        help="Admin username for /auth/token",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD", ""),
        help="Admin password for /auth/token",
    )
    parser.add_argument(
        "--agent-uuid",
        default=None,
        help="Agent UUID (default: DEFAULT_AGENT_UUID from .env)",
    )
    parser.add_argument(
        "--thread-uuid",
        default=None,
        help="Thread UUID (generates a new UUID if not provided)",
    )
    parser.add_argument(
        "--prompt",
        default="Hello, what can you help me with?",
        help="Prompt to send to the AI agent",
    )
    parser.add_argument(
        "--updated-by",
        default=None,
        help="Updated-by user ID (default: DEFAULT_UPDATED_BY from .env)",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="User ID (default: DEFAULT_USER_ID from .env)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming (use synchronous mode)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for receiving chunks (default: 120)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print raw JSON for each chunk",
    )

    args = parser.parse_args()

    # Load .env
    load_env()

    # Re-read defaults from env after loading .env (argparse defaults are
    # evaluated at parser construction time, before load_env() runs)
    endpoint_id = args.endpoint_id or os.getenv("endpoint_id", "gpt")
    part_id = args.part_id or os.getenv("part_id", "nestaging")
    agent_uuid = args.agent_uuid or os.getenv("DEFAULT_AGENT_UUID")
    updated_by = args.updated_by or os.getenv("DEFAULT_UPDATED_BY", "test-user")
    user_id = args.user_id or os.getenv("DEFAULT_USER_ID", "test-user")
    # Password may be empty from argparse default — re-read from env
    password = args.password or os.getenv("ADMIN_PASSWORD", "")
    username = args.username or os.getenv("ADMIN_USERNAME", "admin")
    port = args.gateway_port or os.getenv("GATEWAY_PORT", "8765")

    if not agent_uuid:
        print("ERROR: --agent-uuid is required (or set DEFAULT_AGENT_UUID in .env)")
        parser.print_help()
        sys.exit(1)

    # Build WebSocket URI
    base_url = (args.gateway_url or "ws://localhost").rstrip("/")
    ws_base = f"{base_url}:{port}"

    # Get token
    if args.token:
        token = args.token
        print(f"Using provided token: {token[:20]}...")
    else:
        print(f"Getting auth token from {ws_base}...")
        token = asyncio.run(get_auth_token(ws_base, username, password))
        print(f"Got token: {token[:20]}...")

    # Build full WebSocket URI
    ws_uri = (
        f"{ws_base}/{endpoint_id}/ai_agent_core_ws"
        f"?token={token}&part_id={part_id}"
    )

    # Generate thread_uuid if not provided
    thread_uuid = args.thread_uuid
    if not thread_uuid:
        import uuid as _uuid
        thread_uuid = str(_uuid.uuid4())
        print(f"Generated thread_uuid: {thread_uuid}")

    print(f"Using agent_uuid: {agent_uuid}")
    print(f"Using updated_by: {updated_by}")

    # Connect and stream
    result = asyncio.run(
        connect_and_stream(
            ws_uri=ws_uri,
            token=token,
            agent_uuid=agent_uuid,
            thread_uuid=thread_uuid,
            prompt=args.prompt,
            updated_by=updated_by,
            stream=not args.no_stream,
            timeout=args.timeout,
            verbose=args.verbose,
        )
    )

    if result and result.get("chunk_count", 0) > 0:
        print(f"\n\n{'='*60}")
        print("SUCCESS: WebSocket streaming integration test passed!")
        print(f"  connection_id: {result['connection_id']}")
        print(f"  chunks: {result['chunk_count']}")
        print(f"  text length: {len(result['full_text'])} chars")
        print(f"  elapsed: {result['elapsed']:.2f}s")
        print(f"{'='*60}")
        sys.exit(0)
    elif result and result.get("chunk_count", 0) == 0:
        print(f"\n\nWARNING: No streaming chunks received. Check:")
        print(f"  - agent_uuid exists in DynamoDB for partition '{part_id}'")
        print(f"  - LLM credentials are valid")
        print(f"  - Gateway logs for errors")
        sys.exit(1)
    else:
        print("\n\nFAILED: Connection or streaming failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()