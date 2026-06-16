# WebSocket Development Plan - SilvaEngine Gateway

> Plan status: proposed. The current gateway supports HTTP GraphQL, REST-style, background, and task-status routes. Native WebSocket routing is not implemented yet.

## 1. Problem Statement

`ai_agent_core_engine` currently streams real-time responses through AWS API Gateway Management API (`apigatewaymanagementapi`). That keeps local and non-Lambda deployments tied to AWS WebSocket infrastructure:

- `at_agent_listener.py:send_data_to_stream` posts data through `Config.get_api_gateway_client().post_to_connection(ConnectionId=..., Data=...)`.
- `ai_agent_handler.py:send_data_to_stream` builds the streaming envelope (`message_group_id`, `index`, `chunk_delta`, `is_message_end`) and routes it through the invoker path before it reaches `post_to_connection()`.
- The core config initializes a boto3 API Gateway Management API client from AWS settings, API id, stage, region, and credentials.

The target is for `silvaengine_gateway` to own the WebSocket lifecycle in FastAPI while keeping the existing streaming envelope and Lambda fallback intact.

## 2. Current Gateway Baseline

Verified in this repository:

- Dynamic route registration lives in `silvaengine_gateway/router_builder.py`.
- `RouteSpec.handler_type` currently covers `graphql`, `rest`, `background`, and `task_status`.
- `build_router_from_manifest()` registers HTTP routes through `router.add_api_route(...)`.
- `app.py` loads the route manifest, initializes module config classes, installs middleware, and builds the dynamic router.
- `routes.yaml` currently declares Knowledge Graph Engine and AI RFQ Engine HTTP routes only.
- Auth is enforced for HTTP through `FlexJWTMiddleware` plus `get_current_user`; WebSocket routes need their own token verification because they do not use the same request/response middleware path after upgrade.

Unverified in this repository:

- The exact current code in `ai_agent_core_engine` and `ai_agent_handler`. The plan preserves the previously described integration points, but Phase 0 must confirm names, call signatures, and config behavior before implementation.

## 3. Target Architecture

### Current AWS Flow

```text
Client <== WebSocket ==> AWS API Gateway <== post_to_connection() == Core Engine
                         connection_id       Lambda/invoker path
```

### Target FastAPI Flow

```text
Client <== WebSocket ==> FastAPI Gateway <== ConnectionManager.send_to_connection()
                         connection_id       sync dispatch thread / core engine
```

The gateway keeps active `WebSocket` objects in-process. A sync core handler calls `ConnectionManager.send_to_connection(connection_id, data)`, and the manager schedules `websocket.send_text(...)` on the running event loop with `asyncio.run_coroutine_threadsafe()`.

This design is only valid for a single gateway worker. Multi-worker and multi-instance delivery require a shared broker, described later as a separate phase.

## 4. Phase 0: Inventory and Contract Check

Before changing code, confirm these external contracts in the checked-out versions of `ai_agent_core_engine` and `ai_agent_handler`:

| Area | Check |
|---|---|
| Stream sender | Confirm the exact `send_data_to_stream(logger, **kwargs)` signature and required keys. |
| Envelope | Confirm `message_group_id`, `data_format`, `index`, `chunk_delta`, and `is_message_end` are still emitted as described. |
| Config lifecycle | Confirm whether `Config.initialize(...)` eagerly creates the API Gateway client and whether a local mode can skip that initialization. |
| Invoker path | Confirm where `AIAgentCoreEngine` is instantiated and whether a class-level connection manager is visible before streaming begins. |
| Dispatch input | Confirm whether the WebSocket request should call `dispatch_graphql`, a dedicated action dispatcher, or an ai-agent-specific entry point. |

Phase 0 output should be a short compatibility note before implementation starts. If any names or contracts differ, update this plan and tests first.

## 5. Gateway Components

### 5.1 Connection Manager

Add `silvaengine_gateway/websocket_manager.py`.

Responsibilities:

- Track active WebSocket connections by `connection_id`.
- Store the running event loop during FastAPI lifespan startup.
- Provide a sync-safe `send_to_connection(connection_id, data) -> bool` API for core handlers running in worker threads.
- Unregister failed or closed connections.
- Expose `active_connections` and `connection_count` for tests and health reporting.

Implementation notes:

```python
class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, connection_id: str, websocket: WebSocket) -> None:
        with self._lock:
            self._connections[connection_id] = websocket

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._connections.pop(connection_id, None)

    def send_to_connection(self, connection_id: str, data: Any) -> bool:
        with self._lock:
            websocket = self._connections.get(connection_id)
            loop = self._loop

        if websocket is None or loop is None or loop.is_closed():
            return False

        payload = json.dumps(data, default=str)
        future = asyncio.run_coroutine_threadsafe(websocket.send_text(payload), loop)
        future.add_done_callback(_log_send_error)
        return True
```

Use a lock around the registry because FastAPI connection handlers and sync dispatch threads can touch it concurrently. The send path should be fire-and-forget for MVP, but it must attach a callback so send exceptions are logged instead of silently discarded.

### 5.2 WebSocket Route Support

Update `silvaengine_gateway/router_builder.py`.

Required changes:

- Extend the `RouteSpec.handler_type` comment and validation to include `websocket`.
- Allow `dispatch` to be optional for `websocket` routes only.
- Add `_make_websocket_handler(...)`.
- Update `build_router_from_manifest(..., connection_manager=None)` so WebSocket routes use `router.add_api_websocket_route(...)`.
- Do not add FastAPI `Depends(get_current_user)` to WebSocket routes; validate tokens inside the WebSocket handler.

Handler behavior:

```python
async def ws_handler(websocket: WebSocket, endpoint_id: str, part_id: str):
    claims = await verify_websocket_token(websocket)
    await websocket.accept()

    connection_id = str(uuid.uuid4())
    connection_manager.register(connection_id, websocket)

    try:
        await websocket.send_json({"type": "connection_ack", "connection_id": connection_id})
        while True:
            message = await websocket.receive_json()
            if dispatch_fn is None:
                await websocket.send_json({"type": "error", "detail": "No dispatch configured"})
                continue

            params = build_dispatch_params(message, endpoint_id, part_id, connection_id, claims)
            result = await asyncio.get_running_loop().run_in_executor(
                _executor,
                lambda: dispatch_fn(**params),
            )
            await websocket.send_json(normalize_dispatch_response(result))
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", connection_id)
    finally:
        connection_manager.unregister(connection_id)
```

The WebSocket dispatch parameter builder should mirror HTTP context injection:

- `endpoint_id`
- `part_id`
- `partition_key = f"{endpoint_id}#{part_id}"`
- `connection_id`
- `context.partition_key`
- `context.part_id`
- `context.user`

Unlike HTTP routes, the WebSocket path cannot rely on the `Part-Id` header after upgrade. For consistency with the public URL, use the path `part_id` for WebSocket `partition_key` unless Phase 0 identifies a stronger core requirement.

### 5.3 App Startup Integration

Update `silvaengine_gateway/app.py`.

Required changes:

- Create one `ConnectionManager` inside `create_app()`.
- Set its event loop in the FastAPI lifespan startup block.
- Inject it into module config classes that expose `set_connection_manager(manager)`.
- Pass it to `build_router_from_manifest(...)`.
- During shutdown, close or unregister active connections.

Important ordering:

1. Initialize `GatewayConfig`.
2. Load and validate the manifest.
3. Initialize module configs.
4. Create the FastAPI app and lifespan.
5. On lifespan startup, set the WebSocket event loop and inject the manager before traffic is accepted.

If Phase 0 shows that a module config must receive the manager before `Config.initialize(...)`, adjust this order and document the reason in tests.

### 5.4 WebSocket Auth

Add a small helper in the gateway, for example `silvaengine_gateway/auth/websocket.py`.

The helper should use the same verification primitives as HTTP auth:

- Local mode: `verify_local_jwt(token)`
- Cognito mode: `await verify_cognito_jwt(token)`

Recommended MVP:

- Accept token in `?token=<jwt>`.
- Reject missing or invalid tokens before `websocket.accept()`.
- Close with application code `4001`.
- Avoid logging full WebSocket URLs or token values.

Future enhancement:

- Support first-message auth (`{"type": "auth", "token": "..."}`) with a short timeout. This avoids tokens in URLs but adds more state handling.

## 6. Core Engine Integration

Update `ai_agent_core_engine` only after Phase 0 confirms the current code shape.

### 6.1 Config

Add a nullable connection manager and accessors to the core config class:

```python
connection_manager = None

@classmethod
def set_connection_manager(cls, manager):
    cls.connection_manager = manager

@classmethod
def get_connection_manager(cls):
    return cls.connection_manager
```

If the core config currently requires API Gateway settings during initialization, add a local gateway mode so missing AWS WebSocket settings do not fail FastAPI deployments.

### 6.2 Stream Sender

Refactor `send_data_to_stream` to prefer the local manager and fall back to AWS:

```python
def send_data_to_stream(logger: logging.Logger, **kwargs: dict[str, Any]) -> bool:
    connection_id = kwargs.get("connection_id")
    data = kwargs.get("data")
    if not connection_id or data is None:
        raise ValueError("connection_id and data are required")

    manager = Config.get_connection_manager()
    if manager is not None:
        return manager.send_to_connection(connection_id, data)

    Config.get_api_gateway_client().post_to_connection(
        ConnectionId=connection_id,
        Data=Serializer.json_dumps(data),
    )
    return True
```

Backward compatibility requirement:

- Lambda/API Gateway deployments keep working when no connection manager is configured.
- FastAPI deployments do not require `api_id`, `api_stage`, or API Gateway credentials just to stream locally.

## 7. Route Manifest

After the gateway supports `handler_type: websocket`, add the ai-agent module route to `routes.yaml` or an environment-provided manifest:

```yaml
- name: ai_agent_core_engine
  package: ai_agent_core_engine
  transport: hybrid
  config_class: "ai_agent_core_engine.handlers.config:Config"
  config_init_style: dict
  routes:
    - path: "/{endpoint_id}/{part_id}/ai_agent_core_graphql"
      handler_type: graphql
      dispatch: "ai_agent_core_engine.main:dispatch_graphql"
      methods: ["POST"]
      auth: true

    - path: "/{endpoint_id}/{part_id}/ai_agent_core_ws"
      handler_type: websocket
      dispatch: "ai_agent_core_engine.main:dispatch_graphql"
      auth: true
```

If Phase 0 identifies a dedicated WebSocket dispatcher, use that instead of `dispatch_graphql`.

## 8. Streaming Protocol

Preserve the existing stream envelope:

```json
{
  "message_group_id": "<connection_id>-<run_uuid>",
  "data_format": "text",
  "index": 0,
  "chunk_delta": "partial text...",
  "is_message_end": false
}
```

Expected flow:

1. Client connects to `ws://localhost:8000/{endpoint_id}/{part_id}/ai_agent_core_ws?token=<jwt>`.
2. Gateway validates the token, accepts the socket, creates a `connection_id`, and sends `connection_ack`.
3. Client sends an ai-agent request message.
4. Gateway injects `connection_id`, route context, and user context, then calls the configured dispatch function in the shared executor.
5. Core streaming calls `send_data_to_stream(...)`.
6. `send_data_to_stream(...)` uses `ConnectionManager.send_to_connection(...)`.
7. Client receives chunk envelopes until `is_message_end` is true.

## 9. Configuration

Add WebSocket settings only when they control real behavior. Do not add unused environment variables.

Initial settings:

```bash
# WebSocket token mode. MVP supports query_param.
WEBSOCKET_AUTH_METHOD=query_param

# Required only after first-message auth is implemented.
# WEBSOCKET_AUTH_TIMEOUT=10
```

No `WEBSOCKET_ENABLED` flag is required for MVP. Route registration is controlled by the manifest: if a route declares `handler_type: websocket`, the gateway registers it.

Add `websockets` to the dev dependencies if the integration client uses that package.

## 10. Implementation Phases

### Phase 0: Contract Inventory

| Step | File or area | Change |
|---|---|---|
| 0.1 | `ai_agent_core_engine` | Verify stream sender, config lifecycle, and AWS fallback requirements. |
| 0.2 | `ai_agent_handler` | Verify envelope fields and invoker path. |
| 0.3 | Plan/tests | Update assumptions before implementation if contracts differ. |

### Phase 1: Gateway WebSocket Infrastructure

| Step | File | Change |
|---|---|---|
| 1.1 | `silvaengine_gateway/websocket_manager.py` | Add thread-safe `ConnectionManager`. |
| 1.2 | `silvaengine_gateway/auth/websocket.py` | Add WebSocket JWT verification helper. |
| 1.3 | `silvaengine_gateway/router_builder.py` | Add `websocket` handler type and route registration. |
| 1.4 | `silvaengine_gateway/app.py` | Create, start, inject, and pass `ConnectionManager`. |
| 1.5 | `silvaengine_gateway/tests/test_websocket_manager.py` | Add manager unit tests. |
| 1.6 | `silvaengine_gateway/tests/test_websocket_routes.py` | Add route/auth tests with FastAPI `TestClient`. |

### Phase 2: Core Streaming Integration

| Step | File | Change |
|---|---|---|
| 2.1 | `ai_agent_core_engine/handlers/config.py` | Add connection manager setter/getter. |
| 2.2 | `ai_agent_core_engine/handlers/at_agent_listener.py` | Prefer manager send, fall back to `post_to_connection()`. |
| 2.3 | `ai_agent_core_engine` config initialization | Skip API Gateway client initialization when running in local gateway mode. |
| 2.4 | Core tests | Cover local manager send and AWS fallback. |

### Phase 3: Route and Integration Wiring

| Step | File | Change |
|---|---|---|
| 3.1 | `silvaengine_gateway/routes.yaml` or override manifest | Add ai-agent GraphQL and WebSocket routes. |
| 3.2 | `pyproject.toml` | Add `websockets` to dev dependencies if used by integration scripts. |
| 3.3 | `silvaengine_gateway/tests/call_websocket.py` | Add manual integration client. |
| 3.4 | End-to-end environment | Connect, send ai-agent request, receive streamed chunks. |

### Phase 4: Production Hardening

| Step | File | Change |
|---|---|---|
| 4.1 | `websocket_manager.py` | Add heartbeat or ping/pong handling. |
| 4.2 | `websocket_manager.py` | Add idle timeout and cleanup. |
| 4.3 | `routes/health.py` | Add WebSocket health details if operationally useful. |
| 4.4 | Auth helper | Add first-message auth if URL tokens are unacceptable. |
| 4.5 | Observability | Add connection count, send failure, disconnect, and auth failure metrics/logs without token leakage. |

### Phase 5: Multi-Worker Scaling

Single-process connection storage is not valid for `uvicorn --workers N` or multiple gateway instances. Add a shared broker only after the single-worker path is proven.

Recommended direction:

- Maintain local connection ownership in each process.
- Publish remote sends through Redis or another deployment-approved broker.
- Subscribe each worker to delivery events and send only when it owns the target connection.
- Add explicit deployment guidance: single worker for MVP, broker-backed manager for horizontal scaling.

## 11. Testing Strategy

### Gateway Unit and App Tests

| Test | Target | Expected result |
|---|---|---|
| `test_connection_manager_register_unregister` | `ConnectionManager` | Connection count and active ids update correctly. |
| `test_connection_manager_send_missing` | `ConnectionManager` | Unknown connection returns `False`. |
| `test_connection_manager_send_from_thread` | `ConnectionManager` | Sync thread can schedule a send on the event loop. |
| `test_route_spec_allows_websocket_without_dispatch` | `RouteSpec` | WebSocket route validates without dispatch. |
| `test_route_spec_requires_dispatch_for_http_handlers` | `RouteSpec` | GraphQL/rest/background still require dispatch. |
| `test_websocket_missing_token` | WebSocket handler | Connection closes with `4001`. |
| `test_websocket_invalid_token` | WebSocket handler | Connection closes with `4001`. |
| `test_websocket_context_injection` | WebSocket handler | Dispatch receives partition, user, and connection context. |

### Core Tests

| Test | Target | Expected result |
|---|---|---|
| `test_send_data_to_stream_uses_connection_manager` | Core stream sender | Sends through manager when configured. |
| `test_send_data_to_stream_aws_fallback` | Core stream sender | Uses `post_to_connection()` when manager is absent. |
| `test_config_local_mode_does_not_require_apigw` | Core config | FastAPI local mode initializes without API Gateway settings. |

### Manual Integration Script

Use a small `websockets` client only after unit/app tests pass:

```python
import asyncio
import json
import websockets


async def main():
    token = get_auth_token()
    uri = f"ws://localhost:8000/gpt/nestaging/ai_agent_core_ws?token={token}"

    async with websockets.connect(uri) as ws:
        ack = json.loads(await ws.recv())
        connection_id = ack["connection_id"]

        await ws.send(json.dumps({
            "action": "ask_model",
            "arguments": {
                "agent_uuid": "...",
                "thread_uuid": "...",
                "prompt": "Hello",
            },
        }))

        chunks = []
        while True:
            message = json.loads(await ws.recv())
            chunks.append(message)
            if message.get("is_message_end"):
                break

        print("connection_id:", connection_id)
        print("response:", "".join(c.get("chunk_delta", "") for c in chunks))


asyncio.run(main())
```

## 12. File Summary

| File | Action | Phase |
|---|---|---|
| `docs/websocket_plan.md` | Rephrased and updated plan | Current review |
| `silvaengine_gateway/websocket_manager.py` | New | 1 |
| `silvaengine_gateway/auth/websocket.py` | New | 1 |
| `silvaengine_gateway/router_builder.py` | Modify | 1 |
| `silvaengine_gateway/app.py` | Modify | 1 |
| `silvaengine_gateway/routes.yaml` | Modify after WebSocket support exists | 3 |
| `silvaengine_gateway/tests/test_websocket_manager.py` | New | 1 |
| `silvaengine_gateway/tests/test_websocket_routes.py` | New | 1 |
| `silvaengine_gateway/tests/call_websocket.py` | New optional integration helper | 3 |
| `pyproject.toml` | Add dev dependency for integration helper if needed | 3 |
| `ai_agent_core_engine/handlers/config.py` | Modify in external module | 2 |
| `ai_agent_core_engine/handlers/at_agent_listener.py` | Modify in external module | 2 |
| `ai_agent_core_engine/tests/test_send_data_to_stream.py` | New in external module | 2 |

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| WebSocket plan assumes stale ai-agent contracts | Start with Phase 0 inventory and update tests before code changes. |
| Registry race between async sockets and sync dispatch threads | Protect the connection registry with a lock and keep send scheduling on the event loop. |
| Send failures are lost in fire-and-forget mode | Attach a done callback to the scheduled future and log exceptions. |
| Tokens leak through query strings | Do not log full URLs; use WSS in production; add first-message auth if required. |
| Lambda deployments break | Keep AWS `post_to_connection()` fallback and test both paths. |
| FastAPI local deployments still require API Gateway settings | Add local gateway mode or conditional API Gateway client initialization in core config. |
| Multi-worker deployments lose messages | Document single-worker MVP; implement broker-backed delivery before horizontal scaling. |
| Route context differs from HTTP behavior | Mirror HTTP context injection and explicitly use path `part_id` for WebSocket partitioning. |

## 14. Release Gates

Do not mark the WebSocket migration complete until all of these pass:

- Gateway unit tests pass, including WebSocket route validation and auth behavior.
- Core stream sender tests pass for both local manager and AWS fallback.
- Manual or automated integration test receives multiple stream chunks and a final `is_message_end=true` envelope.
- Existing HTTP routes and auth tests still pass.
- Deployment notes state whether the release supports only one worker or includes broker-backed scaling.
- Rollback path is documented: remove the WebSocket route from the manifest and keep AWS API Gateway streaming active.
