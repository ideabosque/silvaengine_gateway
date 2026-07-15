#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSE streaming test for the Hermes Agent ↔ A2A Daemon Engine integration.

This script tests the real-time streaming path:

    Client (this script)
      → SilvaEngine Gateway (port 8765)
      → POST /{endpoint_id}/a2a_sse  (SSE GET — opens long-lived connection)
      → POST /{endpoint_id}/a2a      (message/send — triggers streaming)
      → A2ADaemonExecutor → Phase 10 bridge → HermesAgentHandler
      → Hermes API Server (POST /v1/runs + GET /v1/runs/{id}/events SSE)
      → Token chunks broadcast to the SSE stream
      → Client receives live chunks on the SSE connection

The script opens an SSE connection in a background thread, sends a
message/send request in the foreground, and prints the streaming
chunks as they arrive in real-time.

Prerequisites:
    - Hermes API Server running on http://127.0.0.1:8642
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - Gateway .env at silvaengine_gateway/silvaengine_gateway/tests/.env
    - PostgreSQL container running with hermes-agent registered

Usage:
    python a2a_daemon_engine/tests/test_hermes_sse_live.py

    # With custom prompt
    python a2a_daemon_engine/tests/test_hermes_sse_live.py --prompt "Write a haiku about AI"

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
DEFAULT_HERMES_URL = "http://127.0.0.1:8642"
HERMES_AGENT_ID = "hermes-agent"


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

    from jose import jwt
    import pendulum

    return jwt.encode(
        {"sub": "sse-test", "username": "sse-test", "role": "admin",
         "iat": pendulum.now("UTC"), "perm": True},
        jwt_secret, algorithm=jwt_algo,
    )


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
# Main test
# ---------------------------------------------------------------------------

def run_sse_test(gateway_url, hermes_url, hermes_key, token, endpoint_id, part_id, prompt):
    _section("Hermes Agent SSE Streaming Test")

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
    task_id = f"hermes-sse-{uuid.uuid4().hex[:8]}"
    _info(f"Sending message: '{prompt}'")
    _info(f"Task ID: {task_id}")

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
                "agent_uuid": HERMES_AGENT_ID,
                "stream": True,
                "task_data": {"task_id": task_id, "task_type": "hermes_sse_test"},
            },
        },
        "id": "hermes-sse-test-001",
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
            _pass("Streaming text received", f"{len(full_text)} chars: '{full_text[:120]}'")
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
                _pass("Response text (non-streaming fallback)", f"'{resp_text[:120]}'")
                _info("No SSE events — the gateway may not have an SSE endpoint configured,")
                _info("or the SSE manager was not initialized. The message/send still works.")
            else:
                _fail("No streaming response", "Neither SSE events nor response text received")
        except Exception:
            _fail("No streaming response", f"Response: {r.text[:200]}")

    # 5. Check Hermes API directly for comparison
    _section("Direct Hermes API Verification")
    hermes_r = requests.post(
        f"{hermes_url}/v1/runs",
        headers={"Authorization": f"Bearer {hermes_key}", "Content-Type": "application/json"},
        json={"input": prompt, "conversation_history": []},
        timeout=30,
    )
    if hermes_r.status_code in (200, 202):
        run_id = hermes_r.json().get("run_id")
        _pass("Hermes /v1/runs", f"run_id={run_id}")
        if run_id:
            # Stream events from Hermes directly
            sse_r = requests.get(
                f"{hermes_url}/v1/runs/{run_id}/events",
                headers={"Authorization": f"Bearer {hermes_key}"},
                stream=True,
                timeout=30,
            )
            if sse_r.status_code == 200:
                _pass("Hermes SSE stream", f"GET /v1/runs/{run_id}/events")
                hermes_text = ""
                for line in sse_r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            event_type = event.get("event", event.get("type", ""))
                            if event_type in ("message.delta", "response.output_text.delta"):
                                delta = event.get("delta", "")
                                hermes_text += delta
                                print(f"  {_C.YELLOW}hermes chunk{_C.RESET}: {delta}", end="", flush=True)
                            elif event_type in ("run.completed", "response.completed"):
                                break
                        except json.JSONDecodeError:
                            pass
                print()
                if hermes_text:
                    _pass("Hermes streaming text", f"'{hermes_text[:120]}'")
            else:
                _fail("Hermes SSE stream", f"HTTP {sse_r.status_code}")
    else:
        _fail("Hermes /v1/runs", f"HTTP {hermes_r.status_code}")

    print(f"\n{_C.BOLD}{'=' * 80}{_C.RESET}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SSE streaming test: Hermes Agent through SilvaEngine Gateway"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--hermes-url", default=None)
    parser.add_argument("--hermes-key", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--prompt", default="Say hello and introduce yourself briefly.")
    args = parser.parse_args()

    env = load_gateway_env()
    gateway_url = args.gateway_url or f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    hermes_url = args.hermes_url or env.get("HERMES_API_URL", "http://127.0.0.1:8642")
    hermes_key = args.hermes_key or env.get("HERMES_API_KEY", "hermes-local-key")
    endpoint_id = args.endpoint_id or env.get("endpoint_id", "gpt")
    part_id = args.part_id or env.get("part_id", "nestaging")
    token = args.token or generate_token(env)

    run_sse_test(gateway_url, hermes_url, hermes_key, token, endpoint_id, part_id, args.prompt)


if __name__ == "__main__":
    main()