#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot "say hello from Hermes" through the A2A Daemon Engine
via the SilvaEngine Gateway.

Pipeline:
    Client -> POST /{endpoint_id}/a2a (message/send)
    -> SilvaEngine Gateway -> A2ADaemonExecutor -> HermesAgentHandler
    -> Hermes API Server (http://127.0.0.1:8642)
    -> streamed back via SSE

Prerequisites:
    - Hermes API Server running on http://127.0.0.1:8642
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - Gateway .env at silvaengine_gateway/tests/.env

Usage:
    python silvaengine_gateway/tests/test_hermes_hello_sse.py

Author: bibow
"""
from __future__ import print_function

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

PROMPT = "Say hello from Hermes through A2A. Reply in one short sentence."


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
        {"sub": "hello", "username": "hello", "role": "admin",
         "iat": pendulum.now("UTC"), "perm": True},
        env.get("JWT_SECRET_KEY", "CHANGEME"),
        algorithm=env.get("JWT_ALGORITHM", "HS256"),
    )


class SSEListener:
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
        self._turn = 0
        self._active_turn = -1

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
                    print(f"SSE connection failed: HTTP {r.status_code}")
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
                        if etype == "task_artifact":
                            tid = event.get("task_id", "")
                            if tid == "streaming-task" or tid == self.current_task_id:
                                if self._active_turn != active_turn:
                                    active_turn = self._active_turn
                                    local_text = ""
                                artifact = event.get("artifact", {})
                                if isinstance(artifact, dict) and artifact.get("text"):
                                    text = artifact["text"]
                                    if local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass
                                    else:
                                        print(text, end="", flush=True)
                                        local_text += text
                                        self.full_text = local_text
                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.done.set()
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                print(f"\nError: {err}", flush=True)
                                self.done.set()
            except requests.exceptions.ConnectionError:
                pass
            except Exception as e:
                if not self.stop:
                    print(f"SSE error: {e}")

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        time.sleep(1)

    def set_task(self, task_id):
        self.current_task_id = task_id
        self.full_text = ""
        self.done.clear()
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


def send_message(gateway_url, token, endpoint_id, part_id, text, task_id):
    metadata = {
        "operation": "task_execution",
        "agent_uuid": HERMES_AGENT_ID,
        "stream": True,
        "task_data": {"task_id": task_id, "task_type": "hermes_hello"},
    }
    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {"role": "ROLE_USER", "parts": [{"text": text}]},
            "metadata": metadata,
        },
        "id": f"hello-{task_id}",
    }
    return requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Part-Id": part_id,
        },
        timeout=240,
    )


def extract_text_from_response(body):
    result = body.get("result", {})
    if isinstance(result, dict):
        parts = result.get("parts", [])
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in parts
        )
    return ""


def main():
    env = load_env()
    gateway_url = f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    hermes_url = env.get("HERMES_API_URL", "http://127.0.0.1:8642")
    endpoint_id = env.get("endpoint_id", "gpt")
    part_id = env.get("part_id", "nestaging")
    token = gen_token(env)

    # Health checks
    try:
        hr = requests.get(f"{hermes_url}/health", timeout=5)
        if hr.status_code != 200:
            print(f"Hermes API Server not healthy: HTTP {hr.status_code}")
            return 1
    except Exception as e:
        print(f"Cannot reach Hermes at {hermes_url}: {e}")
        return 1

    try:
        gr = requests.get(f"{gateway_url}/health", timeout=5)
        if gr.status_code != 200:
            print(f"Gateway not healthy: HTTP {gr.status_code}")
            return 1
    except Exception as e:
        print(f"Cannot reach gateway at {gateway_url}: {e}")
        return 1

    sse = SSEListener(gateway_url, token, endpoint_id, part_id)
    sse.start()

    task_id = f"hello-{uuid.uuid4().hex[:8]}"
    sse.set_task(task_id)

    print("Agent> ", end="", flush=True)
    r = send_message(gateway_url, token, endpoint_id, part_id, PROMPT, task_id)

    sse.done.wait(timeout=200)
    streamed_text = sse.full_text

    if not streamed_text:
        try:
            body = r.json()
            streamed_text = extract_text_from_response(body)
        except Exception:
            streamed_text = ""

    print()
    sse.stop_listening()

    if streamed_text:
        if not sse.full_text:
            print(streamed_text)
        print(f"({len(streamed_text)} chars)")
        return 0
    else:
        try:
            body = r.json()
            if body.get("error"):
                print(f"Error: {body['error'].get('message', '')}")
            else:
                print("No response received")
        except Exception:
            print(f"HTTP {r.status_code}: {r.text[:200]}")
        return 2


if __name__ == "__main__":
    sys.exit(main())