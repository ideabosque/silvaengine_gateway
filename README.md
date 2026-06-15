# SilvaEngine Gateway

FastAPI gateway for authenticated, in-process access to installed SilvaEngine
modules. The gateway currently exposes Knowledge Graph Engine (KGE) GraphQL and
background extraction routes through a configurable route manifest.

## Current Features

- Local JWT or AWS Cognito authentication
- Public health check and authenticated user-claims endpoint
- YAML or JSON route manifests with dynamic dispatch imports
- Per-IP in-memory rate limiting
- Thread-pool execution for synchronous dispatch functions
- Pluggable task-state backend, with an in-memory default

## Requirements

- Python 3.10 or later
- The `knowledge_graph_engine` package and its service dependencies
- DynamoDB, Neo4j, and model-provider configuration when exercising KGE routes

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

| Method | Path | Authentication | Purpose |
|---|---|---:|---|
| `GET` | `/health` | No | Service health |
| `POST` | `/auth/token` | No | Obtain a local or Cognito access token |
| `GET` | `/me` | Yes | Return authenticated claims |
| `POST` | `/{endpoint_id}/{part_id}/knowledge_graph_graphql` | Yes | Execute KGE GraphQL |
| `POST` | `/{endpoint_id}/{part_id}/extract` | Yes | Submit KGE extraction |
| `GET` | `/{endpoint_id}/{part_id}/extract/status/{task_id}` | Yes | Poll extraction status |

KGE dispatch routes require a `Part-Id` header. The gateway builds
`partition_key` as `<endpoint_id>#<Part-Id>` and passes it to the core dispatch
function.

## Route Manifest

The gateway loads routes in this order:

1. `GATEWAY_ROUTES_CONFIG_JSON`
2. `GATEWAY_ROUTES_CONFIG_PATH`
3. Packaged `silvaengine_gateway/routes.yaml`
4. The built-in KGE manifest

`GATEWAY_ROUTES_CONFIG_JSON` must contain a JSON array of module objects.

```yaml
modules:
  - name: knowledge_graph_engine
    package: knowledge_graph_engine
    transport: graphql
    routes:
      - path: "/{endpoint_id}/{part_id}/knowledge_graph_graphql"
        handler_type: graphql
        dispatch: "knowledge_graph_engine.main:dispatch_graphql"
        methods: ["POST"]
        auth: true
```

Supported `handler_type` values are `graphql`, `rest`, `background`, and
`task_status`. GraphQL, REST, and background routes require a `dispatch` target
in `package.module:function` or dotted notation.

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
| `GATEWAY_ROUTES_CONFIG_JSON` | empty | Inline JSON module array |
| `GATEWAY_DISPATCH_WORKERS` | `8` | Dispatch thread-pool size |
| `GATEWAY_RATE_LIMIT` | `100` | Requests per client IP and window |
| `GATEWAY_RATE_WINDOW` | `60` | Rate-limit window in seconds |

KGE infrastructure settings are forwarded from the gateway startup settings.
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
