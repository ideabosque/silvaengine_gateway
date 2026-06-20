# WebSocket Streaming Performance Improvement Plan

> Status: Draft - 2026-06-19
> Baseline: about 10 seconds from request submission to first streaming chunk

## 1. Current Timing Breakdown

The timings below were measured from server logs in `/tmp/gw_debug7.log`.

| Phase | Start | End | Duration | Location |
|---|---:|---:|---:|---|
| Core engine init and async-task pre-create | 27.8s | 28.8s | 1.0s | `dispatch_ask_model` -> `Config.initialize` and `AsyncTaskModel.save()` |
| MCP daemon init, first batch | 30.6s | 31.5s | 0.9s | `resolve_mcp_function_list` -> MCP daemon GraphQL init |
| 13 MCP `graphql_service_initialization` calls | 31.5s | 33.8s | 2.3s | MCP daemon tool resolution, 80 tools across 13 categories |
| `resolve_message_list` | 34.3s | 34.7s | 0.4s | DynamoDB message history query |
| `resolve_tool_call_list` | 34.7s | 35.1s | 0.4s | DynamoDB tool-call history query |
| Message insert | 34.8s | 35.2s | 0.4s | `insert_update_message` for the user message |
| Run update | 35.2s | 35.2s | ~0s | `insert_update_run` for the run record |
| Second core engine init | 35.5s | 35.5s | ~0s | `get_ai_agent_handler` -> `Invoker.resolve_proxied_callable` |
| Second MCP daemon init | 36.3s | 36.5s | 0.2s | MCP tool resolution inside the handler constructor |
| Tools loaded | 36.5s | 36.5s | ~0s | 80-tool list assembled |
| LLM first token | 36.5s | 37.5s | 1.0s | GLM-5.2 streaming first delta |
| Total | | | ~9.7s | |

LLM latency is only about 1 second. The remaining time is setup and persistence overhead before streaming starts.

## 2. Bottleneck Analysis

### 2.1 MCP Tool Loading: About 3.2s

- 80 tools are loaded from the MCP daemon engine through GraphQL.
- Tool loading triggers 13 separate `graphql_service_initialization` calls, one per MCP tool category.
- Each initialization creates a new `Graphql` subclass instance through the `@graphql_service_initialization` decorator.
- Tools are resolved fresh for every request; there is no cache across requests.
- The MCP daemon path initializes twice per request: once in `resolve_mcp_function_list`, then again inside the handler constructor.

Root cause: `get_ai_agent_handler()` calls `Invoker.resolve_proxied_callable()`, which triggers MCP daemon `Graphql.__init__()` for each tool category. No reusable tool cache exists on this path.

### 2.2 Duplicate Initialization: About 1.2s

- `dispatch_ask_model` calls `_build_engine_from_config()`, which creates an `AIAgentCoreEngine` and calls `Config.initialize()`.
- `Config.initialize()` is guarded, but `Graphql.__init__` still runs its initialization decorator on every construction.
- `get_ai_agent_handler()` also calls `Invoker.resolve_proxied_callable()`, which triggers handler and MCP tool initialization again.
- The MCP daemon path appears twice in one request: first around 31.5s, then again around 36.3s.

### 2.3 Blocking DB Writes: About 1.2s

- `dispatch_ask_model` pre-creates `AsyncTaskModel` and `RunModel` with synchronous DynamoDB `save()` calls.
- `execute_ask_model` then performs `insert_update_message`, `insert_update_run`, and `calculate_num_tokens`.
- These steps are sequential and run before the first token can be streamed.
- The `insert_update_decorator` performs a count query before each insert, adding an extra DB round trip.

### 2.4 Gateway Overhead: About 0.05s

- `silvaengine_gateway/router_builder.py` already yields for `asyncio.sleep(0.05)` after dispatch to let pending `ConnectionManager` sends drain before the final dispatch result is sent.
- WebSocket authentication and `connection_ack` are negligible compared with MCP initialization and DB writes.
- `build_setting_from_env()` runs at startup, not per request.

## 3. Improvement Plan

### Phase 1: Low-Risk Gateway/Core Setup Wins

#### 1.1 Cache the `AIAgentCoreEngine` instance

File: `ai_agent_core_engine/main.py`, `_build_engine_from_config()`

Cache the engine as a module-level singleton instead of creating a new `AIAgentCoreEngine` for every WebSocket message.

```python
_engine_instance = None

def _build_engine_from_config() -> AIAgentCoreEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AIAgentCoreEngine(Config.get_logger(), **Config.get_setting())
    return _engine_instance
```

Expected savings: 0.1-0.2s after the first request.

Risk: Low. `Config.initialize()` is already singleton guarded. This is acceptable if the engine instance is request-stateless and settings do not need to change during process lifetime.

#### 1.2 Move async-task and run pre-creation off the critical path

File: `ai_agent_core_engine/main.py`, `dispatch_ask_model()`

The current pre-create writes are synchronous DynamoDB operations. Move them into a background thread only after confirming downstream update code tolerates records that are not visible immediately.

```python
import threading

def _pre_create_records(async_task_uuid, partition_key, arguments, run_uuid):
    try:
        AsyncTaskModel(...).save()
    except Exception:
        pass

    if run_uuid:
        try:
            RunModel(...).save()
        except Exception:
            pass

threading.Thread(
    target=_pre_create_records,
    args=(async_task_uuid, partition_key, arguments, run_uuid),
    daemon=True,
).start()
```

Expected savings: about 0.4s.

Risk: Medium. If `async_execute_ask_model` or the insert/update decorators require pre-created records to exist immediately, this can introduce a race. Verify that missing pre-created records are handled gracefully before enabling this change.

### Phase 2: MCP Tool Caching

#### 2.1 Cache MCP tools across requests

File: `ai_agent_core_engine/handlers/ai_agent_utility.py`, `get_ai_agent_handler()`

Cache the resolved tool list per partition or per agent context, with a short TTL.

```python
_tool_cache = {}  # cache_key -> (tools, cached_at)
_tool_cache_ttl = 300

def get_ai_agent_handler(info, agent):
    cache_key = f"{info.context.get('endpoint_id')}#{info.context.get('part_id')}"
    cached = _tool_cache.get(cache_key)
    if cached and (time.time() - cached[1] < _tool_cache_ttl):
        tools = cached[0]
    else:
        tools = resolve_tools(...)
        _tool_cache[cache_key] = (tools, time.time())
```

Expected savings: about 3 seconds on cache hits.

Risk: Medium. Tool definitions can become stale if the MCP daemon changes. Start with a short TTL and add explicit invalidation if operators need immediate refresh.

#### 2.2 Deduplicate MCP daemon initialization

The MCP daemon initializes twice per request:

1. During `resolve_mcp_function_list`.
2. During `get_ai_agent_handler()` when `Invoker.resolve_proxied_callable()` constructs the handler.

Fix options:

- Pass the already-resolved tools into the handler constructor.
- Cache the MCP daemon client or handler instance.
- Move tool resolution behind a shared cache used by both paths.

Expected savings: 0.2-0.5s.

Risk: Low to medium, depending on whether the handler has hidden request-scoped state.

### Phase 3: DB Write Optimization

#### 3.1 Parallelize pre-streaming DB writes

File: `ai_agent_core_engine/handlers/ai_agent.py`, `execute_ask_model()`

`insert_update_message` and `insert_update_run` are independent enough to run in parallel if both receive all required identifiers up front.

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=2) as pool:
    msg_future = pool.submit(insert_update_message, info, **msg_kwargs)
    run_future = pool.submit(insert_update_run, info, **run_kwargs)
    user_message = msg_future.result()
    run = run_future.result()
```

Expected savings: about 0.4s.

Risk: Low if the two writes are independent. Confirm there is no implicit ordering requirement between user-message creation and run-record update.

#### 3.2 Skip count checks for known inserts

File: `silvaengine_dynamodb_base/decorators.py`, `insert_update_decorator`

The decorator currently performs a count query before each insert. Add an explicit `skip_count_check=True` option for call sites that know they are creating a new record.

Expected savings: about 0.2s per insert, or roughly 0.4s for two inserts.

Risk: Low if the option is opt-in and only used where uniqueness is already guaranteed.

### Phase 4: Streaming Pipeline Optimization

#### 4.1 Start the LLM call earlier

Current order:

1. Read history and write request metadata.
2. Build the AI agent handler.
3. Start the LLM call.

The LLM call cannot start until `input_messages` is available, but user-message and run-record writes can likely be deferred or overlapped after history is read.

Expected savings: 1-2s if DB writes overlap with LLM startup.

Risk: High. This changes ordering semantics and can affect observability, retries, and audit consistency. Defer until the lower-risk phases are implemented and measured.

## 4. Expected Results

| Phase | Expected savings | Risk | Effort |
|---|---:|---|---|
| 1.1 Cache engine | 0.1-0.2s | Low | Small |
| 1.2 Async pre-create | 0.4s | Medium | Small |
| 2.1 MCP tool cache | 3.0s | Medium | Medium |
| 2.2 Deduplicate MCP init | 0.2-0.5s | Low/Medium | Medium |
| 3.1 Parallel DB writes | 0.4s | Low | Small |
| 3.2 Skip count query | 0.4s | Low | Small |
| 4.1 Start LLM earlier | 1-2s | High | Medium |
| Total realistic first pass | 4-5s | | |

Baseline: about 10 seconds to first chunk.

Target after low- and medium-risk phases: about 5-6 seconds to first chunk. A 4-5 second target likely requires Phase 4 or deeper MCP daemon changes.

## 5. Recommended Priority

1. Cache the engine instance.
2. Add MCP tool caching with a short TTL.
3. Deduplicate MCP daemon initialization.
4. Parallelize independent DB writes.
5. Add an opt-in skip-count path for known inserts.
6. Move pre-create writes off the critical path after race behavior is verified.
7. Defer early LLM startup until the safer changes have measured impact.

## 6. Verification

After each phase, restart the gateway and measure at least five warm requests.

```bash
python silvaengine_gateway/tests/run_daemon.py

for i in 1 2 3 4 5; do
    python silvaengine_gateway/tests/call_websocket.py --prompt "Hi" --timeout 120 2>&1 \
        | grep -E 'is_message_end|Elapsed|chunks'
done
```

Record these metrics for each run:

- Time to first streaming chunk.
- Total request elapsed time.
- Chunk count.
- Whether the request was cold or warm.
- MCP tool count and cache hit/miss state.
