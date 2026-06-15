# SilvaEngine Gateway

FastAPI gateway for SilvaEngine modules with JWT authentication, configurable module routing, and background task management.

## Quick Start

```bash
pip install -e .
python -m silvaengine_gateway
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_AUTH_PROVIDER` | `local` | Auth provider: `local` or `cognito` |
| `JWT_SECRET_KEY` | `CHANGEME` | Secret key for local JWT |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `ACCESS_TOKEN_EXP` | `15` | Token expiry in minutes |
| `ADMIN_USERNAME` | (empty) | Admin username for local auth |
| `ADMIN_PASSWORD` | (empty) | Admin password for local auth |
| `ADMIN_STATIC_TOKEN` | (empty) | Static admin token (bypasses JWT) |
| `LOCAL_USER_FILE` | (empty) | Path to JSON user file |
| `COGNITO_USER_POOL_ID` | (empty) | AWS Cognito User Pool ID |
| `COGNITO_APP_CLIENT_ID` | (empty) | AWS Cognito App Client ID |
| `COGNITO_APP_SECRET` | (empty) | AWS Cognito App Client Secret |
| `COGNITO_JWKS_URL` | (auto) | JWKS endpoint URL |
| `GATEWAY_HOST` | `0.0.0.0` | Server bind address |
| `GATEWAY_PORT` | `8000` | Server bind port |
| `GATEWAY_WORKERS` | `1` | Number of workers |
| `GATEWAY_ROUTES_CONFIG_PATH` | (built-in) | Path to routes.yaml |
| `GATEWAY_ROUTES_CONFIG_JSON` | (empty) | JSON string of route config |

### Route Manifest

The gateway uses a configurable route manifest to register modules. Three sources (in priority order):

1. **`GATEWAY_ROUTES_CONFIG_JSON`** env var — JSON string
2. **`GATEWAY_ROUTES_CONFIG_PATH`** env var — path to YAML/JSON file
3. **Built-in `routes.yaml`** — packaged default (KGE only)

Adding a module requires only a config change:

```yaml
# routes.yaml
modules:
  - name: knowledge_graph_engine
    package: knowledge_graph_engine
    transport: graphql
    routes:
      - path: "/{endpoint_id}/{part_id}/knowledge_graph_graphql"
        adapter: "silvaengine_gateway.adapters.kge:graphql_endpoint"
        methods: ["POST"]
        auth: true

  - name: ai_rfq_engine
    package: ai_rfq_engine
    transport: graphql
    routes:
      - path: "/{endpoint_id}/{part_id}/ai_rfq_graphql"
        adapter: "silvaengine_gateway.adapters.rfq:graphql_endpoint"
        methods: ["POST"]
        auth: true
```

## Architecture

```
silvaengine_gateway/
├── app.py              # FastAPI app factory, lifespan, route manifest loading
├── config.py           # GatewayConfig (auth, server, route manifest settings)
├── router_builder.py   # Dynamic router from manifest (importlib dispatch)
├── resolve_info.py     # ResolveInfo shim for calling core dispatch functions
├── routes.yaml         # Default route manifest
├── adapters/
│   └── kge.py          # KGE HTTP endpoints → core dispatch calls
├── routes/
│   ├── health.py       # /health, /me
│   └── auth.py         # /auth/token
├── auth/
│   ├── jwt_local.py    # Local JWT create/verify
│   ├── jwt_cognito.py  # Cognito JWT verify
│   ├── middleware.py    # FlexJWTMiddleware
│   └── users.py         # LocalUser model, user file loader
└── tasks/
    └── backend.py      # Task state interface (InMemoryTaskBackend)
```

## Adding a New Module

1. Install the module's Python package alongside the gateway
2. Create an adapter in `silvaengine_gateway/adapters/`
3. Add a module entry to `routes.yaml` (or set `GATEWAY_ROUTES_CONFIG_JSON`)
4. Restart the gateway — no code changes needed

## License

MIT