# SilvaEngine Gateway

FastAPI gateway for authenticated, in-process access to installed SilvaEngine
modules. The gateway exposes module GraphQL and REST routes through a
configurable YAML route manifest — adding a new module requires only manifest
changes, zero gateway Python code.

## Current Features

- Local JWT or AWS Cognito authentication
- Public health check and authenticated user-claims endpoint
- YAML or JSON route manifests with dynamic dispatch imports
- **Auto-initialization of module Config classes** from manifest `config_class` declarations
- **Manifest-driven exception handlers** — register domain exceptions → HTTP status codes
- **Manifest-driven lifecycle hooks** — `on_startup` / `on_shutdown` per module
- **Manifest-driven SSE** — `sse_manager` resolves the SSE manager per module
- Per-IP in-memory rate limiting
- Thread-pool execution for synchronous dispatch functions
- Pluggable task-state backend, with an in-memory default

## Registered Modules

| Module | Package | Config Init | Routes |
|---|---|---|---|
| Knowledge Graph Engine | `knowledge_graph_engine` | `dict` | GraphQL, Extract, Extract Status |
| RFQ Engine | `rfq_engine` | `dict` | GraphQL |
| MCP Daemon Engine | `mcp_daemon_engine` | `dict` | GraphQL, REST, SSE, Background, Cache Admin, Info |

## Requirements

- Python 3.10 or later
- The `knowledge_graph_engine`, `rfq_engine`, and `mcp_daemon_engine` packages
  (the gateway starts gracefully without any module installed — unresolvable
  imports are logged as warnings)
- DynamoDB, Neo4j, and model-provider configuration when exercising KGE routes
- DynamoDB and model-provider configuration when exercising AI RFQ Engine routes

## Install And Run

```powershell
python -m pip install -e ".[dev]"
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "change-me"
$env:JWT_SECRET_KEY = "replace-with-a-random-secret"
python -m silvaengine_gateway
```

The default server listens on `0.0.0.0:8000`.

```powershell
Invoke-RestMethod http://localhost:8000/health
```

## Authentication

`POST /auth/token` accepts OAuth2 form fields (`username` and `password`).
Protected routes require `Authorization: Bearer <token>`.

Local authentication supports either:

- `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- `LOCAL_USER_FILE`, containing a JSON array of users with `username`,
  `hashed_password`, and optional `roles`

The default `JWT_SECRET_KEY=CHANGEME` is for development only.

## Default Routes

Paths carry only `{ep}` (endpoint_id). The tenant partition id is sent in the
**`Part-Id` request header**; the gateway builds `partition_key = "{ep}#{Part-Id}"`.

| Method | Path | Auth | Purpose |
|---|---|:---:|---|
| `GET` | `/health` | No | Service health |
| `POST` | `/auth/token` | No | Obtain a local or Cognito access token |
| `GET` | `/me` | Yes | Return authenticated claims |
| `POST` | `/{ep}/knowledge_graph_graphql` | Yes | KGE GraphQL |
| `POST` | `/{ep}/extract` | Yes | KGE extraction |
| `GET` | `/{ep}/extract/status/{task_id}` | Yes | Poll extraction status |
| `POST` | `/{ep}/rfq_graphql` | Yes | RFQ Engine GraphQL |
| `POST` | `/{ep}/mcp_daemon_graphql` | Yes | MCP Daemon GraphQL |
| `POST` | `/{ep}/mcp` | Yes | MCP JSON-RPC |
| `GET` | `/{ep}/sse` | Yes | MCP SSE stream |
| `POST` | `/{ep}/sse` | Yes | MCP SSE message |
| `POST` | `/{ep}/mcp_async_execute` | Yes | MCP async tool execution |
| `GET` | `/{ep}/mcp_async/status/{task_id}` | Yes | MCP task status |
| `POST` | `/{ep}/admin/cache/refresh` | Yes | MCP cache refresh |
| `DELETE` | `/{ep}/admin/cache` | Yes | MCP cache clear |
| `GET` | `/{ep}/mcp_info` | Yes | MCP endpoint info |

> All tenant-scoped requests require a `Part-Id: <part_id>` header.

Module dispatch routes build `partition_key` as `<endpoint_id>#<part_id>`, where
`endpoint_id` comes from the route path and `part_id` from the `Part-Id` header.

## Route Manifest

The gateway loads routes in this order:

1. `GATEWAY_ROUTES_CONFIG_PATH` (path to a YAML/JSON manifest file)
2. Packaged `silvaengine_gateway/routes.yaml`
3. The built-in KGE manifest

### Module Specification

Each module entry in the manifest declares:

```yaml
modules:
  - name: my_module
    package: my_module                  # Python package name
    transport: graphql                  # "graphql" | "rest" | "hybrid"
    config_class: "my_module.handlers.config:Config"  # auto-init at startup
    config_init_style: dict             # "dict" → Config.initialize(logger, setting)
    config_exclude_keys: [...]          # optional override
    on_shutdown: "my_module.handlers.lifecycle:cleanup"  # async or sync
    sse_manager: "my_module.handlers.sse_manager:sse_manager"
    exception_handlers:
      - exception_class: "my_module.exceptions:AuthenticationError"
        status_code: 401
    routes:
      - path: "/{endpoint_id}/my_graphql"  # part_id comes from the Part-Id header
        handler_type: graphql
        dispatch: "my_module.main:dispatch_graphql"
        methods: ["POST"]
        auth: true
```

**Key fields:**

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Module identifier (used for logging) |
| `package` | Yes | Python package to import |
| `transport` | No | `graphql` (default), `rest`, or `hybrid` |
| `config_class` | No | Dotted path to Config class. If set, `Config.initialize()` is called at startup. |
| `config_init_style` | No | `dict` (default) → `Config.initialize(logger, setting_dict)`. Use `kwargs` for `Config.initialize(logger, **setting_dict)`. |
| `config_exclude_keys` | No | Gateway-only keys to strip before passing to Config |
| `on_shutdown` | No | Dotted path to async/sync cleanup function, called during FastAPI lifespan shutdown |
| `sse_manager` | No | Dotted path to SSE manager instance for `handler_type: sse` routes |
| `exception_handlers` | No | List of `{exception_class, status_code}` pairs. Gateway registers FastAPI exception handlers for each. |
| `routes` | Yes | List of route specifications |

**Route fields:**

| Field | Required | Description |
|---|---|---|
| `path` | Yes | URL path template |
| `handler_type` | No | `graphql` (default), `rest`, `background`, `task_status`, or `sse` |
| `dispatch` | Yes* | Dotted path to dispatch function. Required for `graphql`, `rest`, `background`. |
| `methods` | No | HTTP methods (default: `["POST"]`) |
| `auth` | No | Require authentication (default: `true`) |

\* `task_status` and `sse` routes use built-in handlers and do not need `dispatch`.

### Adding a New Module

To add a new module, edit `routes.yaml` (or provide a custom manifest via env
vars). **No gateway Python code changes are needed.** The module must provide:

1. **Config class** with `initialize(logger, setting)` or `initialize(logger, **setting)`, matching the module's `config_init_style`
2. **Dispatch functions** (e.g. `dispatch_graphql(**params)`) that execute the
   module's business logic and return JSON-serializable results
3. **Domain exceptions** (optional) — registered via `exception_handlers`
4. **Lifecycle hooks** (optional) — `on_shutdown` for cleanup (e.g. SSE manager)
5. **SSE manager** (optional) — for `handler_type: sse` routes

### Config Auto-Initialization

When a module declares `config_class`, the gateway:

1. Resolves the class via `importlib` at startup
2. Filters the gateway setting dict (removing `config_exclude_keys`)
3. Calls `Config.initialize(logger, setting)` (or `Config.initialize(logger, **setting)` if `config_init_style: kwargs`)

This replaces the need for any hard-coded `_init_xxx_config()` function in
`app.py`. The default `config_exclude_keys` strips gateway-specific auth,
server, and routing keys so only infrastructure/service settings reach the
module Config.

### Domain Exception Handlers

Modules can declare domain exception classes that the gateway maps to HTTP
status codes. Instead of hard-coding `from mcp_daemon_engine.utils.exceptions import
...` in `app.py`, the manifest declares:

```yaml
exception_handlers:
  - exception_class: "mcp_daemon_engine.utils.exceptions:AuthenticationError"
    status_code: 401
```

The gateway resolves each class via `importlib` and registers a FastAPI
exception handler. If the module isn't installed, the handler is skipped with a
warning.

### Lifecycle Hooks

- `on_shutdown`: Called during FastAPI lifespan shutdown. Can be async or sync.
  Used for cleanup (e.g. `sse_manager.cleanup_all()`).

### Cross-Module Function Routing (`functs_on_local`)

The `functs_on_local` setting for cross-module calls is now built
**automatically from the manifest**. Each module with a `config_class` and a
`graphql` route gets an entry. No hard-coded module names in `app.py`.

Env var overrides: `FUNCTS_{NAME}_CLASS` overrides the class name for module
`name`. `FUNCTS_ON_LOCAL_OVERRIDES` (JSON) adds/replaces entries.

Manifest dispatch targets are imported at startup. Treat custom manifests as
trusted deployment configuration; import-prefix allowlisting is not yet
implemented.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_AUTH_PROVIDER` | `local` | `local` or `cognito` |
| `JWT_SECRET_KEY` | `CHANGEME` | Local JWT signing secret |
| `JWT_ALGORITHM` | `HS256` | Local JWT algorithm |
| `ACCESS_TOKEN_EXP` | `15` | Token lifetime in minutes |
| `ADMIN_USERNAME` | empty | Local administrator username |
| `ADMIN_PASSWORD` | empty | Local administrator password |
| `ADMIN_STATIC_TOKEN` | empty | Optional permanent administrator token |
| `LOCAL_USER_FILE` | empty | Path to local users JSON |
| `COGNITO_USER_POOL_ID` | empty | Cognito user-pool ID |
| `COGNITO_APP_CLIENT_ID` | empty | Cognito application client ID |
| `COGNITO_APP_SECRET` | empty | Cognito application client secret |
| `COGNITO_JWKS_URL` | derived | Optional JWKS override |
| `JWKS_CACHE_TTL` | `3600` | Cognito JWKS cache lifetime in seconds |
| `GATEWAY_HOST` | `0.0.0.0` | Bind address |
| `GATEWAY_PORT` | `8000` | Bind port |
| `GATEWAY_ROUTES_CONFIG_PATH` | packaged file | YAML or JSON manifest path |
| `GATEWAY_DISPATCH_WORKERS` | `8` | Dispatch thread-pool size |
| `GATEWAY_RATE_LIMIT` | `100` | Requests per client IP and window |
| `GATEWAY_RATE_WINDOW` | `60` | Rate-limit window in seconds |

Infrastructure settings (AWS, Neo4j, LLM, etc.) are forwarded from the gateway
startup settings to module Config classes via the `config_class` mechanism.
See [`silvaengine_gateway/tests/.env.example`](silvaengine_gateway/tests/.env.example)
for the currently supported development variables.

## Background Tasks

The default `InMemoryTaskBackend` is process-local. Completed and failed task
records are deleted after the first successful status read.

Multi-process or multi-replica deployments must install a shared backend:

```python
from silvaengine_gateway.tasks import TaskBackend, set_task_backend

class SharedTaskBackend(TaskBackend):
    ...

set_task_backend(SharedTaskBackend())
```

## Development

```powershell
python -m pip install -e ".[dev]"
pytest -q
```

Tests marked `integration` exercise the full gateway/KGE route path. External
service behavior depends on the local environment and credentials:

```powershell
$env:RUN_GATEWAY_INTEGRATION = "1"
pytest -q -m integration
```

## Known Gaps

- Manifest import targets are not yet restricted by an allowlist.
- The default task backend does not survive process restart or support replicas.
- Task records do not currently enforce tenant ownership.
- `GATEWAY_WORKERS` is retained in configuration, but the module entry point
  currently starts one Uvicorn process.

## License

MIT
