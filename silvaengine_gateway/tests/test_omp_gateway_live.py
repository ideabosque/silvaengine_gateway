#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live E2E test for the omp (oh-my-pi) Agent ↔ A2A Protocol Plugin integration
through the SilvaEngine Gateway.

Full request path:

    Client
      → SilvaEngine Gateway (host port: $CONTAINER_PORT, in-container $GATEWAY_PORT)
      → POST /{endpoint_id}/a2a  (dispatch_a2a_jsonrpc)
      → a2a_protocol_plugin executor
      → Phase 10 bridge → OmpAgentHandler
      → omp sidecar (HTTP + SSE)
      → JSON-RPC response back through gateway

Test sequence (mirrors test_hermes_gateway_live.py):

    1. Probe the omp sidecar health
    2. Probe gateway health and A2A route availability
    3. Register or verify the a2a-omp-agent fixture via gateway GraphQL
    4. Non-streaming message/send through gateway
    5. Echoed contextId resumes the same conversation
    6. Cancel a running task (tasks/cancel → canceled)
    7. SSE read from GET /{endpoint_id}/a2a_sse
    8. Failure case — unknown agent
    9. Approval flow (omp_permission_mode=ask): task_permission on the SSE
       stream → approval_response → the approved run completes

The agent's backend type ("omp") is resolved inside a2a_protocol_plugin from
the a2a_agents row's metadata JSON (AGENT_TYPE_MAP). The gateway routes A2A
traffic by endpoint/partition only — it has no per-agent-type branch — so this
test exercises the plugin's omp handler through the standard A2A routes.

Prerequisites:
    - omp sidecar running (host http://127.0.0.1:$OMP_PORT — the published
      host port; the container listens on 3310 and answers the gateway at
      http://omp:3310)
    - SilvaEngine Gateway running on http://127.0.0.1:$CONTAINER_PORT
      (the published host port; GATEWAY_PORT is the in-container port)
    - Gateway .env at silvaengine_gateway/silvaengine_gateway/tests/.env
      with ADMIN_STATIC_TOKEN, endpoint_id, part_id, db_backend=postgresql
    - PostgreSQL container running (silvaengine-postgres)
    - omp env vars in the gateway .env:
      OMP_API_URL, OMP_SECRET_KEY, OMP_CWD

Usage:
    # Set env var to enable live tests (pytest mode)
    A2A_RUN_LIVE_OMP_TESTS=1 python -m pytest \
        silvaengine_gateway/tests/test_omp_gateway_live.py -v

    # Or run directly (auto-reads gateway .env, generates token)
    python silvaengine_gateway/tests/test_omp_gateway_live.py

    # With explicit args
    python silvaengine_gateway/tests/test_omp_gateway_live.py \
        --gateway-url http://127.0.0.1:8775 \
        --omp-url http://127.0.0.1:3320 \
        --omp-api-url http://omp:3310 \
        --endpoint-id a2a \
        --part-id default

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

import pytest
import requests

__author__ = "bibow"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GATEWAY_TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_ENV_FILE = GATEWAY_TESTS_DIR / ".env"

#: Fallback gateway address (a local `python -m silvaengine_gateway` run).
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
#: Where the sidecar answers from the HOST (published port, docker .env
#: OMP_PORT). The container always listens on 3310.
DEFAULT_OMP_PROBE_URL = "http://127.0.0.1:3320"
#: Where the sidecar answers from the GATEWAY process (docker network DNS).
#: This is the value that goes into the agent metadata — it must be
#: reachable from the gateway, not from the test host.
DEFAULT_OMP_API_URL = "http://omp:3310"
OMP_AGENT_ID = "a2a-omp-agent"

#: The partition the bundled agents are seeded in (docker-a2a-gateway
#: scripts/seed_agents.py: SEED_ENDPOINT_ID / SEED_PART_ID defaults).
DEFAULT_ENDPOINT_ID = "a2a"
DEFAULT_PART_ID = "default"

pytestmark = pytest.mark.skipif(
    os.getenv("A2A_RUN_LIVE_OMP_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="Live omp gateway E2E tests require A2A_RUN_LIVE_OMP_TESTS=1 "
    "plus a running omp sidecar and SilvaEngine Gateway.",
)


# ---------------------------------------------------------------------------
# .env loader (mirrors test_hermes_gateway_live.py)
# ---------------------------------------------------------------------------

def load_gateway_env():
    """Load the gateway's tests/.env so we get ADMIN_STATIC_TOKEN, endpoint_id, etc."""
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
            # Strip inline comments
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
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _pass(name, detail=""):
    print(f"  {_C.GREEN}[PASS]{_C.RESET} {name}")
    if detail:
        print(f"       {detail}")


def _fail(name, detail=""):
    print(f"  {_C.RED}[FAIL]{_C.RESET} {name}")
    if detail:
        print(f"       {detail}")


def _info(msg):
    print(f"  {_C.CYAN}[INFO]{_C.RESET} {msg}")


def _section(title):
    print(f"\n{_C.BOLD}{'=' * 80}{_C.RESET}")
    print(f"{_C.CYAN}{title}{_C.RESET}")
    print(f"{_C.BOLD}{'=' * 80}{_C.RESET}\n")


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token(env: dict, gateway_url: str) -> str:
    """Get a valid auth token for the gateway.

    If ADMIN_STATIC_TOKEN is set, use it.  If the gateway uses local auth,
    generate a local JWT with the same secret.  If the gateway uses Cognito,
    try POST /auth/token with admin credentials.
    """
    static = env.get("ADMIN_STATIC_TOKEN", "")
    auth_provider = env.get("GATEWAY_AUTH_PROVIDER", "local")

    if static and auth_provider != "cognito":
        return static

    if auth_provider == "cognito":
        username = env.get("ADMIN_USERNAME", "admin")
        password = env.get("ADMIN_PASSWORD", "")
        if password:
            try:
                r = requests.post(
                    f"{gateway_url}/auth/token",
                    data=f"username={username}&password={password}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15,
                )
                if r.status_code == 200:
                    return r.json().get("access_token", "")
            except Exception:
                pass

    jwt_secret = env.get("JWT_SECRET_KEY", "CHANGEME")
    jwt_algo = env.get("JWT_ALGORITHM", "HS256")

    try:
        from jose import jwt
        import pendulum

        payload = {
            "sub": "e2e-test",
            "username": "e2e-test",
            "role": "admin",
            "iat": pendulum.now("UTC"),
            "perm": True,
        }
        return jwt.encode(payload, jwt_secret, algorithm=jwt_algo)
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
        return create_local_jwt({"username": "e2e-test", "role": "admin"}, forever=True)


# ---------------------------------------------------------------------------
# pytest fixture — provides ctx to test_* functions when run via pytest
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ctx():
    """Build a test context from gateway .env for pytest mode."""
    env = load_gateway_env()
    # The gateway's host address (CONTAINER_PORT is published on the host;
    # GATEWAY_PORT is the in-container port) — see resolve_gateway_url.
    gateway_url = resolve_gateway_url(env)
    endpoint_id = env.get("endpoint_id", DEFAULT_ENDPOINT_ID)
    part_id = env.get("part_id", DEFAULT_PART_ID)
    token = (os.getenv("ADMIN_STATIC_TOKEN") or env.get("ADMIN_STATIC_TOKEN")
             or generate_token(env, gateway_url)).strip()
    return {
        "gateway_url": gateway_url,
        # Host-reachable sidecar (direct probe).
        "omp_url": resolve_omp_probe_url(env),
        # Gateway-reachable sidecar (goes into the agent metadata).
        "omp_api_url": env.get("OMP_API_URL", DEFAULT_OMP_API_URL),
        "omp_secret_key": env.get("OMP_SECRET_KEY", ""),
        "endpoint_id": endpoint_id,
        "part_id": part_id,
        "token": token,
        "env": env,
        "headers": {
            "Authorization": f"Bearer {token}",
            "Part-Id": part_id,
        },
    }


def resolve_gateway_url(env: dict) -> str:
    """The gateway's address as seen from THIS host.

    The docker stack publishes CONTAINER_PORT on the host, while
    GATEWAY_PORT is the port INSIDE the container (docker-a2a-gateway/.env:
    CONTAINER_PORT=8775, GATEWAY_PORT=8765). GATEWAY_URL overrides both.
    """
    explicit = os.getenv("GATEWAY_URL")
    if explicit:
        return explicit.rstrip("/")
    port = env.get("CONTAINER_PORT") or env.get("GATEWAY_PORT")
    if port:
        return f"http://127.0.0.1:{port}"
    return DEFAULT_GATEWAY_URL


def resolve_omp_probe_url(env: dict) -> str:
    """The sidecar's address as seen from THIS host (published host port)."""
    explicit = env.get("OMP_PROBE_URL")
    if explicit:
        return explicit.rstrip("/")
    port = env.get("OMP_PORT")
    if port:
        return f"http://127.0.0.1:{port}"
    return DEFAULT_OMP_PROBE_URL


# ---------------------------------------------------------------------------
# omp sidecar probe helpers
# ---------------------------------------------------------------------------

def _omp_get(omp_url, secret_key, path):
    """GET a sidecar path. The sidecar expects the shared secret as X-Secret-Key."""
    try:
        headers = {}
        if secret_key:
            headers["X-Secret-Key"] = secret_key
        return requests.get(f"{omp_url}{path}", headers=headers, timeout=15)
    except Exception:
        return None


def omp_health_ok(omp_url, secret_key):
    """The sidecar answers /health (401 when the shared secret does not match)."""
    r = _omp_get(omp_url, secret_key, "/health")
    if r is None:
        return False
    return r.status_code in (200, 401, 403)


# ---------------------------------------------------------------------------
# Gateway probe helpers
# ---------------------------------------------------------------------------

def gateway_health_ok(gateway_url, token):
    try:
        r = requests.get(
            f"{gateway_url}/health",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


def gateway_graphql_ping(gateway_url, token, endpoint_id, part_id):
    try:
        r = requests.post(
            f"{gateway_url}/{endpoint_id}/a2a_core_graphql",
            json={"query": "query { ping }"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Part-Id": part_id,
            },
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Agent registration via gateway GraphQL
# ---------------------------------------------------------------------------

def register_omp_agent(gateway_url, token, endpoint_id, part_id, env, omp_api_url,
                       permission_mode=None):
    """Register the a2a-omp-agent fixture via the gateway's A2A GraphQL.

    The metadata carries the omp connection details but NOT the sidecar
    secret: the gateway's own env (OMP_SECRET_KEY) wins at resolve time, so
    the secret never lives in tenant-writable agent metadata.
    """
    mutation = """
        mutation RegisterOmpAgent(
            $endpointId: String!
            $partId: String!
            $agentId: String
            $agentName: String!
            $endpointUrl: String!
            $metadata: JSON
            $updatedBy: String!
        ) {
            insertUpdateA2aAgent(
                endpointId: $endpointId
                partId: $partId
                agentId: $agentId
                agentName: $agentName
                endpointUrl: $endpointUrl
                metadata: $metadata
                updatedBy: $updatedBy
            ) {
                a2aAgent { agentId agentName }
            }
        }
    """
    metadata = {
        "agent_type": "omp",
        "omp_api_url": omp_api_url,
        "omp_cwd": env.get("OMP_CWD", "/workspace"),
        "omp_permission_mode": permission_mode
        or env.get("OMP_PERMISSION_MODE", "auto_allow"),
        "omp_busy_behavior": env.get("OMP_BUSY_BEHAVIOR", "followUp"),
        "omp_timeout": 300.0,
    }
    variables = {
        "endpointId": endpoint_id,
        "partId": part_id,
        "agentId": OMP_AGENT_ID,
        "agentName": "omp Agent",
        "endpointUrl": gateway_url,
        "metadata": metadata,
        "updatedBy": "e2e-test",
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
# JSON-RPC request helper (through gateway)
# ---------------------------------------------------------------------------

def send_a2a(gateway_url, token, endpoint_id, part_id, method, params, request_id="1", timeout=180):
    """Send a JSON-RPC 2.0 request through the gateway's /{endpoint_id}/a2a route."""
    body = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": request_id,
    }
    return requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Part-Id": part_id,
        },
        timeout=timeout,
    )


def extract_text(result):
    """Extract text from an A2A JSON-RPC result (parts or artifacts)."""
    if not isinstance(result, dict):
        return ""
    parts = result.get("parts", [])
    if parts:
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in parts
        )
    artifacts = result.get("artifacts", [])
    if artifacts:
        texts = []
        for a in artifacts:
            if isinstance(a, dict):
                for p in a.get("parts", []):
                    texts.append(p.get("text", "") if isinstance(p, dict) else str(p))
        return "".join(texts)
    return result.get("text", "")


def extract_state(result):
    """The task state, normalized (the SDK may answer "canceled" or the
    protobuf enum name "task_state_canceled")."""
    if not isinstance(result, dict):
        return ""
    status = result.get("status", {})
    if isinstance(status, dict):
        state = status.get("state", "")
    elif isinstance(status, str):
        state = status
    else:
        return ""
    state = str(state).lower()
    for prefix in ("task_state_", "t_"):
        if state.startswith(prefix):
            state = state[len(prefix):]
            break
    return state


def extract_context_id(result):
    """The conversation id the client echoes back on the next message/send."""
    if not isinstance(result, dict):
        return ""
    for key in ("contextId", "context_id", "sessionId"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def read_sse_connected_frame(gateway_url, endpoint_id, headers, timeout=30):
    """Read the first SSE frame from GET /{endpoint_id}/a2a_sse.

    The gateway opens the stream with an "event: connected" frame
    (router_builder._make_sse_handler) followed by heartbeats.
    """
    try:
        with requests.get(
            f"{gateway_url}/{endpoint_id}/a2a_sse",
            headers=headers,
            stream=True,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                return None
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.strip()
                if line.startswith("event:"):
                    return line.split(":", 1)[1].strip()
                if line.startswith("data:"):
                    return "data"
    except Exception:
        return None
    return None


def collect_sse_events(gateway_url, endpoint_id, headers, sink, stop, timeout=300):
    """Stream GET /{endpoint_id}/a2a_sse into ``sink`` (a list of dicts).

    The A2A event stream is partition-scoped, so the approval request, the
    token chunks and the task status transitions all arrive here.
    """
    try:
        with requests.get(
            f"{gateway_url}/{endpoint_id}/a2a_sse",
            headers=headers,
            stream=True,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                return
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        sink.append(json.loads(payload))
                    except ValueError:
                        continue
                if stop.is_set():
                    break
    except Exception:
        return


# ---------------------------------------------------------------------------
# Test 1: omp sidecar health
# ---------------------------------------------------------------------------

def test_01_omp_health(ctx):
    _section("TEST 1: omp Sidecar Health")
    assert omp_health_ok(ctx["omp_url"], ctx["omp_secret_key"]), \
        f"omp sidecar not reachable at {ctx['omp_url']}"
    _pass("omp sidecar /health", ctx["omp_url"])


# ---------------------------------------------------------------------------
# Test 2: Gateway health and A2A route
# ---------------------------------------------------------------------------

def test_02_gateway_health(ctx):
    _section("TEST 2: SilvaEngine Gateway Health & A2A Route")
    assert gateway_health_ok(ctx["gateway_url"], ctx["token"]), \
        f"Gateway /health failed at {ctx['gateway_url']}"
    _pass("Gateway /health", ctx["gateway_url"])

    ok = gateway_graphql_ping(
        ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"]
    )
    assert ok, f"POST /{ctx['endpoint_id']}/a2a_core_graphql ping failed"
    _pass("A2A GraphQL ping", f"POST /{ctx['endpoint_id']}/a2a_core_graphql")


# ---------------------------------------------------------------------------
# Test 3: Register a2a-omp-agent fixture
# ---------------------------------------------------------------------------

def test_03_register_agent(ctx):
    _section("TEST 3: Register/Verify omp Agent Fixture")
    result = register_omp_agent(
        ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
        ctx["env"], ctx["omp_api_url"],
    )
    if result:
        _pass("insertUpdateA2aAgent", f"agent_id={result.get('a2aAgent', {}).get('agent_id')}")
    else:
        _info("Agent registration via GraphQL not available — proceeding (agent may already exist)")


# ---------------------------------------------------------------------------
# Test 4: Non-streaming message/send
# ---------------------------------------------------------------------------

def test_04_non_streaming(ctx):
    _section("TEST 4: Non-Streaming message/send via Gateway")
    params = {
        "message": {
            "role": "user",
            "parts": [{"text": "Say hello in one word."}],
        },
        "metadata": {
            "operation": "message_response",
            "agent_uuid": OMP_AGENT_ID,
        },
    }
    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", params, "omp-e2e-send-001")
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "error" not in body or body.get("error") is None, \
        f"JSON-RPC error: {body.get('error')}"
    text = extract_text(body.get("result", {}))
    _pass("message/send", f"Response: {text[:120]}")
    assert len(text) > 0, "Response text is empty"
    _pass("Non-empty response", f"{len(text)} chars")


# ---------------------------------------------------------------------------
# Test 5: Echoed contextId resumes the same conversation
# ---------------------------------------------------------------------------

def test_05_context_id_resume(ctx):
    _section("TEST 5: Echoed contextId Resumes the Same Conversation")
    first_params = {
        "message": {
            "role": "user",
            "parts": [{"text": "Remember the token word: BANANA42."}],
        },
        "metadata": {
            "operation": "message_response",
            "agent_uuid": OMP_AGENT_ID,
        },
    }
    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", first_params, "omp-e2e-ctx-001")
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    first = r.json()
    assert "error" not in first or first.get("error") is None, \
        f"JSON-RPC error: {first.get('error')}"
    context_id = extract_context_id(first.get("result", {}))
    assert context_id, f"No contextId in reply: {json.dumps(first.get('result', {}))[:300]}"
    _pass("First turn contextId", context_id)

    second_params = {
        "message": {
            "role": "user",
            "parts": [{"text": "What was the token word?"}],
            "contextId": context_id,
        },
        "metadata": {
            "operation": "message_response",
            "agent_uuid": OMP_AGENT_ID,
        },
    }
    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", second_params, "omp-e2e-ctx-002")
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    second = r.json()
    assert "error" not in second or second.get("error") is None, \
        f"JSON-RPC error: {second.get('error')}"
    text = extract_text(second.get("result", {}))
    _pass("Second turn (echoed contextId)", f"Response: {text[:120]}")
    assert len(text) > 0, "Resumed response text is empty"


# ---------------------------------------------------------------------------
# Test 6: Cancel a running task
# ---------------------------------------------------------------------------

def test_06_cancel(ctx):
    _section("TEST 6: CancelTask via Gateway")
    task_id = f"omp-cancel-{uuid.uuid4().hex[:8]}"
    # A prompt that keeps the run in flight long enough to cancel it: the
    # bash tool runs `sleep 25`. The agent's omp_permission_mode is
    # auto_allow, so the bridge auto-approves the tool call and the task
    # stays WORKING until the sleep ends.
    params = {
        "message": {
            "role": "user",
            "parts": [{"text":
                       "Use the bash tool to run `sleep 25` and report its output."}],
        },
        "metadata": {
            "operation": "task_execution",
            "agent_uuid": OMP_AGENT_ID,
            "task_data": {"task_id": task_id, "task_type": "omp_e2e_cancel"},
        },
    }

    # The send blocks until the run ends — run it in the background so the
    # cancel lands WHILE the task is still in flight (a cancel after the
    # run finished is rejected with -32002 "Task cannot be canceled").
    send_result = {"response": None, "error": None}

    def _bg():
        try:
            send_result["response"] = send_a2a(
                ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", params, "omp-e2e-cancel-start", timeout=180,
            )
        except Exception as e:
            send_result["error"] = str(e)

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    # Wait for the task to show up as a non-terminal task.
    state = ""
    for _ in range(60):
        r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                     "tasks/get", {"id": task_id}, "omp-e2e-cancel-poll", timeout=30)
        body = r.json()
        if not body.get("error"):
            state = extract_state(body.get("result", {}))
            if state and state not in ("completed", "failed", "canceled", "cancelled"):
                break
        time.sleep(0.5)
    assert state, f"Task {task_id} never showed up in tasks/get"
    assert state not in ("completed", "failed", "canceled", "cancelled"), \
        f"Task {task_id} finished before it could be canceled (state={state})"

    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                 "tasks/cancel", {"id": task_id}, "omp-e2e-cancel-001", timeout=30)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert not body.get("error"), f"JSON-RPC error: {body.get('error')}"
    canceled_state = extract_state(body.get("result", {}))
    _pass("CancelTask", f"task_id={task_id}, state={canceled_state}")
    assert canceled_state in ("canceled", "cancelled"), \
        f"Expected canceled state, got {canceled_state!r}"
    t.join(timeout=30)


# ---------------------------------------------------------------------------
# Test 7: SSE read from GET /{endpoint_id}/a2a_sse
# ---------------------------------------------------------------------------

def test_07_sse(ctx):
    _section("TEST 7: SSE Stream (GET /{endpoint_id}/a2a_sse)")
    event = read_sse_connected_frame(
        ctx["gateway_url"], ctx["endpoint_id"], ctx["headers"], timeout=30
    )
    assert event, "No SSE frame received from the A2A stream"
    _pass("SSE connected frame", f"event={event}")


# ---------------------------------------------------------------------------
# Test 8: Failure case — unknown agent
# ---------------------------------------------------------------------------

def test_08_failure(ctx):
    _section("TEST 8: Failure Case — Unknown Agent")
    params = {
        "message": {
            "role": "user",
            "parts": [{"text": "This should fail — agent does not exist."}],
        },
        "metadata": {
            "operation": "message_response",
            "agent_uuid": "nonexistent-agent-xyz",
        },
    }
    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", params, "omp-e2e-fail-001", timeout=60)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    text = extract_text(body.get("result", {}))
    error = body.get("error")
    if error:
        _pass("Failure: unknown agent (JSON-RPC error)", f"error: {str(error)[:120]}")
    elif text and ("not found" in text.lower() or "error" in text.lower()):
        _pass("Failure: unknown agent (error text)", f"text: {text[:120]}")
    else:
        _info(f"Unknown agent returned: {text[:120] if text else 'empty'}")


# ---------------------------------------------------------------------------
# Test 9: Approval flow (human-in-the-loop) — permission_mode "ask"
# ---------------------------------------------------------------------------

def test_09_approval(ctx):
    """omp's native tool approval, bridged to A2A INPUT_REQUIRED.

    In "ask" mode the handler turns omp's ``extension_ui_request`` select
    dialog into an "approval" chunk; the bridge answers it only after the
    client sends ``operation=approval_response`` for the same task. The
    deployment default is auto_allow (the bridge answers omp itself), so
    this test re-registers the agent in ask mode and restores it after.
    """
    _section("TEST 9: Approval Flow (permission_mode=ask)")
    register_omp_agent(
        ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
        ctx["env"], ctx["omp_api_url"], permission_mode="ask",
    )

    events = []
    stop = threading.Event()
    reader = threading.Thread(
        target=collect_sse_events,
        args=(ctx["gateway_url"], ctx["endpoint_id"], ctx["headers"], events, stop),
        daemon=True,
    )
    reader.start()
    time.sleep(2)

    task_id = f"omp-approval-{uuid.uuid4().hex[:8]}"
    params = {
        "message": {
            "role": "user",
            "parts": [{"text":
                       "Use the bash tool to run `echo approval-probe` and "
                       "report its output."}],
        },
        "metadata": {
            "operation": "task_execution",
            "agent_uuid": OMP_AGENT_ID,
            "stream": True,
            "task_data": {"task_id": task_id, "task_type": "omp_e2e_approval"},
        },
    }

    answer = {}

    def _bg():
        try:
            answer["body"] = send_a2a(
                ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                "message/send", params, "omp-e2e-approval-ask", timeout=180,
            ).json()
        except Exception as e:
            answer["error"] = str(e)

    asker = threading.Thread(target=_bg, daemon=True)
    asker.start()

    # The approval request arrives on the SSE stream, not in the JSON-RPC
    # response (the bridge emits INPUT_REQUIRED to the SSE channel only).
    permission = None
    deadline = time.time() + 120
    while time.time() < deadline and permission is None:
        for event in list(events):
            if event.get("type") == "task_permission":
                permission = event
                break
        time.sleep(0.5)
    assert permission, (
        "No task_permission event on the A2A SSE stream within 120s — the "
        "model may not have called a tool, or omp's --approval-mode is not "
        "always-ask"
    )
    details = permission.get("details", "")
    _pass("task_permission received", details[:160])
    assert "approval-probe" in details, f"Unexpected approval details: {details[:200]}"

    # Answer the approval.
    approval = {
        "message": {"role": "user", "parts": [{"text": "approved"}]},
        "metadata": {
            "operation": "approval_response",
            "approval_task_id": task_id,
            "approved": True,
            "reason": "e2e",
        },
    }
    r = send_a2a(ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
                 "message/send", approval, "omp-e2e-approval-answer", timeout=60)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert not body.get("error"), f"JSON-RPC error: {body.get('error')}"
    granted = extract_text(body.get("result", {}))
    _pass("approval_response", granted[:120])
    assert "Approval granted" in granted, f"Unexpected answer: {granted!r}"

    # The approved run continues and completes.
    asker.join(timeout=180)
    stop.set()
    final = answer.get("body", {})
    text = extract_text(final.get("result", {}))
    _pass("final answer after approval", text[:160])
    assert len(text) > 0, "No answer after the approval"

    # Restore the deployment default.
    register_omp_agent(
        ctx["gateway_url"], ctx["token"], ctx["endpoint_id"], ctx["part_id"],
        ctx["env"], ctx["omp_api_url"],
    )


# ---------------------------------------------------------------------------
# Context builder — reads gateway .env, generates token
# ---------------------------------------------------------------------------

def build_context(args):
    """Build a test context dict from CLI args + gateway .env."""
    env = load_gateway_env()

    gateway_url = args.gateway_url or resolve_gateway_url(env)
    # Host-reachable sidecar (direct probe).
    omp_url = args.omp_url or resolve_omp_probe_url(env)
    # Gateway-reachable sidecar (goes into the agent metadata).
    omp_api_url = args.omp_api_url or env.get("OMP_API_URL", DEFAULT_OMP_API_URL)
    endpoint_id = args.endpoint_id or env.get("endpoint_id", DEFAULT_ENDPOINT_ID)
    part_id = args.part_id or env.get("part_id", DEFAULT_PART_ID)
    token = (args.token or os.getenv("ADMIN_STATIC_TOKEN")
             or env.get("ADMIN_STATIC_TOKEN")
             or generate_token(env, gateway_url)).strip()

    return {
        "gateway_url": gateway_url,
        "omp_url": omp_url,
        "omp_api_url": omp_api_url,
        "omp_secret_key": env.get("OMP_SECRET_KEY", ""),
        "endpoint_id": endpoint_id,
        "part_id": part_id,
        "token": token,
        "env": env,
        "headers": {
            "Authorization": f"Bearer {token}",
            "Part-Id": part_id,
        },
    }


# ---------------------------------------------------------------------------
# Direct-run main()
# ---------------------------------------------------------------------------

def run_all(ctx):
    results = []
    tests = [
        ("omp Sidecar Health", lambda: test_01_omp_health(ctx)),
        ("Gateway Health & A2A", lambda: test_02_gateway_health(ctx)),
        ("Register omp Agent", lambda: test_03_register_agent(ctx)),
        ("Non-Streaming message/send", lambda: test_04_non_streaming(ctx)),
        ("Echoed contextId Resume", lambda: test_05_context_id_resume(ctx)),
        ("CancelTask", lambda: test_06_cancel(ctx)),
        ("A2A SSE stream", lambda: test_07_sse(ctx)),
        ("Failure: Unknown Agent", lambda: test_08_failure(ctx)),
        ("Approval Flow (ask)", lambda: test_09_approval(ctx)),
    ]

    for name, fn in tests:
        try:
            fn()
            results.append((name, True))
        except AssertionError as e:
            _fail(name, str(e))
            results.append((name, False))
        except Exception as e:
            _fail(name, f"Exception: {e}")
            results.append((name, False))
        time.sleep(3)

    _section("E2E TEST SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        status = f"{_C.GREEN}PASS{_C.RESET}" if ok else f"{_C.RED}FAIL{_C.RESET}"
        print(f"  [{status}] {name}")
    print(f"\n  Total: {len(results)}  Passed: {passed}  Failed: {len(results) - passed}")
    print(f"{_C.BOLD}{'=' * 80}{_C.RESET}\n")
    return passed == len(results)


def main():
    parser = argparse.ArgumentParser(
        description="Live E2E test: omp (oh-my-pi) Agent ↔ A2A Plugin via SilvaEngine Gateway"
    )
    parser.add_argument("--gateway-url", default=None,
                        help="Gateway base URL (default: from gateway .env)")
    parser.add_argument("--omp-url", default=None,
                        help="omp sidecar base URL reachable from THIS host "
                             "(default: http://127.0.0.1:$OMP_PORT)")
    parser.add_argument("--omp-api-url", default=None,
                        help="omp sidecar base URL reachable from the GATEWAY "
                             "(default: $OMP_API_URL)")
    parser.add_argument("--token", default=None,
                        help="JWT token (default: generated from gateway .env)")
    parser.add_argument("--endpoint-id", default=None,
                        help="Endpoint ID (default: from gateway .env)")
    parser.add_argument("--part-id", default=None,
                        help="Partition ID (default: from gateway .env)")
    args = parser.parse_args()

    ctx = build_context(args)
    _info(f"Gateway:  {ctx['gateway_url']}")
    _info(f"omp:      {ctx['omp_url']} (probe) / {ctx['omp_api_url']} (gateway)")
    _info(f"Endpoint: {ctx['endpoint_id']} / {ctx['part_id']}")
    _info(f"Token:    {ctx['token'][:20]}...")

    ok = run_all(ctx)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
