#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for Core Engine Agent through the A2A Daemon Engine
via the SilvaEngine Gateway.

Opens an SSE connection for real-time streaming and lets you chat
with the ai_agent_core_engine agent interactively.  Each message you
type is sent through the full pipeline:

    You (stdin)
      → POST /{endpoint_id}/a2a  (message/send)
      → SilvaEngine Gateway → A2ADaemonExecutor → Phase 10 bridge
      → CoreEngineAgentHandler → WS /{endpoint_id}/ai_agent_core_ws
      → ai_agent_core_engine (ask_model stream=true → chunk_delta frames)
      → Token chunks broadcast to SSE stream
      → Printed here in real-time

Unlike the Hermes chatbot, no external API server is needed — the Core
Engine bridge routes back through the gateway's own ai_agent_core_ws
route to the in-process ai_agent_core_engine module.

Prerequisites:
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - ai_agent_core_engine installed and importable
    - a2a_daemon_engine installed and importable
    - Gateway .env at silvaengine_gateway/tests/.env
    - PostgreSQL running with core-engine-agent registered

Usage:
    python silvaengine_gateway/tests/test_core_engine_chatbot.py

    # With a custom system prompt
    python silvaengine_gateway/tests/test_core_engine_chatbot.py --system "You are a pirate"

Author: bibow
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

__author__ = "bibow"

# This script prints check marks and coloured output. On a Windows console
# (cp1252) those raise UnicodeEncodeError, which the health-check's broad
# `except Exception` then reports as "Cannot reach gateway" — pointing at the
# wrong thing entirely. Force UTF-8 so the output is the output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - older/odd streams
        pass

GATEWAY_TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_ENV_FILE = GATEWAY_TESTS_DIR / ".env"
CORE_ENGINE_AGENT_ID = "core-engine-agent"
DEFAULT_AGENT_UUID = "agent-1780802783-70468776"

# Colours
G = "\033[92m"   # green
R = "\033[91m"   # red
C = "\033[96m"   # cyan
Y = "\033[93m"   # yellow
M = "\033[95m"   # magenta
B = "\033[1m"    # bold
D = "\033[2m"    # dim
RST = "\033[0m"


# ---------------------------------------------------------------------------
# Reasoning detection (mirrors chat_websocket.py)
# ---------------------------------------------------------------------------
# chat_websocket.py reads the raw WebSocket frames, where ai_agent_core marks
# reasoning tokens with an "rs#" marker in suffix / message_group_id. Here the
# tokens arrive as A2A SSE artifacts, so the A2A bridge forwards those same
# frame fields under artifact["metadata"] — see a2a_core_engine_handler
# (_STREAM_META_KEYS) and a2a_ai_agent_utility._emit_to_sse.


def _find_reasoning_marker(metadata):
    """Return a reasoning marker such as rs#1 when present in artifact metadata."""
    if not isinstance(metadata, dict):
        return ""
    for key in ("suffix", "message_group_id", "data_format", "type"):
        value = str(metadata.get(key) or "")
        lowered = value.lower()
        marker_index = lowered.find("rs#")
        if marker_index >= 0:
            marker = value[marker_index:].split("-", 1)[0].split("/", 1)[0]
            return marker.strip()
        if "reason" in lowered:
            return value.strip() or "reasoning"
    return ""


def load_env():
    env = {}
    if GATEWAY_ENV_FILE.exists():
        with open(GATEWAY_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.split(" #")[0].strip()
                if k.strip():
                    env[k.strip()] = v
    return env


def gen_token(env):
    static = env.get("ADMIN_STATIC_TOKEN", "")
    auth_provider = env.get("GATEWAY_AUTH_PROVIDER", "local")
    if static and auth_provider != "cognito":
        return static

    try:
        from jose import jwt
        import pendulum
        return jwt.encode(
            {"sub": "chatbot", "username": "chatbot", "role": "admin",
             "iat": pendulum.now("UTC"), "perm": True},
            env.get("JWT_SECRET_KEY", "CHANGEME"),
            algorithm=env.get("JWT_ALGORITHM", "HS256"),
        )
    except ImportError:
        sys.path.insert(0, str(GATEWAY_TESTS_DIR.parent.parent))
        from silvaengine_gateway.auth.jwt_local import create_local_jwt
        from silvaengine_gateway.config import GatewayConfig
        import logging

        GatewayConfig.initialize(logging.getLogger("chatbot"), {
            "jwt_secret_key": env.get("JWT_SECRET_KEY", "CHANGEME"),
            "jwt_algorithm": env.get("JWT_ALGORITHM", "HS256"),
            "access_token_exp": 15,
            "admin_static_token": "",
        })
        return create_local_jwt(
            {"username": "chatbot", "role": "admin"}, forever=True,
        )


def register_core_engine_agent(gateway_url, token, endpoint_id, part_id,
                                core_engine_agent_uuid):
    """Register the core-engine-agent fixture with full handler metadata."""
    metadata = {
        "module_name": "a2a_daemon_engine.handlers.a2a_core_engine_handler",
        "class_name": "CoreEngineAgentHandler",
        "core_engine_graphql_url": gateway_url,
        "core_engine_ws_url": gateway_url.replace("http://", "ws://"),
        "core_engine_token": token,
        "core_engine_agent_uuid": core_engine_agent_uuid,
        "core_engine_updated_by": "a2a-daemon",
        "core_engine_stream_timeout": 120.0,
    }
    mutation = """
        mutation RegisterCoreEngineAgent(
            $endpointId: String!
            $partId: String!
            $agentId: String
            $agentName: String!
            $endpointUrl: String!
            $updatedBy: String!
            $metadata: JSON
        ) {
            insertUpdateA2aAgent(
                endpointId: $endpointId
                partId: $partId
                agentId: $agentId
                agentName: $agentName
                endpointUrl: $endpointUrl
                updatedBy: $updatedBy
                metadata: $metadata
            ) {
                a2aAgent { agentId agentName }
            }
        }
    """
    variables = {
        "endpointId": endpoint_id,
        "partId": part_id,
        "agentId": CORE_ENGINE_AGENT_ID,
        "agentName": "Core Engine Agent",
        "endpointUrl": gateway_url,
        "updatedBy": "e2e-test",
        "metadata": metadata,
    }
    try:
        r = requests.post(
            f"{gateway_url}/{endpoint_id}/a2a_core_graphql",
            json={"query": mutation, "variables": variables},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Part-Id": part_id,
            },
            timeout=30,
        )
        if r.status_code == 200:
            body = r.json()
            if "body" in body:
                body = json.loads(body["body"])
            if body.get("data", {}).get("insertUpdateA2aAgent"):
                print(f"{G}✓{RST} Agent registered: {CORE_ENGINE_AGENT_ID}")
            elif body.get("errors"):
                print(f"{D}Agent registration: {body['errors'][0].get('message', '')[:80]}{RST}")
        else:
            print(f"{D}Agent registration: HTTP {r.status_code}{RST}")
    except Exception as e:
        print(f"{D}Agent registration skipped: {e}{RST}")


class SSEListener:
    """Background SSE listener that prints streaming chunks in real-time."""

    def __init__(self, gateway_url, token, endpoint_id, part_id):
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.stop = False
        self.thread = None
        self.current_task_id = None
        self.full_text = ""
        self.done = threading.Event()
        self._turn = 0  # increments on each new message
        self._active_turn = -1  # the turn currently being listened to
        # Reasoning/response section tracking (see _print_boundary)
        self._mode = None    # "reasoning" | "response"
        self._marker = ""    # e.g. rs#1
        # Accumulated text of the CURRENT section only, used to de-duplicate
        # accumulated chunks. self.full_text stays answer-only for history.
        self._section_text = ""

    def _print_boundary(self, metadata):
        """Print a section header when the stream switches reasoning <-> response.

        Returns True when a NEW section started, so the caller can restart the
        per-section accumulator.
        """
        marker = _find_reasoning_marker(metadata)
        mode = "reasoning" if marker else "response"
        if mode == self._mode and marker == self._marker:
            return False

        self._close_boundary()

        if mode == "reasoning":
            label = f" {marker}" if marker else ""
            print(f"\n{Y}>>> REASONING START{label}{RST}", flush=True)
        else:
            print(f"\n{M}>>> RESPONSE START{RST}", flush=True)

        self._mode = mode
        self._marker = marker
        return True

    def _close_boundary(self):
        """Close the currently open section, if any."""
        if self._mode == "reasoning":
            label = f" {self._marker}" if self._marker else ""
            print(f"\n{Y}<<< REASONING END{label}{RST}", flush=True)
        elif self._mode == "response":
            print(f"\n{M}<<< RESPONSE END{RST}", flush=True)
        self._mode = None
        self._marker = ""

    def start(self):
        def _listen():
            try:
                r = requests.get(
                    f"{self.gateway_url}/{self.endpoint_id}/a2a_sse",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Part-Id": self.part_id,
                        "Accept": "text/event-stream",
                    },
                    stream=True,
                    timeout=300,
                )
                if r.status_code != 200:
                    print(f"{R}SSE connection failed: HTTP {r.status_code}{RST}")
                    return

                for line in r.iter_lines(decode_unicode=True):
                    if self.stop:
                        break
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", event.get("event", ""))

                        # The gateway SSE stream is partition-scoped: every
                        # client on this partition receives EVERY conversation's
                        # events. Without this filter a second chatbot session
                        # bleeds its tokens into ours, and worse, its
                        # "completed" status ends our turn early — which looks
                        # like "no response". Events carry the daemon's task_id
                        # (our thread_uuid); anything else belongs to someone
                        # else. Events with no task_id (connected/heartbeat)
                        # are kept.
                        # Note the missing `self.current_task_id and ...` guard:
                        # the listener connects before the first set_task(), so
                        # while current_task_id is still None any in-flight
                        # session's tokens would leak in. Anything carrying a
                        # task_id that isn't ours is dropped, unconditionally.
                        event_task_id = event.get("task_id")
                        if event_task_id and event_task_id != self.current_task_id:
                            continue

                        # Streaming text chunks — print deltas in real-time
                        artifact = event.get("artifact", {})
                        if isinstance(artifact, dict) and artifact.get("text"):
                            text = artifact["text"]
                            # The A2A bridge forwards the originating stream
                            # frame's fields here so reasoning tokens can be
                            # told apart from answer tokens.
                            meta = artifact.get("metadata")
                            # Print only the new delta (subtract already-printed
                            # text if the gateway sends accumulated chunks)
                            # Reasoning and the answer are SEPARATE accumulating
                            # streams (ai_agent_core sends accumulated text per
                            # message index, and reasoning uses its own index).
                            # Dedup within a section only — comparing across
                            # sections corrupts the accumulator and reprints the
                            # answer. A new section restarts it.
                            if self._print_boundary(meta):
                                self._section_text = ""

                            if self._section_text and text.startswith(self._section_text):
                                delta = text[len(self._section_text):]
                                self._section_text = text
                            elif self._section_text and self._section_text.startswith(text):
                                # Older or duplicate chunk — skip
                                delta = ""
                                self._section_text = text
                            else:
                                # New non-accumulated chunk — print as-is
                                delta = text
                                self._section_text += text

                            if delta:
                                if self._mode == "reasoning":
                                    print(f"{D}{delta}{RST}", end="", flush=True)
                                else:
                                    # full_text feeds conversation_history, so it
                                    # must hold the answer only — never reasoning.
                                    print(f"{Y}{delta}{RST}", end="", flush=True)
                                    self.full_text += delta

                        # Also handle raw delta/text fields (non-artifact events)
                        elif "delta" in event:
                            delta = event["delta"]
                            if delta:
                                print(f"{Y}{delta}{RST}", end="", flush=True)
                                self.full_text += delta
                        elif "text" in event and etype not in ("task_status", "status"):
                            text = event["text"]
                            if text:
                                print(f"{Y}{text}{RST}", end="", flush=True)
                                self.full_text += text

                        # Status events
                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self._close_boundary()
                                self.done.set()
                            elif state in ("working", "WORKING", "in_progress"):
                                print(f"{D}[{state}...]{RST}", end="", flush=True)
                            elif state in ("input_required", "INPUT_REQUIRED",
                                           "auth_required", "AUTH_REQUIRED"):
                                self._close_boundary()
                                print(f"\n{D}[{state}]{RST}", flush=True)
                                self.done.set()

                        # Error events
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                self._close_boundary()
                                print(f"\n{R}Error: {err}{RST}", flush=True)
                                self.done.set()

                    elif line.startswith("event: "):
                        pass  # SSE event type line

            except requests.exceptions.ConnectionError:
                pass
            except Exception as e:
                if not self.stop:
                    print(f"{R}SSE error: {e}{RST}")

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        time.sleep(1)  # Let connection establish

    def set_task(self, task_id):
        """Scope the listener to one conversation.

        ``task_id`` must match what the daemon stamps on its SSE events —
        i.e. the thread_uuid we send, not the client-side JSON-RPC request id.
        """
        self.current_task_id = task_id
        self.full_text = ""
        self._mode = None
        self._marker = ""
        self._section_text = ""
        self.done.clear()

    def stop_listening(self):
        self.stop = True


def send_message(gateway_url, token, endpoint_id, part_id, text, request_id,
                 system_prompt=None, conversation_history=None,
                 thread_uuid=None):
    """Send a message/send and return the HTTP response."""
    parts = [{"text": text}]
    metadata = {
        "operation": "task_execution",
        "agent_uuid": CORE_ENGINE_AGENT_ID,
        "stream": True,
        "task_data": {"task_type": "ce_chatbot"},
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if conversation_history:
        metadata["conversation_history"] = conversation_history
    if thread_uuid:
        metadata["thread_uuid"] = thread_uuid

    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {"role": "ROLE_USER", "parts": parts},
            "metadata": metadata,
        },
        "id": f"chat-{request_id}",
    }

    r = requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Part-Id": part_id,
        },
        timeout=180,
    )
    return r


def extract_text_from_response(body):
    """Extract text from a JSON-RPC response."""
    result = body.get("result", {})
    if isinstance(result, dict):
        parts = result.get("parts", [])
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in parts
        )
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Interactive chatbot: Core Engine Agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--core-engine-agent-uuid", default=None,
                        help="ai_agent_core_engine agent UUID")
    parser.add_argument("--system", default=None,
                        help="System prompt for the agent")
    args = parser.parse_args()

    env = load_env()
    gateway_url = args.gateway_url or f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    endpoint_id = args.endpoint_id or env.get("endpoint_id", "gpt")
    part_id = args.part_id or env.get("part_id", "nestaging")
    token = args.token or gen_token(env)
    core_engine_agent_uuid = (
        args.core_engine_agent_uuid
        or env.get("CORE_ENGINE_AGENT_UUID")
        or DEFAULT_AGENT_UUID
    )

    # Header
    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Core Engine Agent Chatbot — A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    # Health check
    try:
        gr = requests.get(f"{gateway_url}/health", timeout=5)
        if gr.status_code != 200:
            print(f"{R}Gateway not healthy: HTTP {gr.status_code}{RST}")
            return
        print(f"{G}✓{RST} Gateway: {gateway_url}")
    except Exception as e:
        print(f"{R}Cannot reach gateway at {gateway_url}: {e}{RST}")
        return

    print(f"{G}✓{RST} Endpoint: {endpoint_id}/{part_id}")
    print(f"{G}✓{RST} Core Engine Agent: {core_engine_agent_uuid}")
    if args.system:
        print(f"{G}✓{RST} System prompt: {args.system[:60]}...")
    print()

    # Register agent with full metadata
    register_core_engine_agent(
        gateway_url, token, endpoint_id, part_id, core_engine_agent_uuid,
    )

    # Start SSE listener
    sse = SSEListener(gateway_url, token, endpoint_id, part_id)
    sse.start()
    print(f"{D}SSE stream connected. Type a message and press Enter to chat.{RST}")
    print(f"{D}Type 'quit' or 'exit' to leave. Type 'clear' to reset history.{RST}\n")

    conversation_history = []
    turn = 0
    thread_uuid = str(uuid.uuid4())  # Persist across turns for conversation continuity

    while True:
        try:
            user_input = input(f"{B}{C}You>{RST} ")
        except (EOFError, KeyboardInterrupt):
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", ":q"):
            break
        if user_input.lower() == "clear":
            conversation_history = []
            thread_uuid = str(uuid.uuid4())  # New thread on clear
            print(f"{D}Conversation history cleared.{RST}\n")
            continue

        turn += 1
        # Use a simple request ID for JSON-RPC; the daemon generates
        # its own task_id for DB persistence.
        request_id = f"chat-{uuid.uuid4().hex[:8]}"
        # Scope SSE to this conversation. The daemon stamps its streaming
        # events with the thread_uuid, so that — not request_id — is what
        # identifies our events on the shared partition stream.
        sse.set_task(thread_uuid)

        print(f"{B}Agent>{RST} ", end="", flush=True)

        # Send the message in a background thread so the SSE listener
        # can receive streaming chunks while the HTTP request is in flight.
        http_result = {"response": None, "error": None}

        def _send():
            try:
                http_result["response"] = send_message(
                    gateway_url, token, endpoint_id, part_id,
                    user_input, request_id,
                    system_prompt=args.system,
                    conversation_history=conversation_history if conversation_history else None,
                    thread_uuid=thread_uuid,
                )
            except Exception as e:
                http_result["error"] = str(e)

        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()

        # Wait for either SSE "completed" or the HTTP response to arrive
        # (whichever signals the turn is done). Poll both with a 120s cap.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if sse.done.is_set() or http_result["response"] is not None:
                break
            if http_result["error"] is not None:
                break
            time.sleep(0.5)

        send_thread.join(timeout=5)

        # Get streaming text from SSE
        streamed_text = sse.full_text

        # If SSE didn't deliver text, fall back to HTTP response
        if not streamed_text and http_result["response"] is not None:
            try:
                body = http_result["response"].json()
                streamed_text = extract_text_from_response(body)
            except Exception:
                streamed_text = ""

        print()  # newline after streaming chunks

        if streamed_text:
            if not sse.full_text:
                # Text came from HTTP response, not SSE — print it now
                print(f"{G}{streamed_text}{RST}")
            print(f"{D}({len(streamed_text)} chars){RST}")
            # Add to conversation history
            conversation_history.append({"role": "user", "content": user_input})
            conversation_history.append({"role": "assistant", "content": streamed_text})
        else:
            # Check for errors
            if http_result["error"]:
                print(f"{R}Error: {http_result['error']}{RST}")
            elif http_result["response"] is not None:
                try:
                    body = http_result["response"].json()
                    if body.get("error"):
                        print(f"{R}Error: {body['error'].get('message', '')}{RST}")
                    else:
                        print(f"{R}No response received{RST}")
                except Exception:
                    print(f"{R}HTTP {http_result['response'].status_code}: {http_result['response'].text[:200]}{RST}")
            else:
                print(f"{R}No response received (timeout){RST}")

        print()

    # Cleanup
    sse.stop_listening()

    # Verify task-message link in DB
    try:
        import psycopg2 as _pg
        conn = _pg.connect(host="localhost", port="5432", user="silvaengine", password="silvaengine", dbname="silvaengine")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM a2a_tasks")
        task_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM a2a_messages WHERE task_id IS NOT NULL")
        linked_msg_count = cur.fetchone()[0]
        cur.close()
        conn.close()
        if task_count > 0 and linked_msg_count > 0:
            print(f"{D}DB: {task_count} tasks, {linked_msg_count} linked messages{RST}")
        else:
            print(f"{D}DB: {task_count} tasks, {linked_msg_count} linked messages (persistence may not be loaded){RST}")
    except Exception as e:
        print(f"{D}DB check skipped: {e}{RST}")

    print(f"\n{D}Goodbye! ({turn} turns){RST}")


if __name__ == "__main__":
    main()