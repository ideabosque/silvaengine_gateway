#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive WebSocket chatbot client for the SilvaEngine Gateway.

Maintains a persistent WebSocket connection and lets you chat with an
AI agent in a REPL-style loop.  Each message is sent as an ask_model
request on the same thread_uuid, so the agent retains conversation
context across turns.

Streaming chunks are printed in real-time as they arrive.

Prerequisites:
    1. Gateway running:  python -m silvaengine_gateway
    2. ai_agent_core_engine installed:  pip install -e ../ai_agent_core_engine
    3. .env in tests/ with AWS, Neo4j, LLM credentials
    4. DEFAULT_AGENT_UUID in .env (or pass --agent-uuid)

Usage:
    # Basic — all defaults from .env
    python silvaengine_gateway/tests/chat_websocket.py

    # Custom agent + prompt
    python silvaengine_gateway/tests/chat_websocket.py \\
        --agent-uuid "agent-xxx" \\
        --thread-uuid "thread-xxx"

    # Non-default gateway
    python silvaengine_gateway/tests/chat_websocket.py \\
        --gateway-url ws://localhost \\
        --gateway-port 8765

    # Use admin static token (skip /auth/token)
    python silvaengine_gateway/tests/chat_websocket.py --token "$ADMIN_STATIC_TOKEN"

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
import uuid as _uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' library not installed. Run: pip install websockets")
    sys.exit(1)


# ANSI colors for a nicer terminal experience
class C:
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"


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
        print(f"{C.RED}ERROR: Failed to get auth token: {e}{C.RESET}")
        sys.exit(1)


async def receive_streaming_response(ws, timeout=120, idle_timeout=8):
    """Receive streaming chunks with real-time printing and idle-timeout
    stream completion.

    Prints text chunks inline in real-time as they arrive.  If no new
    chunks arrive for ``idle_timeout`` seconds after the last chunk,
    the stream is treated as complete (the agent handler does not always
    send ``is_message_end=true``).  After completion, drains the trailing
    dispatch result message silently so it doesn't block the next turn.

    Args:
        ws: WebSocket connection.
        timeout: Maximum total wait per response (seconds).
        idle_timeout: If no chunk arrives for this many seconds after the
            last chunk, treat the stream as complete.

    Returns (full_text, chunk_count, elapsed).
    """
    full_text = ""
    chunk_count = 0
    started = time.time()
    last_chunk_time = None

    try:
        while True:
            # Use idle_timeout after first chunk, full timeout before
            recv_timeout = idle_timeout if last_chunk_time else timeout
            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            message = json.loads(raw)

            if "chunk_delta" in message:
                chunk_count += 1
                last_chunk_time = time.time()
                delta = message["chunk_delta"]
                data_format = message.get("data_format", "text")
                is_end = message.get("is_message_end", False)

                if data_format == "text":
                    full_text += delta
                    print(delta, end="", flush=True)
                elif data_format == "xml":
                    full_text += delta
                    print(f"{C.DIM}{delta}{C.RESET}", end="", flush=True)
                else:
                    full_text += delta
                    print(f"{C.DIM}[{data_format}]{delta}{C.RESET}", end="", flush=True)

                if is_end:
                    print(f"\n{C.GREEN}[is_message_end=True — stream complete]{C.RESET}", flush=True)
                    # Explicit end marker — stream is done.
                    # Briefly drain the trailing dispatch result message
                    # (sent by the gateway after run_in_executor returns)
                    # so it doesn't block the next turn. Use a short timeout
                    # since we already know the stream is complete.
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break

            elif message.get("type") == "error":
                print(f"\n{C.RED}ERROR: {message.get('detail')}{C.RESET}")
                break

            elif chunk_count > 0 and (
                "result" in message or "status" in message
            ):
                # Dispatch result arrived without is_message_end — stream
                # is done. Consume and break.
                break

            else:
                # Unknown message before stream end — skip silently
                pass

    except asyncio.TimeoutError:
        if chunk_count == 0:
            print(f"\n{C.YELLOW}[no response — timeout after {time.time() - started:.0f}s]{C.RESET}")
        else:
            print(f"\n{C.YELLOW}[is_message_end not received — idle timeout after {time.time() - started:.0f}s, {chunk_count} chunks]{C.RESET}")

    # If we exited via idle timeout (no is_message_end), drain the
    # dispatch result so it doesn't block the next turn.
    if last_chunk_time and chunk_count > 0:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    elapsed = time.time() - started
    return full_text, chunk_count, elapsed


async def chat_loop(
    ws_uri: str,
    agent_uuid: str,
    thread_uuid: str,
    updated_by: str,
    stream: bool = True,
    timeout: int = 120,
):
    """Run the interactive chat loop over a persistent WebSocket connection."""

    print(f"\n{C.DIM}Connecting to {ws_uri.split('?')[0]}...{C.RESET}")

    async with websockets.connect(ws_uri, max_size=2**20) as ws:
        # Receive connection_ack
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ack.get("type") != "connection_ack":
            print(f"{C.RED}ERROR: Expected connection_ack, got: {ack}{C.RESET}")
            return

        connection_id = ack["connection_id"]
        print(f"{C.GREEN}Connected!{C.RESET}")
        print(f"  {C.DIM}connection_id : {connection_id}{C.RESET}")
        print(f"  {C.DIM}agent_uuid   : {agent_uuid}{C.RESET}")
        print(f"  {C.DIM}thread_uuid  : {thread_uuid}{C.RESET}")
        print(f"  {C.DIM}updated_by   : {updated_by}{C.RESET}")
        print(f"  {C.DIM}streaming    : {'on' if stream else 'off'}{C.RESET}")
        print()
        print(f"{C.CYAN}Type your message and press Enter. Commands:{C.RESET}")
        print(f"  {C.YELLOW}/quit{C.RESET}          - Exit the chat")
        print(f"  {C.YELLOW}/new-thread{C.RESET}    - Start a new conversation thread")
        print(f"  {C.YELLOW}/retry{C.RESET}        - Re-send the last message")
        print(f"  {C.YELLOW}/stats{C.RESET}        - Show connection stats")
        print()

        turn = 0
        last_message = None

        while True:
            try:
                # Read user input (blocking — run in executor to avoid
                # blocking the event loop)
                loop = asyncio.get_event_loop()
                prompt = await loop.run_in_executor(
                    None,
                    lambda: input(
                        f"{C.BOLD}{C.CYAN}[turn {turn + 1}]{C.RESET} {C.GREEN}You>{C.RESET} "
                    ),
                )
            except (EOFError, KeyboardInterrupt):
                print(f"\n{C.DIM}Goodbye!{C.RESET}")
                break

            prompt = prompt.strip()
            if not prompt:
                continue

            # Handle commands
            if prompt in ("/quit", "/exit", "/q"):
                print(f"{C.DIM}Goodbye!{C.RESET}")
                break

            if prompt == "/new-thread":
                thread_uuid = str(_uuid.uuid4())
                turn = 0
                print(f"\n{C.YELLOW}Started new thread: {thread_uuid}{C.RESET}\n")
                continue

            if prompt == "/retry":
                if last_message is None:
                    print(f"{C.YELLOW}No previous message to retry.{C.RESET}")
                    continue
                prompt = last_message
                print(f"{C.DIM}Retrying: {prompt}{C.RESET}")

            if prompt == "/stats":
                print(f"\n{C.DIM}Connection stats:{C.RESET}")
                print(f"  connection_id : {connection_id}")
                print(f"  thread_uuid   : {thread_uuid}")
                print(f"  agent_uuid    : {agent_uuid}")
                print(f"  turns         : {turn}")
                print()
                continue

            # Send ask_model request
            turn += 1
            last_message = prompt

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

            await ws.send(json.dumps(request))

            # Receive streaming response
            print(f"{C.MAGENTA}Agent>{C.RESET} ", end="", flush=True)

            full_text, chunk_count, elapsed = await receive_streaming_response(
                ws, timeout=timeout
            )

            print(f" {C.DIM}[{chunk_count} chunks, {elapsed:.1f}s]{C.RESET}")
            print()

            # If we got no response at all, something went wrong
            if not full_text and chunk_count == 0:
                print(
                    f"{C.YELLOW}No response received. "
                    f"Check gateway logs for errors.{C.RESET}\n"
                )


def main():
    parser = argparse.ArgumentParser(
        description="Interactive WebSocket chatbot client for SilvaEngine Gateway"
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
        default=None,
        help="Endpoint ID (default: from .env or 'gpt')",
    )
    parser.add_argument(
        "--part-id",
        default=None,
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
        "--updated-by",
        default=None,
        help="Updated-by user ID (default: DEFAULT_UPDATED_BY from .env)",
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
        help="Timeout per response in seconds (default: 120)",
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
    password = args.password or os.getenv("ADMIN_PASSWORD", "")
    username = args.username or os.getenv("ADMIN_USERNAME", "admin")
    port = args.gateway_port or os.getenv("GATEWAY_PORT", "8765")

    if not agent_uuid:
        print(
            f"{C.RED}ERROR: --agent-uuid is required "
            f"(or set DEFAULT_AGENT_UUID in .env){C.RESET}"
        )
        parser.print_help()
        sys.exit(1)

    # Generate thread_uuid if not provided
    thread_uuid = args.thread_uuid
    if not thread_uuid:
        thread_uuid = str(_uuid.uuid4())

    # Build WebSocket URI
    base_url = (args.gateway_url or "ws://localhost").rstrip("/")
    ws_base = f"{base_url}:{port}"

    # Get token
    if args.token:
        token = args.token
        print(f"{C.DIM}Using provided token: {token[:20]}...{C.RESET}")
    else:
        print(f"{C.DIM}Getting auth token from {ws_base}...{C.RESET}")
        token = asyncio.run(get_auth_token(ws_base, username, password))
        print(f"{C.DIM}Got token: {token[:20]}...{C.RESET}")

    # Build full WebSocket URI
    ws_uri = (
        f"{ws_base}/{endpoint_id}/ai_agent_core_ws"
        f"?token={token}&part_id={part_id}"
    )

    # Run the chat loop
    try:
        asyncio.run(
            chat_loop(
                ws_uri=ws_uri,
                agent_uuid=agent_uuid,
                thread_uuid=thread_uuid,
                updated_by=updated_by,
                stream=not args.no_stream,
                timeout=args.timeout,
            )
        )
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Interrupted. Goodbye!{C.RESET}")
    except ConnectionRefusedError:
        print(
            f"{C.RED}ERROR: Connection refused. "
            f"Is the gateway running on {ws_base}?{C.RESET}"
        )
        sys.exit(1)
    except Exception as e:
        print(f"\n{C.RED}ERROR: {e}{C.RESET}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()