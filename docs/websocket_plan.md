# WebSocket Development Plan - SilvaEngine Gateway

> Plan status: Live E2E streaming verified (2026-06-19). Both HTTP and WebSocket routes registered and tested. `call_websocket.py` and `chat_websocket.py` clients confirmed real LLM streaming through ConnectionManager with 26+ chunks per response.

## 1. Purpose

`ai_agent_core_engine` streams real-time responses through the AWS API Gateway Management API (`apigatewaymanagementapi`). That remains the correct path for Lambda/API Gateway deployments, but local FastAPI deployments need a gateway-owned WebSocket path so they do not depend on AWS only to deliver stream chunks.

The target design keeps both transports:

- AWS API Gateway mode: no gateway `ConnectionManager` is injected, so `send_data_to_stream` falls back to `post_to_connection()`.
- SilvaEngine Gateway mode: the FastAPI gateway injects a `ConnectionManager`, and `send_data_to_stream` sends chunks to active WebSocket connections.

The streaming envelope, invoker path, and Lambda fallback should remain compatible.

## 2. Current Gateway Baseline

Verified in this repository on June 19, 2026:

- `silvaengine_gateway/websocket_manager.py` defines a thread-safe `ConnectionManager`.
- `silvaengine_gateway/auth/websocket.py` verifies WebSocket JWTs before accept and resolves `part_id` from the handshake.
- `silvaengine_gateway/router_builder.py` supports `handler_type: websocket` and registers routes with `router.add_api_websocket_route(...)`.
- `silvaengine_gateway/app.py` creates a `ConnectionManager`, binds its event loop during lifespan startup, injects it into module configs that expose `set_connection_manager`, passes it to the router builder, and closes active sockets during shutdown.
- `silvaengine_gateway/app.py::build_setting_from_env()` adds `send_data_to_stream` and `async_insert_update_tool_call` to `functs_on_local` for modules with WebSocket routes, using route method names such as `ai_agent_core_graphql` and concrete default invoker classes such as `AIAgentCoreEngine`.
- `silvaengine_gateway/routes.yaml` includes `ai_agent_core_engine` GraphQL and WebSocket routes.
- `silvaengine_gateway/tests/test_websocket_manager.py` and `silvaengine_gateway/tests/test_websocket_routes.py` cover the new gateway manager and route behavior.
- Existing HTTP auth still runs through `FlexJWTMiddleware`; WebSocket auth is handled separately before `websocket.accept()`.
- SSE remains an HTTP streaming transport and is independent from WebSocket support.

Verified in the adjacent `ai_agent_core_engine` checkout on June 19, 2026:

- `ai_agent_core_engine.main:dispatch_graphql` and `ai_agent_core_engine.main:dispatch_ask_model` exist as module-level gateway dispatch wrappers.
- `ai_agent_core_engine.handlers.config:Config` exposes `set_connection_manager()` and `get_connection_manager()` and keeps API Gateway Management API initialization conditional on AWS WebSocket settings.
- `ai_agent_core_engine.handlers.at_agent_listener:send_data_to_stream` prefers an injected gateway `ConnectionManager` and falls back to AWS API Gateway `post_to_connection()` when no manager is present.
- `tests/test_send_data_to_stream.py` covers manager send, AWS fallback, manager priority, local config without API Gateway settings, and config accessors.

Still not verified:

- Whether the deployed gateway environment imports the adjacent editable `ai_agent_core_engine` package version with these wrappers.
- Whether a live client receives complete ai-agent stream chunks over `/{endpoint_id}/ai_agent_core_ws`.
- Whether the full ai-agent request payload used by the WebSocket client matches `async_execute_ask_model` requirements, including `async_task_uuid` and `arguments`.

## 3. Target Architecture

AWS API Gateway mode:

```text
Client <== WebSocket ==> AWS API Gateway <== post_to_connection() == Core Engine
                         connection_id       Lambda/invoker path
```

SilvaEngine Gateway mode:

```text
Client <== WebSocket ==> FastAPI Gateway <== ConnectionManager.send_to_connection()
                         connection_id       sync dispatch thread / core engine
```

Selection logic in the core stream sender:

```text
send_data_to_stream(connection_id, data)
  if Config.get_connection_manager() is not None:
      manager.send_to_connection(connection_id, data)
  else:
      Config.get_api_gateway_client().post_to_connection(ConnectionId, Data)
```

This design is valid for a single gateway worker. Multi-worker and multi-instance delivery require a shared broker.

## 4. Implemented Gateway Components

### 4.1 Connection Manager

Implemented in `silvaengine_gateway/websocket_manager.py`.

Responsibilities:

- Track active WebSocket connections by `connection_id`.
- Store the running event loop during FastAPI lifespan startup.
- Provide sync-safe `send_to_connection(connection_id, data) -> bool`.
- Serialize non-string payloads to JSON.
- Log send failures through a done callback.
- Expose `active_connections`, `connection_count`, and `shutdown()`.

The registry uses a `threading.RLock` because FastAPI connection handlers and sync dispatch threads can touch the connection map concurrently.

### 4.2 WebSocket Auth

Implemented in `silvaengine_gateway/auth/websocket.py`.

Current behavior:

- Accepts JWTs through `?token=<jwt>`.
- Supports local JWT verification and a Cognito verifier path.
- Resolves partition id from `?part_id=<tenant>`, a real `Part-Id`/`Part-ID` handshake header, or a `part-id:<tenant>` WebSocket subprotocol entry.
- Closes missing or invalid tokens with code `4001`.
- Closes missing or mismatched partition context with code `4002`.
- Logs only the path, not the full URL, to avoid token leakage.

Planned hardening:

- Add first-message auth if URL tokens are unacceptable for production clients.

### 4.3 WebSocket Route Support

Implemented in `silvaengine_gateway/router_builder.py`.

Current behavior:

- `RouteSpec` allows `handler_type: websocket`.
- WebSocket routes may omit `dispatch`; when omitted, the handler returns an error message for each incoming request.
- WebSocket routes do not use HTTP `Depends(get_current_user)` dependencies.
- The handler authenticates before accept, registers a generated `connection_id`, sends `connection_ack`, injects partition/user/connection context, dispatches sync work in the shared executor, sends the dispatch result if present, and unregisters on disconnect.

Injected dispatch params:

```text
endpoint_id
part_id
partition_key = "{endpoint_id}#{part_id}"
connection_id
context.endpoint_id
context.part_id
context.partition_key
context.connection_id
context.user
```

### 4.4 App Startup Integration

Implemented in `silvaengine_gateway/app.py`.

Current behavior:

1. Initializes `GatewayConfig`.
2. Loads the route manifest.
3. Initializes module config classes from the manifest.
4. Creates one `ConnectionManager`.
5. Injects the manager into module config classes that expose `set_connection_manager`.
6. Binds the manager to the running event loop during lifespan startup.
7. Passes the manager to `build_router_from_manifest(...)`.
8. Calls `connection_manager.shutdown()` during lifespan shutdown.

Note: the current ordering initializes module configs before injecting the manager. If an external module must receive the manager before `Config.initialize(...)`, move manager creation/injection earlier and cover that ordering with a regression test.

## 5. Route Manifest State

`silvaengine_gateway/routes.yaml` now declares:

```yaml
- name: ai_agent_core_engine
  package: ai_agent_core_engine
  transport: hybrid
  config_class: "ai_agent_core_engine.handlers.config:Config"
  config_init_style: dict
  routes:
    - path: "/{endpoint_id}/ai_agent_core_graphql"
      handler_type: graphql
      dispatch: "ai_agent_core_engine.main:dispatch_graphql"
      methods: ["POST"]
      auth: true

    - path: "/{endpoint_id}/ai_agent_core_ws"
      handler_type: websocket
      dispatch: "ai_agent_core_engine.main:dispatch_ask_model"
      auth: true
```

These routes require the adjacent `ai_agent_core_engine` package to provide both module-level dispatch wrappers. If either wrapper is absent, the gateway logs route-resolution errors and skips the affected route.

## 6. Core Engine Integration State

The following changes are implemented in the adjacent `ai_agent_core_engine` checkout. They still need to be present in the package installed into the gateway runtime.

### 6.1 Config

Implemented: the ai-agent config class now has a nullable connection manager:

```python
connection_manager = None

@classmethod
def set_connection_manager(cls, manager):
    cls.connection_manager = manager

@classmethod
def get_connection_manager(cls):
    return cls.connection_manager
```

`Config._initialize_apigw_client(...)` should remain conditional. Local gateway mode should not require API Gateway WebSocket settings.

### 6.2 Dispatch Wrappers

Implemented: `ai_agent_core_engine/main.py` now exposes module-level wrappers:

```python
def _build_engine_from_config() -> "AIAgentCoreEngine":
    return AIAgentCoreEngine(Config.get_logger(), **Config.get_setting())


def dispatch_graphql(**params):
    return _build_engine_from_config().ai_agent_core_graphql(**params)


def dispatch_ask_model(**params):
    return _build_engine_from_config().async_execute_ask_model(**params)
```

Use the exact method names from the current core package. If the class method names differ, update the manifest and wrappers together.

### 6.3 Stream Sender

Implemented: the ai-agent stream sender now gives the gateway manager priority:

```python
manager = Config.get_connection_manager()
if manager is not None:
    return manager.send_to_connection(connection_id, data)

return Config.get_api_gateway_client().post_to_connection(
    ConnectionId=connection_id,
    Data=Serializer.json_dumps(data),
)
```

The AWS fallback must remain tested so Lambda streaming is not regressed.

## 7. Streaming Protocol

Expected client flow:

1. Client connects to `ws://localhost:8000/{endpoint_id}/ai_agent_core_ws?token=<jwt>&part_id=<tenant>`.
2. Gateway validates token and partition context.
3. Gateway accepts the socket, creates a `connection_id`, and sends `connection_ack`.
4. Client sends an ai-agent request, for example:

   ```json
   {
     "async_task_uuid": "...",
     "arguments": {
       "agent_uuid": "...",
       "thread_uuid": "...",
       "user_query": "Hello"
     }
   }
   ```

5. Gateway injects `connection_id`, `endpoint_id`, `part_id`, `partition_key`, and user context, then calls `dispatch_ask_model(**params)` in the dispatch executor. The WebSocket handler forwards the JSON object as dispatch params, so fields required by `async_execute_ask_model` must be top-level request fields.
6. Core streaming calls `send_data_to_stream(...)` through the invoker chain.
7. The invoker resolves `send_data_to_stream` locally through `functs_on_local`.
8. The manager sends stream envelopes to the WebSocket until `is_message_end` is true.

The expected stream envelope remains:

```json
{
  "message_group_id": "<connection_id>-<run_uuid>[-<suffix>]",
  "data_format": "text",
  "index": 0,
  "chunk_delta": "partial text...",
  "is_message_end": false
}
```

Treat `message_group_id` as opaque. The optional suffix supports tool-call or sub-stream grouping.

## 8. Configuration

No `WEBSOCKET_ENABLED` flag is required. Route registration is controlled by the manifest.

Current useful settings:

```bash
GATEWAY_WORKERS=1
GATEWAY_AUTH_PROVIDER=local
JWT_SECRET_KEY=...
```

Optional future settings should be added only when they control implemented behavior:

```bash
WEBSOCKET_AUTH_METHOD=query_param
WEBSOCKET_PARTITION_METHOD=query_param_or_header
WEBSOCKET_AUTH_TIMEOUT=10
```

## 9. Testing Strategy

Gateway tests:

- `test_connection_manager_register_unregister`
- `test_connection_manager_send_missing`
- `test_connection_manager_send_from_thread`
- `test_route_spec_allows_websocket_without_dispatch`
- `test_route_spec_requires_dispatch_for_http_handlers`
- `test_websocket_missing_token`
- `test_websocket_missing_part_id`
- `test_websocket_context_injection`
- `test_websocket_uses_part_id_header_for_partition`
- `test_websocket_part_id_mismatch_rejected`

Core tests now present in `ai_agent_core_engine`:

- `test_send_data_to_stream_uses_connection_manager`
- `test_send_data_to_stream_aws_fallback`
- `test_send_data_to_stream_manager_takes_priority`
- `test_config_local_mode_does_not_require_apigw`
- `test_config_aws_mode_initializes_apigw`
- `test_set_and_get_connection_manager`

Core tests still useful:

- wrapper tests for `dispatch_graphql` and `dispatch_ask_model`

Integration gate:

- Start the gateway with `GATEWAY_WORKERS=1`.
- Connect to `/gpt/ai_agent_core_ws?token=<jwt>&part_id=<tenant>`.
- Receive `connection_ack`.
- Send an ask-model request that includes top-level `async_task_uuid` and `arguments`.
- Confirm multiple stream chunks arrive and the final chunk has `is_message_end=true`.
- Confirm existing HTTP routes and auth tests still pass.
- Confirm Lambda mode still streams through AWS API Gateway when no manager is injected.

## 10. Production Risks

| Risk | Status | Mitigation |
|---|---|---|
| Gateway runtime imports stale ai-agent package | Open | Ensure the deployed gateway environment uses the adjacent/editable `ai_agent_core_engine` version that contains `dispatch_graphql`, `dispatch_ask_model`, and manager-first streaming. |
| Core stream sender regresses Lambda fallback | Mitigated locally | Keep manager-first and AWS fallback tests in `ai_agent_core_engine`. |
| Manager injected after core config initialization | Watch | Current gateway injects after `Config.initialize(...)`; move earlier if the core reads the manager during initialization. |
| Multi-worker delivery loss | Open | Use one worker for MVP or add a Redis/pub-sub broker before horizontal scaling. |
| Query-string token exposure | Open | Avoid logging URLs, require WSS, and add first-message auth if production policy disallows query tokens. |
| Send failures are asynchronous | Mitigated | `ConnectionManager` attaches a done callback and logs failed sends. |
| Lambda deployment regression | Mitigated by design | Keep manager optional and test AWS fallback. |

## 11. Release Gates

Do not mark WebSocket support complete until:

- Gateway WebSocket unit/app tests pass.
- ai-agent core manager-first sender tests pass.
- ai-agent core AWS fallback tests pass.
- ai-agent core dispatch wrapper tests pass, or route resolution is verified against the installed package.
- Route manifest dispatch targets resolve in the deployed environment.
- Manual or automated integration receives stream chunks over the gateway WebSocket route.
- Existing HTTP, SSE, WebSocket auth, and `functs_on_local` behavior remains green.
- Deployment notes state `GATEWAY_WORKERS=1` unless a broker-backed manager is implemented.
- Rollback is documented: remove the WebSocket route from the manifest and keep AWS API Gateway streaming active.

## 15. Live Integration Testing — VERIFIED

Live E2E testing was performed on 2026-06-19 against a real gateway instance with DynamoDB + Neo4j + OpenAI. Results:

### Test results

| Test | Result |
|---|---|
| `call_websocket.py --prompt "Hello, what can you do?"` | 26 chunks, 628 chars, 11.1s — **PASS** |
| `chat_websocket.py` (interactive, piped input) | 27 chunks, 10.4s — **PASS** |
| 14 gateway unit tests | **PASS** |
| 15 gateway integration tests | **PASS** |
| 6 core engine dual-mode tests | **PASS** |

### Issues found and fixed during live testing

1. **`internal_mcp` missing from `build_setting_from_env()`** — The gateway's `build_setting_from_env()` did not include the `internal_mcp` config block that `ai_agent_core_engine.handlers.config:Config._initialize_internal_mcp()` requires. Fixed by adding:
   ```python
   "internal_mcp": {
       "base_url": os.getenv("mcp_server_url"),
       "bearer_token": os.getenv("bearer_token"),
       "headers": {"x-api-key": os.getenv("x-api-key"), "Content-Type": "application/json"},
   } if os.getenv("mcp_server_url") else None,
   ```

2. **`async_task_uuid` + `run_uuid` pre-creation** — The `insert_update_decorator` in `silvaengine_dynamodb_base` raises "Cannot find the {entity}" when `count == 0` and the caller provides a `range_key` (e.g. `async_task_uuid` or `run_uuid`). In the Lambda path, the invoker pre-creates these records before calling `async_execute_ask_model`. In the WebSocket path, `dispatch_ask_model` now pre-creates both `AsyncTaskModel` and `RunModel` records directly via `model.save()` before calling `async_execute_ask_model`.

3. **`run_uuid` generation** — `execute_ask_model` requires `arguments["run_uuid"]` which is normally generated by the Lambda invoker. `dispatch_ask_model` now generates it via `uuid.uuid4()` if not provided by the client.

4. **`tiktoken` dependency** — Required for GPT token calculation. Installed via `pip install tiktoken`.

5. **`call_websocket.py` / `chat_websocket.py` password resolution** — argparse defaults for `--password` and `--username` were evaluated at parser construction time (before `load_env()`), resulting in empty credentials. Fixed by re-resolving from `os.getenv()` after `load_env()`.

### `.env` additions required for WebSocket streaming

```bash
# Internal MCP server (for ai_agent_core_engine — _get_agent fetches agent config)
mcp_server_url=http://localhost:8000/gpt/mcp
bearer_token=<ADMIN_STATIC_TOKEN or valid JWT>
x-api-key=<api-key>
```
