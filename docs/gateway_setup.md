# Gateway Setup Guide

## Quick Start

```powershell
# Install the gateway
python -m pip install -e ".[dev]"

# Configure authentication (minimum required)
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "change-me"
$env:JWT_SECRET_KEY = "replace-with-a-random-secret"

# Start the gateway
python -m silvaengine_gateway
```

The default server listens on `0.0.0.0:8000`.

```powershell
# Verify it's running
Invoke-RestMethod http://localhost:8000/health
```

## Environment Variables

See the main [README.md](../README.md) for the full configuration reference.

Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_AUTH_PROVIDER` | `local` | `local` or `cognito` |
| `JWT_SECRET_KEY` | `CHANGEME` | Local JWT signing secret |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | empty | Local admin credentials |
| `GATEWAY_PORT` | `8000` | Bind port |
| `GATEWAY_ROUTES_CONFIG_PATH` | packaged `routes.yaml` | Custom manifest path |
| `GATEWAY_WORKERS` | `1` | Uvicorn worker processes |
| `GATEWAY_TASK_BACKEND` | `memory` | `memory` or `dynamodb` (background task state) |
| `GATEWAY_TASK_TABLE` | `silvaengine-gateway-tasks` | DynamoDB task table (hash key `task_id`) |
| `GATEWAY_TASK_TTL` | `3600` | Task state TTL in seconds |
| `GATEWAY_RATE_LIMIT_BACKEND` | `memory` | `memory` or `dynamodb` (rate-limit counters) |
| `GATEWAY_RATE_LIMIT_TABLE` | `silvaengine-gateway-ratelimit` | DynamoDB table (hash key `rl_key`) |

## Scaling & Multi-Process

By default the gateway runs a **single** uvicorn process and keeps task state,
rate-limit counters, and the SSE client registry in memory. Set
`GATEWAY_WORKERS > 1` to run multiple worker processes — but in-memory state is
**not shared** across processes, so switch the affected stores to a shared
backend:

| Concern | Single process | `workers > 1` |
|---|---|---|
| Background task status | in-memory (default) | set `GATEWAY_TASK_BACKEND=dynamodb` so a poll on any worker sees the result |
| Rate limiting | in-memory (per-process; effective limit × workers) | set `GATEWAY_RATE_LIMIT_BACKEND=dynamodb` for a shared fixed-window counter |
| SSE streaming | in-memory registry (works) | requires **sticky sessions** so a client's `GET /sse` stream and its `POST /sse` land on the same worker; cross-user broadcast across workers needs a pub/sub backplane (e.g. Redis) — not provided |

The DynamoDB tables need:

- **tasks**: string hash key `task_id`; enable TTL on the `expires_at` attribute.
- **rate limit**: string hash key `rl_key`; enable TTL on the `expires_at` attribute.

AWS credentials are taken from the standard `region_name` /
`aws_access_key_id` / `aws_secret_access_key` settings. On startup the gateway
logs the selected backends and warns when `workers > 1` is combined with an
in-memory store.

## Route Manifest

The gateway reads `routes.yaml` (or a JSON override) to discover modules,
dispatch routes, and configuration. **Adding a module = editing routes.yaml,
zero Python changes.**

### Module Fields

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Module identifier |
| `package` | Yes | Python package to import |
| `transport` | No | `graphql` (default), `rest`, or `hybrid` |
| `config_class` | No | `"pkg.module:Config"` — auto-initialized at startup |
| `config_init_style` | No | `dict` (default) or `kwargs` |
| `config_exclude_keys` | No | Gateway-only keys to strip (defaults apply) |
| `on_shutdown` | No | `"pkg.module:cleanup_fn"` — called on shutdown |
| `sse_manager` | No | `"pkg.module:sse_manager"` — for `handler_type: sse` |
| `exception_handlers` | No | List of `{exception_class, status_code}` |

### Route Fields

| Field | Required | Description |
|---|---|---|
| `path` | Yes | URL path template |
| `handler_type` | No | `graphql`, `rest`, `background`, `task_status`, `sse` |
| `dispatch` | Yes* | `"pkg.module:function"` — required except for `task_status` and `sse` |
| `methods` | No | HTTP methods (default: `["POST"]`) |
| `auth` | No | Require auth (default: `true`) |

### Adding a New Module

1. Add a module entry to `routes.yaml`
2. The module must provide:
   - **Config class** with `initialize(logger, setting)` or `initialize(logger, **setting)`, matching the module's `config_init_style`
   - **Dispatch functions** (e.g. `dispatch_graphql(**params)`)
   - **Domain exceptions** (optional) — registered via `exception_handlers`
   - **Lifecycle hooks** (optional) — `on_shutdown` for cleanup
   - **SSE manager** (optional) — for `handler_type: sse`

Example:

```yaml
  - name: my_new_module
    package: my_new_module
    transport: graphql
    config_class: "my_new_module.handlers.config:Config"
    config_init_style: dict
    on_shutdown: "my_new_module.handlers.lifecycle:cleanup"
    exception_handlers:
      - exception_class: "my_new_module.exceptions:AuthenticationError"
        status_code: 401
    routes:
      - path: "/{endpoint_id}/my_graphql"  # part_id comes from the Part-Id header
        handler_type: graphql
        dispatch: "my_new_module.main:dispatch_graphql"
        methods: ["POST"]
        auth: true
```

### Config Auto-Initialization

When `config_class` is set, the gateway:

1. Resolves the class via `importlib`
2. Strips `config_exclude_keys` from the settings
3. Calls `Config.initialize(logger, setting)` (or `Config.initialize(logger, **setting)` if `config_init_style: kwargs`)

No hard-coded `_init_xxx_config()` functions needed in `app.py`.

### Domain Exception Handlers

Modules register domain exception → HTTP status code mappings in the manifest:

```yaml
exception_handlers:
  - exception_class: "mcp_daemon_engine.utils.exceptions:AuthenticationError"
    status_code: 401
  - exception_class: "mcp_daemon_engine.utils.exceptions:InvalidRequestError"
    status_code: 400
```

The gateway resolves each class and registers a FastAPI exception handler.
Modules not installed are skipped with a warning.

### Lifecycle Hooks

- **`on_shutdown`**: Async or sync function called during FastAPI lifespan shutdown.
  Used for cleanup (e.g. `sse_manager.cleanup_all()`).

### Cross-Module Function Routing

`functs_on_local` is built automatically from the manifest — each module with a
`config_class` and a `graphql` route gets an entry. Env var overrides:
`FUNCTS_{NAME}_CLASS` for class name, `FUNCTS_ON_LOCAL_OVERRIDES` (JSON) for
additions.

## Test Scripts

| Script | Purpose |
|---|---|
| `gen_token.py` | Generate a JWT from `.env` (admin/user/custom; reuses gateway auth) |
| `call_search.py` | Test KGE search (text2cypher, vector, hybrid) |
| `call_inquire_catalog.py` | Test AI RFQ Engine inquireCatalog |
| `call_mcp_graphql.py` | Test MCP Daemon GraphQL |
| `call_mcp_rest.py` | Test MCP JSON-RPC REST |
| `call_mcp_sse.py` | Test SSE stream + message posting |

## MCP Daemon Engine Integration

The MCP Daemon Engine is fully integrated through the manifest:

- **9 routes** (GraphQL, REST, SSE, background, cache admin, info)
- **Domain exceptions** mapped to HTTP 401/400/429
- **SSE manager** resolved from `mcp_daemon_engine.handlers.sse_manager:sse_manager`
- **Shutdown hook** calls `cleanup_sse()` to disconnect all SSE clients
- **Config init** uses `dict` style: `Config.initialize(logger, setting)`
