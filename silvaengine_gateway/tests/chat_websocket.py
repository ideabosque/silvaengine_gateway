#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive WebSocket chatbot client for the SilvaEngine Gateway.

Maintains a persistent WebSocket connection and lets you chat with an
AI agent in a REPL-style loop. Each message is sent as an ask_model
request on the same thread_uuid, so the agent retains conversation
context across turns.

Streaming chunks are printed in real time as they arrive. A single
background WebSocket reader owns ws.recv(), so late chunks can still be
printed while the user prompt is open.

Prerequisites:
    1. Gateway running:  python -m silvaengine_gateway
    2. ai_agent_core_engine installed:  pip install -e ../ai_agent_core_engine
    3. .env in tests/ with AWS, Neo4j, LLM credentials
    4. DEFAULT_AGENT_UUID in .env (or pass --agent-uuid)

Usage:
    python silvaengine_gateway/tests/chat_websocket.py
    python silvaengine_gateway/tests/chat_websocket.py --agent-uuid "agent-xxx"
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
from contextlib import suppress
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' library not installed. Run: pip install websockets")
    sys.exit(1)


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


async def websocket_reader(ws, incoming_queue):
    """Read WebSocket frames into a queue.

    The websockets client only allows one coroutine to call ws.recv() at a
    time. This reader is the single owner; prompt handling and response
    handling consume parsed messages from incoming_queue.
    """
    while True:
        raw = await ws.recv()
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            message = {"type": "raw", "data": raw}
        await incoming_queue.put(message)


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
        print(
            f"\n{C.YELLOW}>>> {reasoning_label(message)}>{C.RESET} ",
            end="",
            flush=True,
        )
    elif previous and previous[0]:
        print(f"\n{C.MAGENTA}Agent>{C.RESET} ", end="", flush=True)

    stream_state["marker"] = current


def print_stream_chunk(message, stream_state=None, debug_chunks=False):
    """Print one stream chunk and return (delta, is_end)."""
    delta = message.get("chunk_delta", "")
    data_format = message.get("data_format", "text")
    is_end = message.get("is_message_end", False)
    if stream_state is None:
        stream_state = {}

    if debug_chunks:
        metadata = {
            key: value
            for key, value in message.items()
            if key != "chunk_delta"
        }
        print(f"\n{C.DIM}[chunk metadata] {json.dumps(metadata, default=str)}{C.RESET}", flush=True)

    print_stream_boundary(message, stream_state)

    if data_format == "text":
        print(delta, end="", flush=True)
    elif data_format == "xml":
        print(f"{C.DIM}{delta}{C.RESET}", end="", flush=True)
    else:
        print(
            f"{C.DIM}[{data_format}]{delta}{C.RESET}",
            end="",
            flush=True,
        )

    if is_end:
        print(
            f"\n{C.GREEN}[is_message_end=True - stream complete]{C.RESET}",
            flush=True,
        )

    return delta, is_end


def print_unsolicited_stream_message(message, active, stream_state, debug_chunks=False):
    """Print frames that arrive while the user prompt is open."""
    if "chunk_delta" in message:
        if not active and not is_reasoning_chunk(message):
            print(f"\n{C.MAGENTA}Agent>{C.RESET} ", end="", flush=True)
        active = True
        _, is_end = print_stream_chunk(message, stream_state, debug_chunks)
        return False if is_end else active

    if message.get("type") == "error":
        print(f"\n{C.RED}ERROR: {message.get('detail')}{C.RESET}", flush=True)
        return False

    if active and ("result" in message or "status" in message):
        return False

    return active


async def read_prompt_while_receiving(prompt_text, incoming_queue, idle_timeout=8, debug_chunks=False):
    """Read user input while continuing to receive late stream chunks.

    If the user presses Enter while a late stream is still active, the entered
    prompt is held until the stream ends or goes idle. This avoids sending a new
    ask_model request while the previous agent response is still arriving.
    """
    loop = asyncio.get_running_loop()
    input_future = loop.run_in_executor(None, lambda: input(prompt_text))
    active_stream = False
    stream_state = {}
    pending_prompt = None

    while True:
        if pending_prompt is not None:
            if not active_stream:
                return pending_prompt
            try:
                message = await asyncio.wait_for(
                    incoming_queue.get(),
                    timeout=idle_timeout,
                )
            except asyncio.TimeoutError:
                print(
                    f"\n{C.YELLOW}[stream still active - idle timeout]{C.RESET}",
                    flush=True,
                )
                return pending_prompt
            active_stream = print_unsolicited_stream_message(
                message,
                active_stream,
                stream_state,
                debug_chunks,
            )
            continue

        message_task = asyncio.create_task(incoming_queue.get())
        done, pending = await asyncio.wait(
            {input_future, message_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if input_future in done:
            pending_prompt = input_future.result()
            if message_task in done:
                message = message_task.result()
                was_active = active_stream
                active_stream = print_unsolicited_stream_message(
                    message,
                    active_stream,
                    stream_state,
                    debug_chunks,
                )
                if was_active and not active_stream and pending_prompt is None:
                    print(prompt_text, end="", flush=True)
            else:
                message_task.cancel()
                with suppress(asyncio.CancelledError):
                    await message_task
            continue

        message = message_task.result()
        was_active = active_stream
        active_stream = print_unsolicited_stream_message(
            message,
            active_stream,
            stream_state,
            debug_chunks,
        )
        if was_active and not active_stream:
            print(prompt_text, end="", flush=True)


async def receive_streaming_response(incoming_queue, timeout=120, idle_timeout=8, debug_chunks=False):
    """Receive streaming chunks with real-time printing.

    Returns (full_text, chunk_count, elapsed).
    """
    full_text = ""
    chunk_count = 0
    started = time.time()
    last_chunk_time = None
    completed = False
    stream_state = {}

    try:
        while True:
            recv_timeout = idle_timeout if last_chunk_time else timeout
            message = await asyncio.wait_for(
                incoming_queue.get(),
                timeout=recv_timeout,
            )

            if "chunk_delta" in message:
                chunk_count += 1
                last_chunk_time = time.time()
                delta, is_end = print_stream_chunk(message, stream_state, debug_chunks)
                full_text += delta

                if is_end:
                    completed = True
                    try:
                        await asyncio.wait_for(incoming_queue.get(), timeout=5)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break

            elif message.get("type") == "error":
                print(f"\n{C.RED}ERROR: {message.get('detail')}{C.RESET}")
                completed = True
                break

            elif chunk_count > 0 and ("result" in message or "status" in message):
                completed = True
                break

            else:
                pass

    except asyncio.TimeoutError:
        if chunk_count == 0:
            print(
                f"\n{C.YELLOW}[no response - timeout after {time.time() - started:.0f}s]{C.RESET}"
            )
        else:
            print(
                f"\n{C.YELLOW}[is_message_end not received - idle timeout after "
                f"{time.time() - started:.0f}s, {chunk_count} chunks]{C.RESET}"
            )

    if not completed and last_chunk_time and chunk_count > 0:
        try:
            await asyncio.wait_for(incoming_queue.get(), timeout=5)
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
    debug_chunks: bool = False,
):
    """Run the interactive chat loop over a persistent WebSocket connection."""

    print(f"\n{C.DIM}Connecting to {ws_uri.split('?')[0]}...{C.RESET}")

    async with websockets.connect(ws_uri, max_size=2**20) as ws:
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
        print(f"  {C.YELLOW}/retry{C.RESET}         - Re-send the last message")
        print(f"  {C.YELLOW}/stats{C.RESET}         - Show connection stats")
        print()

        turn = 0
        last_message = None
        incoming_queue = asyncio.Queue()
        reader_task = asyncio.create_task(websocket_reader(ws, incoming_queue))

        try:
            while True:
                try:
                    prompt = await read_prompt_while_receiving(
                        f"{C.BOLD}{C.CYAN}[turn {turn + 1}]{C.RESET} {C.GREEN}You>{C.RESET} ",
                        incoming_queue,
                        debug_chunks=debug_chunks,
                    )
                except (EOFError, KeyboardInterrupt):
                    print(f"\n{C.DIM}Goodbye!{C.RESET}")
                    break

                prompt = prompt.strip()
                if not prompt:
                    continue

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

                print(f"{C.MAGENTA}Agent>{C.RESET} ", end="", flush=True)

                full_text, chunk_count, elapsed = await receive_streaming_response(
                    incoming_queue,
                    timeout=timeout,
                    debug_chunks=debug_chunks,
                )

                print(f" {C.DIM}[{chunk_count} chunks, {elapsed:.1f}s]{C.RESET}")
                print()

                if not full_text and chunk_count == 0:
                    print(
                        f"{C.YELLOW}No response received. "
                        f"Check gateway logs for errors.{C.RESET}\n"
                    )
        finally:
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task


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
    parser.add_argument(
        "--debug-chunks",
        action="store_true",
        help="Print each stream chunk's metadata before its content",
    )

    args = parser.parse_args()

    load_env()

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

    thread_uuid = args.thread_uuid
    if not thread_uuid:
        thread_uuid = str(_uuid.uuid4())

    base_url = (args.gateway_url or "ws://localhost").rstrip("/")
    ws_base = f"{base_url}:{port}"

    if args.token:
        token = args.token
        print(f"{C.DIM}Using provided token: {token[:20]}...{C.RESET}")
    else:
        print(f"{C.DIM}Getting auth token from {ws_base}...{C.RESET}")
        token = asyncio.run(get_auth_token(ws_base, username, password))
        print(f"{C.DIM}Got token: {token[:20]}...{C.RESET}")

    ws_uri = (
        f"{ws_base}/{endpoint_id}/ai_agent_core_ws"
        f"?token={token}&part_id={part_id}"
    )

    try:
        asyncio.run(
            chat_loop(
                ws_uri=ws_uri,
                agent_uuid=agent_uuid,
                thread_uuid=thread_uuid,
                updated_by=updated_by,
                stream=not args.no_stream,
                timeout=args.timeout,
                debug_chunks=args.debug_chunks,
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
