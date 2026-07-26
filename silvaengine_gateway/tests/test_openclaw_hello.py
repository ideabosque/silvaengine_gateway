#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal E2E: say hello from OpenClaw through A2A via the SilvaEngine Gateway.

    Client -> Gateway (8765) -> A2A daemon -> Phase 10 bridge -> OpenClaw Gateway (18789) -> reply

Reads gateway tests/.env, generates a local JWT, sends message/send with a
short prompt, and prints the agent's reply text.

Prerequisites:
    - OpenClaw Gateway running on http://127.0.0.1:18789
      (gateway.http.endpoints.chatCompletions.enabled = true)
    - SilvaEngine Gateway running on http://127.0.0.1:8765
    - Gateway .env at silvaengine_gateway/silvaengine_gateway/tests/.env
    - PostgreSQL running with openclaw-agent registered

Author: bibow
"""
from __future__ import print_function

import json
import os
import sys
import uuid
from pathlib import Path

import requests

__author__ = "bibow"

GATEWAY_TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_ENV_FILE = GATEWAY_TESTS_DIR / ".env"
OPENCLAW_AGENT_ID = "openclaw-agent"


def load_env():
    env = {}
    if not GATEWAY_ENV_FILE.exists():
        return env
    for line in GATEWAY_ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        if k:
            env[k] = v
    return env


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
            {"sub": "openclaw-hello", "username": "openclaw-hello", "role": "admin",
             "iat": pendulum.now("UTC"), "perm": True},
            jwt_secret, algorithm=jwt_algo,
        )
    except ImportError:
        sys.path.insert(0, str(GATEWAY_TESTS_DIR.parent.parent))
        from silvaengine_gateway.auth.jwt_local import create_local_jwt
        from silvaengine_gateway.config import GatewayConfig
        import logging
        GatewayConfig.initialize(logging.getLogger("openclaw-hello"), {
            "jwt_secret_key": jwt_secret,
            "jwt_algorithm": jwt_algo,
            "access_token_exp": 15,
            "admin_static_token": "",
        })
        return create_local_jwt({"username": "openclaw-hello", "role": "admin"}, forever=True)


def main():
    env = load_env()
    gateway_url = f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8765')}"
    endpoint_id = env.get("endpoint_id", "gpt")
    part_id = env.get("part_id", "nestaging")
    token = generate_token(env)

    print(f"[INFO] gateway={gateway_url} endpoint_id={endpoint_id} part_id={part_id}")

    # Health probes
    try:
        r = requests.get(f"{gateway_url}/health",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        print(f"[INFO] gateway /health -> {r.status_code}")
    except Exception as e:
        print(f"[FAIL] gateway /health error: {e}")
        return 1

    openclaw_url = env.get("OPENCLAW_API_URL", "http://127.0.0.1:18789")
    openclaw_key = env.get("OPENCLAW_API_KEY", "")
    try:
        r = requests.get(f"{openclaw_url}/v1/models",
                         headers={"Authorization": f"Bearer {openclaw_key}"}, timeout=15)
        print(f"[INFO] openclaw /v1/models -> {r.status_code}")
    except Exception as e:
        print(f"[WARN] openclaw /v1/models error: {e}")

    # message/send through gateway
    task_id = f"openclaw-hello-{uuid.uuid4().hex[:8]}"
    prompt = "Say hello and introduce yourself in one short sentence."
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
                "agent_uuid": OPENCLAW_AGENT_ID,
                "task_data": {"task_id": task_id, "task_type": "openclaw_hello"},
            },
        },
        "id": "openclaw-hello-001",
    }

    print(f"[INFO] sending message/send (task_id={task_id})")
    print(f"[INFO] prompt: {prompt}")

    r = requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Part-Id": part_id,
        },
        timeout=300,
    )
    print(f"[INFO] message/send response: HTTP {r.status_code}")

    # Extract reply text
    reply_text = ""
    try:
        resp = r.json()
    except Exception:
        print(f"[FAIL] non-JSON response: {r.text[:300]}")
        return 2

    # Gateway may wrap: {statusCode, headers, body: "<json str>"} or plain JSON-RPC
    if "body" in resp and isinstance(resp["body"], str):
        try:
            resp = json.loads(resp["body"])
        except Exception:
            pass

    result = resp.get("result") if isinstance(resp, dict) else None
    error = resp.get("error") if isinstance(resp, dict) else None

    if error:
        print(f"[FAIL] JSON-RPC error: {json.dumps(error)[:300]}")
        return 3

    if isinstance(result, dict):
        parts = result.get("parts", []) or result.get("artifacts", [])
        for p in parts:
            if isinstance(p, dict):
                reply_text += p.get("text", "") or ""
            elif isinstance(p, str):
                reply_text += p
        if not reply_text:
            reply_text = result.get("text", "") or result.get("content", "")

    if not reply_text:
        print(f"[INFO] raw response: {json.dumps(resp)[:500]}")
        print("[FAIL] no reply text extracted")
        return 4

    print(f"[PASS] OpenClaw reply: {reply_text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())