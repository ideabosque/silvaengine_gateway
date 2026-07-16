#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSE streaming test for the Core Engine Agent (ai_agent_core_engine) ↔ A2A
Daemon Engine integration.

This script tests the real-time streaming path:

    Client (this script)
      → SilvaEngine Gateway (port 8765)
      → GET  /{endpoint_id}/a2a_sse   (SSE — opens long-lived connection)
      → POST /{endpoint_id}/a2a       (message/send — triggers streaming)
      → A2ADaemonExecutor → Phase 10 bridge → CoreEngineAgentHandler
      → WS  /{endpoint_id}/ai_agent_core_ws  (streaming via WebSocket)
      → ai_agent_core_engine (ask_model stream=true → chunk_delta frames)
      → Token chunks broadcast to the SSE stream
      → Client receives live chunks on the SSE connection

Unlike the Hermes SSE test, no external API server is needed — the Core
Engine bridge routes back through the gateway's own ai_agent_core_ws route.

The script opens an SSE connection in a background thread, sends a
message/send request in the foreground, and prints the streaming
chunks as they arrive in real-time.

Prerequisites:
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - ai_agent_core_engine installed and importable
    - a2a_daemon_engine installed and importable
    - Gateway .env at silvaengine_gateway/silvaengine_gateway/tests/.env
      with ADMIN_STATIC_TOKEN, endpoint_id, part_id, db_backend=postgresql
    - PostgreSQL container running
    - A2A_STREAMING_ENABLED=true in the gateway .env

Usage:
    python silvaengine_gateway/tests/test_core_engine_sse_live.py

    # With custom prompt
    python silvaengine_gateway/tests/test_core_engine_sse_live.py \
        --prompt "Write a haiku about AI"

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GATEWAY_TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_ENV_FILE = GATEWAY_TESTS_DIR / ".env"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
CORE_ENGINE_AGENT_ID = "core-engine-agent"
DEFAULT_AGENT_UUID = "agent-1780802783-70468776"


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def load_gateway_env():
    if not GATEWAY_ENV_FILE.exists():
        return {}
    env = {}
    with open(GATEWAY_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            if key:
                env[key] = value
    return env


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

class _C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _info(msg):
    print(f"  {_C.CYAN}[INFO]{_C.RESET} {msg}")


def _pass(name, detail=""):
    print(f"  {_C.GREEN}[PASS]{_C.RESET} {name}")
    if detail:
        print(f"       {detail}")


def _fail(name, detail=""):
    print(f"  {_C.RED}[FAIL]{_C.RESET} {name}")
    if detail:
        print(f"       {detail}")


def _section(title):
    print(f"\n{_C.BOLD}{'=' * 80}{_C.RESET}")
    print(f"{_C.CYAN}{title}{_C.RESET}")
    print(f"{_C.BOLD}{'=' * 80}{_C.RESET}\n")


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token(env):
    static = env.get("ADMIN_STATIC_TOKEN", "")
    auth_provider = env.get("GATEWAY_AUTH_PROVIDER", "local")

    if static and auth_provider != "cognito":
        return static

    jwt_secret = env.get("JWT_SECRET_KEY", "CHANGEME")
    jwt_algo = env.get("JWT_ALGORITHM", "HS256")

    try:
        from jose import jwt
        import pendulum

        return jwt.encode(
            {"sub": "sse-test", "username": "sse-test", "role": "admin",
             "iat": pendulum.now("UTC"), "perm": True},
            jwt_secret, algorithm=jwt_algo,
        )
    except ImportError:
        sys.path.insert(0, str(GATEWAY_TESTS_DIR.parent.parent))
        from silvaengine_gateway.auth.jwt_local import create_local_jwt
        from silvaengine_gateway.config import GatewayConfig
        import logging

        GatewayConfig.initialize(logging.getLogger("e2e"), {
            "jwt_secret_key": jwt_secret,
            "jwt_algorithm": jwt_algo,
            "access_token_exp": 15,
            "admin_static_token": "",
        })
        return create_local_jwt(
            {"username": "sse-test", "role": "admin"}, forever=True,
        )


# ---------------------------------------------------------------------------
# Agent registration via gateway GraphQL (mirrors test_core_engine_gateway_live.py)
# ---------------------------------------------------------------------------

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
                return body["data"]["insertUpdateA2aAgent"]
            if body.get("errors"):
                _info(f"GraphQL errors (may be OK): {body['errors'][0].get('message', '')[:120]}")
        return {}
    except Exception as e:
        _info(f"GraphQL registration skipped: {e}")
        return {}


# ---------------------------------------------------------------------------
# SSE listener thread
# ---------------------------------------------------------------------------

def start_sse_listener(gateway_url, token, endpoint_id, part_id, partition_key,
                       received_events, stop_event):
    """Open a GET SSE connection and collect events until stop_event is set."""

    def _listen():
        try:
            r = requests.get(
                f"{gateway_url}/{endpoint_id}/a2a_sse",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Part-Id": part_id,
                    "Accept": "text/event-stream",
                },
                stream=True,
                timeout=120,
            )
            if r.status_code != 200:
                _info(f"SSE connection returned HTTP {r.status_code}")
                return

            _info(f"SSE connection opened (HTTP {r.status_code})")

            for line in r.iter_lines(decode_unicode=True):
                if stop_event.is_set():
                    break
                if not line:
                    continue
                # Parse SSE data lines
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                        received_events.append(event)
                        # Print the event in real-time
                        event_type = event.get("type", event.get("event", "unknown"))
                        artifact = event.get("artifact", {})
                        if isinstance(artifact, dict) and artifact.get("text"):
                            print(f"  {_C.YELLOW}chunk{_C.RESET}: {artifact['text']}", flush=True)
                        elif "delta" in event:
                            print(f"  {_C.YELLOW}chunk{_C.RESET}: {event['delta']}", flush=True)
                        elif "text" in event:
                            print(f"  {_C.YELLOW}chunk{_C.RESET}: {event['text']}", flush=True)
                        elif event_type in ("status", "task_status"):
                            state = event.get("state", event.get("status", ""))
                            print(f"  {_C.DIM}status: {state}{_C.RESET}", flush=True)
                        else:
                            print(f"  {_C.DIM}event: {data_str[:120]}{_C.RESET}", flush=True)
                    except json.JSONDecodeError:
                        print(f"  {_C.DIM}raw: {data_str[:120]}{_C.RESET}", flush=True)
                elif line.startswith(":"):
                    # SSE comment / heartbeat
                    pass
                else:
                    print(f"  {_C.DIM}sse: {line[:120]}{_C.RESET}", flush=True)

        except requests.exceptions.ConnectionError:
            pass  # Expected when stop_event closes the connection
        except Exception as e:
            _info(f"SSE listener error: {e}")

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_error_text(text):
    """Check if response text is actually an error message, not a real response."""
    if not text:
        return False
    lower = text.lower()
    return lower.startswith("ai agent error:") or lower.startswith("error:")


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_sse_test(gateway_url, token, endpoint_id, part_id, prompt,
                 core_engine_agent_uuid):
    _section("Core Engine Agent SSE Streaming Test")

    # 0. Register the agent with full metadata
    _info("Registering core-engine-agent with full metadata...")
    register_core_engine_agent(
        gateway_url, token, endpoint_id, part_id, core_engine_agent_uuid,
    )

    partition_key = f"{endpoint_id}#{part_id}"
    received_events = []
    stop_event = threading.Event()

    # 1. Start SSE listener in background
    _info("Opening SSE connection...")
    sse_thread = start_sse_listener(
        gateway_url, token, endpoint_id, part_id, partition_key,
        received_events, stop_event,
    )
    time.sleep(2)  # Give SSE connection time to establish

    # 2. Send a message/send that triggers streaming
    _info("Sending streaming message/send...")

    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"text": prompt}],
            },
            "metadata": {
                "operation": "task_execution",
                "agent_uuid": CORE_ENGINE_AGENT_ID,
                "stream": True,
                "task_data": {
                    "task_type": "ce_sse_test",
                },
            },
        },
        "id": "ce-sse-stream",
    }

    print()
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
    print()
    _info(f"message/send response: HTTP {r.status_code}")

    # Give SSE a moment to receive any final events
    time.sleep(3)

    # 3. Stop SSE listener
    stop_event.set()
    sse_thread.join(timeout=5)

    # 4. Report results
    _section("SSE Streaming Results")
    _info(f"Total SSE events received: {len(received_events)}")

    # Extract all text from events
    full_text = ""
    for event in received_events:
        if isinstance(event, dict):
            # task_artifact shape: {type: "task_artifact", artifact: {type: "text", text: "..."}}
            artifact = event.get("artifact", {})
            if isinstance(artifact, dict) and artifact.get("text"):
                full_text += artifact["text"]
            elif "delta" in event:
                full_text += event["delta"]
            elif "text" in event:
                full_text += event["text"]
            elif "parts" in event:
                for p in event["parts"]:
                    if isinstance(p, dict):
                        full_text += p.get("text", "")

    if received_events:
        _pass("SSE events received", f"{len(received_events)} events")
        if full_text:
            if is_error_text(full_text):
                _fail("Streaming text is error", f"'{full_text[:120]}'")
            else:
                _pass("Streaming text received", f"{len(full_text)} chars: '{full_text[:120]}'")
        else:
            # Check for failed status events
            has_failed = any(
                isinstance(e, dict) and e.get("state") == "failed"
                for e in received_events
            )
            if has_failed:
                _fail("SSE status: failed", "Streaming returned a failed status")
            else:
                _info("Events received but no text (status-only events)")
    else:
        # No SSE events — check if the message/send response has text
        try:
            resp_body = r.json()
            resp_text = ""
            result = resp_body.get("result", {})
            if isinstance(result, dict):
                parts = result.get("parts", [])
                if parts:
                    resp_text = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in parts
                    )
            if resp_text:
                if is_error_text(resp_text):
                    _fail("Response is error", f"'{resp_text[:120]}'")
                else:
                    _pass("Response text (non-streaming fallback)", f"'{resp_text[:120]}'")
                    _info("No SSE events — the gateway may not have an SSE endpoint configured,")
                    _info("or the SSE manager was not initialized. The message/send still works.")
            else:
                _fail("No streaming response", "Neither SSE events nor response text received")
        except Exception:
            _fail("No streaming response", f"Response: {r.text[:200]}")

    # Verify task-message link in DB
    try:
        import psycopg2 as _pg
        conn = _pg.connect(host="localhost", port="5432", user="silvaengine", password="silvaengine", dbname="silvaengine")
        cur = conn.cursor()
        cur.execute("SELECT task_id FROM a2a_tasks ORDER BY created_at DESC LIMIT 1")
        task_row = cur.fetchone()
        if task_row:
            task_id_db = task_row[0]
            cur.execute("SELECT message_id FROM a2a_messages WHERE task_id = %s ORDER BY created_at DESC LIMIT 1", (task_id_db,))
            msg_row = cur.fetchone()
            if msg_row:
                _pass("Task-Message Link", f"task_id={task_id_db}, message_id={msg_row[0]}")
            else:
                _info(f"Task-Message Link: task {task_id_db} has no linked message")
        else:
            _info("Task-Message Link: no tasks in DB")
        cur.close()
        conn.close()
    except Exception as e:
        _info(f"Task-Message Link: DB check failed: {e}")

    print(f"\n{_C.BOLD}{'=' * 80}{_C.RESET}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SSE streaming test: Core Engine Agent through SilvaEngine Gateway"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument(
        "--core-engine-agent-uuid", default=None,
        help="ai_agent_core_engine agent UUID (default: from .env or DEFAULT_AGENT_UUID)",
    )
    parser.add_argument(
        "--prompt", default="Say hello and introduce yourself briefly.",
    )
    args = parser.parse_args()

    env = load_gateway_env()
    gateway_url = args.gateway_url or f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    endpoint_id = args.endpoint_id or env.get("endpoint_id", "gpt")
    part_id = args.part_id or env.get("part_id", "nestaging")
    token = args.token or generate_token(env)
    core_engine_agent_uuid = (
        args.core_engine_agent_uuid
        or env.get("CORE_ENGINE_AGENT_UUID")
        or DEFAULT_AGENT_UUID
    )

    run_sse_test(gateway_url, token, endpoint_id, part_id, args.prompt,
                 core_engine_agent_uuid)


if __name__ == "__main__":
    main()