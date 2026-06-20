# WebSocket Streaming Performance Improvement Plan

> Status: Draft — 2026-06-19
> Baseline: ~10s from request to first streaming chunk

## 1. Current Timing Breakdown

Measured from server logs (`/tmp/gw_debug7.log`):

| Phase | Start | End | Duration | Location |
|---|---|---|---|---|
| Core engine init + async_task pre-create | 27.8s | 28.8s | 1.0s | `dispatch_ask_model` → `Config.initialize` + `AsyncTaskModel.save()` |
| MCP daemon init (first batch) | 30.6s | 31.5s | 0.9s | `resolve_mcp_function_list` → MCP daemon GraphQL init |
| 13× `graphql_service_initialization` (MCP) | 31.5s | 33.8s | 2.3s | MCP daemon tool resolution (80 tools, 13 categories) |
| `resolve_message_list` | 34.3s | 34.7s | 0.4s | DynamoDB message history query |
| `resolve_tool_call_list` | 34.7s | 35.1s | 0.4s | DynamoDB tool-call history query |
| Message insert | 34.8s | 35.2s | 0.4s | `insert_update_message` (user message) |
| Run update | 35.2s | 35.2s | ~0s | `insert_update_run` (run record) |
| **Second** core engine init | 35.5s | 35.5s | ~0s | `get_ai_agent_handler` → `Invoker.resolve_proxied_callable` |
| **Second** MCP daemon init | 36.3s | 36.5s | 0.2s | MCP tool resolution inside handler constructor |
| TOOLS_LOADED (80 tools) | 36.5s | 36.5s | ~0s | Tool list assembled |
| **LLM first token** | 36.5s | 37.5s | **1.0s** | GLM-5.2 streaming first delta |
| **Total** | | | **~9.7s** | |

LLM latency is only ~1s. The other ~9s is setup overhead.

## 2. Bottleneck Analysis

### 2.1 MCP Tool Loading (~3.2s) — BIGGEST

- 80 tools loaded from MCP daemon engine via GraphQL
- 13 separate `graphql_service_initialization` calls (one per MCP tool category)
- Each init creates a new `Graphql` subclass instance with `@graphql_service_initialization` decorator
- Tools are resolved **fresh on every request** — no caching across requests
- The MCP daemon engine itself initializes twice per request (once in `resolve_mcp_function_list`, once in handler constructor)

**Root cause**: `get_ai_agent_handler()` → `Invoker.resolve_proxied_callable()` → MCP daemon `Graphql.__init__()` for each tool category. No tool cache exists.

### 2.2 Duplicate Initialization (~1.2s)

- `dispatch_ask_model` calls `_build_engine_from_config()` → `AIAgentCoreEngine(logger, **setting)` → `Config.initialize()` (guarded, returns immediately if `_initialized`)
- But `Graphql.__init__` still runs `@graphql_service_initialization` decorator on every call
- `get_ai_agent_handler()` calls `Invoker.resolve_proxied_callable()` which also triggers initialization
- The MCP daemon is initialized twice: once for `resolve_mcp_function_list` (line 31.5s), again inside the handler constructor (line 36.3s)

### 2.3 DB Writes (~1.2s) — Blocking

- `dispatch_ask_model` pre-creates `AsyncTaskModel` and `RunModel` (2 synchronous DynamoDB `save()` calls)
- `execute_ask_model` then does `insert_update_message` (user message) + `insert_update_run` (run record) + `calculate_num_tokens` (CPU-bound)
- All of these are sequential, blocking the event loop's thread pool executor
- The `insert_update_decorator` does a count query before each insert (extra DB round-trip)

### 2.4 Gateway Overhead (~0.1s)

- `await asyncio.sleep(0.1)` after `run_in_executor` (race condition fix)
- WebSocket auth + connection_ack — negligible
- `build_setting_from_env()` — runs once at startup, not per-request

## 3. Improvement Plan

### Phase 1: Gateway-Side Quick Wins (~0.5s savings)

#### 1.1 Cache the `AIAgentCoreEngine` instance

**File**: `ai_agent_core_engine/main.py` — `_build_engine_from_config()`

Currently creates a new `AIAgentCoreEngine` on every WebSocket message. Cache it as a module-level singleton:

```python
_engine_instance = None

def _build_engine_from_config() -> AIAgentCoreEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AIAgentCoreEngine(Config.get_logger(), **Config.get_setting())
    return _engine_instance
```

**Savings**: Eliminates `Graphql.__init__` + `@graphql_service_initialization` on every request after the first. ~0.1-0.2s per request.

**Risk**: Low — `Config.initialize()` is already a singleton with `_initialized` guard. The engine instance is stateless between requests (setting/logger don't change).

#### 1.2 Make `async_task` + `run` pre-creation non-blocking

**File**: `ai_agent_core_engine/main.py` — `dispatch_ask_model()`

Currently `AsyncTaskModel.save()` and `RunModel.save()` are synchronous DynamoDB calls that block the thread pool. Move them to a background thread:

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

# In dispatch_ask_model:
threading.Thread(
    target=_pre_create_records,
    args=(async_task_uuid, partition_key, arguments, run_uuid),
    daemon=True,
).start()
```

**Savings**: ~0.4s (2 DynamoDB writes moved off the critical path)

**Risk**: Medium — if the pre-creation thread is slower than `async_execute_ask_model`, the `insert_update_decorator` will raise "Cannot find". Need to verify the core engine's `insert_update_async_task` can handle a race where the pre-created record doesn't exist yet. The `try/except pass` in `dispatch_ask_model` already handles this gracefully.

### Phase 2: Core Engine MCP Tool Caching (~3s savings)

#### 2.1 Cache MCP tools across requests

**File**: `ai_agent_core_engine/handlers/ai_agent_utility.py` — `get_ai_agent_handler()`

Tools are resolved fresh on every request via MCP daemon GraphQL queries. Cache the tool list per `(endpoint_id, part_id)` combination with a TTL:

```python
_tool_cache: Dict[str, tuple] = {}  # key: "endpoint_id#part_id"
_tool_cache_ttl = 300  # 5 minutes

def get_ai_agent_handler(info, agent):
    cache_key = f"{info.context.get('endpoint_id')}#{info.context.get('part_id')}"
    cached = _tool_cache.get(cache_key)
    if cached and (time.time() - cached[1] < _tool_cache_ttl):
        # Reuse cached tools, skip MCP resolution
        ...
```

**Savings**: ~3s on cache hit (eliminates 13× GraphQL init + tool resolution)

**Risk**: Medium — stale tools if MCP daemon changes. TTL of 5 minutes is a reasonable trade-off. Can add a cache invalidation endpoint.

#### 2.2 Deduplicate MCP daemon initialization

The MCP daemon engine initializes twice per request:
1. `resolve_mcp_function_list` at 31.5s
2. Handler constructor at 36.3s

This is because `get_ai_agent_handler()` calls `Invoker.resolve_proxied_callable()` which constructs the handler class, which in turn initializes MCP tools again.

Fix: Pass the already-resolved tools to the handler constructor, or cache the MCP daemon client.

**Savings**: ~0.2-0.5s

### Phase 3: Core Engine DB Write Optimization (~1s savings)

#### 3.1 Parallelize pre-streaming DB writes

**File**: `ai_agent_core_engine/handlers/ai_agent.py` — `execute_ask_model()`

Currently sequential:
1. `insert_update_message` (user message)
2. `insert_update_run` (run record)
3. `calculate_num_tokens` (CPU)

Parallelize 1 and 2 with a thread pool:

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=2) as pool:
    msg_future = pool.submit(insert_update_message, info, **msg_kwargs)
    run_future = pool.submit(insert_update_run, info, **run_kwargs)
    user_message = msg_future.result()
    run = run_future.result()
```

**Savings**: ~0.4s (two DB writes in parallel instead of sequential)

#### 3.2 Skip `count` query in `insert_update_decorator`

**File**: `silvaengine_dynamodb_base/decorators.py` — `insert_update_decorator`

The decorator does a `count_funct()` query before every insert to check if the record exists. For pre-created records (where we know it's an insert, not an update), this count query is wasted.

Add a `skip_count_check=True` parameter to bypass the count query when the caller knows it's a new record.

**Savings**: ~0.2s per insert (2 inserts = 0.4s)

### Phase 4: Streaming Pipeline Optimization (~0.5s savings)

#### 4.1 Start LLM call before DB writes complete

Currently the flow is:
1. DB writes (message + run) → 2. `get_ai_agent_handler` → 3. LLM call

If we start the LLM call in parallel with DB writes, the first token arrives sooner:

```python
# Start LLM streaming in a thread immediately
stream_thread = threading.Thread(target=ai_agent_handler.ask_model, ...)
stream_thread.start()

# Do DB writes in parallel (don't block on them)
user_message = insert_update_message(...)  # can be async
run = insert_update_run(...)               # can be async

# Wait for LLM to finish
stream_event.wait(timeout=120)
```

**Savings**: ~1-2s (LLM call starts immediately, DB writes overlap)

**Risk**: High — the LLM call needs `input_messages` which includes the conversation history from `get_input_messages()`. This query must complete before the LLM call starts. But the user message insert and run record insert can be deferred.

#### 4.2 Reduce `asyncio.sleep(0.1)` to `asyncio.sleep(0.05)`

**File**: `silvaengine_gateway/router_builder.py`

The 0.1s sleep is conservative. 50ms should be enough for the event loop to drain pending `ConnectionManager` sends.

**Savings**: 0.05s

## 4. Expected Results

| Phase | Savings | Risk | Effort |
|---|---|---|---|
| 1.1 Cache engine | 0.1-0.2s | Low | 5 lines |
| 1.2 Async pre-create | 0.4s | Medium | 15 lines |
| 2.1 MCP tool cache | 3.0s | Medium | 30 lines |
| 2.2 Dedup MCP init | 0.3s | Low | 10 lines |
| 3.1 Parallel DB writes | 0.4s | Low | 10 lines |
| 3.2 Skip count query | 0.4s | Low | 5 lines |
| 4.1 Start LLM early | 1-2s | High | 20 lines |
| 4.2 Reduce sleep | 0.05s | Low | 1 line |
| **Total** | **5-6s** | | **~100 lines** |

**Baseline**: ~10s → **Target**: ~4-5s to first chunk

## 5. Priority

1. **Phase 1.1** (cache engine) — immediate, zero risk
2. **Phase 2.1** (MCP tool cache) — biggest single win, medium risk
3. **Phase 1.2** (async pre-create) — easy, medium risk
4. **Phase 3.1** (parallel DB writes) — easy, low risk
5. **Phase 2.2** (dedup MCP init) — low risk
6. **Phase 3.2** (skip count query) — requires decorator change
7. **Phase 4.1** (start LLM early) — highest risk, defer
8. **Phase 4.2** (reduce sleep) — trivial

## 6. Verification

After each phase, run:
```bash
# Start gateway
python silvaengine_gateway/tests/run_daemon.py

# Time 5 requests
for i in 1 2 3 4 5; do
    python silvaengine_gateway/tests/call_websocket.py --prompt "Hi" --timeout 120 2>&1 | grep -E 'is_message_end|Elapsed|chunks'
done
```

Measure: time to first chunk, total elapsed, chunk count. Compare against baseline.