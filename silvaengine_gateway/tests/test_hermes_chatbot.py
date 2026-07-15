#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for Hermes Agent through the A2A Daemon Engine
via the SilvaEngine Gateway.

Opens an SSE connection for real-time streaming and lets you chat
with the Hermes Agent interactively.  Each message you type is sent
through the full pipeline:

    You (stdin)
      → POST /{endpoint_id}/a2a  (message/send)
      → SilvaEngine Gateway → A2ADaemonExecutor → Phase 10 bridge
      → HermesAgentHandler → Hermes API Server (POST /v1/runs + SSE)
      → Token chunks broadcast to SSE stream
      → Printed here in real-time

Prerequisites:
    - Hermes API Server running on http://127.0.0.1:8642
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - Gateway .env at silvaengine_gateway/tests/.env
    - PostgreSQL running with hermes-agent registered

Usage:
    python silvaengine_gateway/tests/test_hermes_chatbot.py

    # With a custom system prompt
    python silvaengine_gateway/tests/test_hermes_chatbot.py --system "You are a pirate"

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

GATEWAY_TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_ENV_FILE = GATEWAY_TESTS_DIR / ".env"
HERMES_AGENT_ID = "hermes-agent"

# Colours
G = "\033[92m"   # green
R = "\033[91m"   # red
C = "\033[96m"   # cyan
Y = "\033[93m"   # yellow
B = "\033[1m"    # bold
D = "\033[2m"    # dim
RST = "\033[0m"


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
    from jose import jwt
    import pendulum
    return jwt.encode(
        {"sub": "chatbot", "username": "chatbot", "role": "admin",
         "iat": pendulum.now("UTC"), "perm": True},
        env.get("JWT_SECRET_KEY", "CHANGEME"),
        algorithm=env.get("JWT_ALGORITHM", "HS256"),
    )


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

    def start(self):
        def _listen():
            active_turn = -1
            local_text = ""
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

                        # Detect a new task turn — reset local accumulator
                        if etype == "task_artifact":
                            tid = event.get("task_id", "")
                            if tid == "streaming-task" or tid == self.current_task_id:
                                # Check if this is a new turn
                                if self._active_turn != active_turn:
                                    active_turn = self._active_turn
                                    local_text = ""

                                artifact = event.get("artifact", {})
                                if isinstance(artifact, dict) and artifact.get("text"):
                                    text = artifact["text"]
                                    # Skip accumulated chunk: it equals or
                                    # starts with what we've already printed
                                    if local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass  # duplicate / accumulated — skip
                                    else:
                                        print(f"{Y}{text}{RST}", end="", flush=True)
                                        local_text += text
                                        self.full_text = local_text

                        # Status events
                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.done.set()

                        # Error events
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
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
        self.current_task_id = task_id
        self.full_text = ""
        self.done.clear()
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


def send_message(gateway_url, token, endpoint_id, part_id, text, task_id,
                 system_prompt=None, conversation_history=None):
    """Send a message/send and return the HTTP response."""
    parts = [{"text": text}]

    metadata = {
        "operation": "task_execution",
        "agent_uuid": HERMES_AGENT_ID,
        "stream": True,
        "task_data": {"task_id": task_id, "task_type": "hermes_chatbot"},
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if conversation_history:
        metadata["conversation_history"] = conversation_history

    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {"role": "ROLE_USER", "parts": parts},
            "metadata": metadata,
        },
        "id": f"chat-{task_id}",
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
        description="Interactive chatbot: Hermes Agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--hermes-url", default=None)
    parser.add_argument("--hermes-key", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--system", default=None,
                        help="System prompt for the agent")
    args = parser.parse_args()

    env = load_env()
    gateway_url = args.gateway_url or f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    hermes_url = args.hermes_url or env.get("HERMES_API_URL", "http://127.0.0.1:8642")
    hermes_key = args.hermes_key or env.get("HERMES_API_KEY", "hermes-local-key")
    endpoint_id = args.endpoint_id or env.get("endpoint_id", "gpt")
    part_id = args.part_id or env.get("part_id", "nestaging")
    token = args.token or gen_token(env)

    # Health checks
    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Hermes Agent Chatbot — A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    try:
        hr = requests.get(f"{hermes_url}/health", timeout=5)
        if hr.status_code != 200:
            print(f"{R}Hermes API Server not healthy: HTTP {hr.status_code}{RST}")
            return
        print(f"{G}✓{RST} Hermes API Server: {hermes_url}")
    except Exception as e:
        print(f"{R}Cannot reach Hermes at {hermes_url}: {e}{RST}")
        return

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
    if args.system:
        print(f"{G}✓{RST} System prompt: {args.system[:60]}...")
    print()

    # Start SSE listener
    sse = SSEListener(gateway_url, token, endpoint_id, part_id)
    sse.start()
    print(f"{D}SSE stream connected. Type a message and press Enter to chat.{RST}")
    print(f"{D}Type 'quit' or 'exit' to leave. Type 'clear' to reset history.{RST}\n")

    conversation_history = []
    turn = 0

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
            print(f"{D}Conversation history cleared.{RST}\n")
            continue

        turn += 1
        task_id = f"chat-{uuid.uuid4().hex[:8]}"
        sse.set_task(task_id)

        print(f"{B}Agent>{RST} ", end="", flush=True)

        # Send the message
        r = send_message(
            gateway_url, token, endpoint_id, part_id,
            user_input, task_id,
            system_prompt=args.system,
            conversation_history=conversation_history if conversation_history else None,
        )

        # Wait for SSE to deliver streaming chunks (max 30s)
        sse.done.wait(timeout=30)

        # Get streaming text from SSE
        streamed_text = sse.full_text

        # If SSE didn't deliver text, fall back to HTTP response
        if not streamed_text:
            try:
                body = r.json()
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
            try:
                body = r.json()
                if body.get("error"):
                    print(f"{R}Error: {body['error'].get('message', '')}{RST}")
                else:
                    print(f"{R}No response received{RST}")
            except Exception:
                print(f"{R}HTTP {r.status_code}: {r.text[:200]}{RST}")

        print()

    # Cleanup
    sse.stop_listening()
    print(f"\n{D}Goodbye! ({turn} turns){RST}")


if __name__ == "__main__":
    main()