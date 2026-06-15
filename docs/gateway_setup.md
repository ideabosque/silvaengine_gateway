# Gateway Setup Guide

## Local Setup

```powershell
python -m pip install -e ".[dev]"
Copy-Item silvaengine_gateway/tests/.env.example silvaengine_gateway/tests/.env
```

Fill in the required authentication and KGE service settings, then run:

```powershell
python -m silvaengine_gateway
```

Do not commit `silvaengine_gateway/tests/.env`; it may contain credentials.

## Local JWT

Local JWT is the default authentication provider.

```text
GATEWAY_AUTH_PROVIDER=local
JWT_SECRET_KEY=<random-secret>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong-password>
```

Request a token with OAuth2 form data:

```powershell
$token = (Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/auth/token `
  -ContentType "application/x-www-form-urlencoded" `
  -Body "username=admin&password=<strong-password>").access_token
```

## AWS Cognito

Set `GATEWAY_AUTH_PROVIDER=cognito` and configure:

- `COGNITO_USER_POOL_ID`
- `COGNITO_APP_CLIENT_ID`
- `COGNITO_APP_SECRET`
- `region_name`

The JWKS URL is derived from the region and user-pool ID unless
`COGNITO_JWKS_URL` is set. Username/password login also requires a configured
Cognito Identity Provider client, which the gateway creates when AWS
credentials are supplied.

## Calling A KGE Route

Protected KGE routes require both a bearer token and `Part-Id`:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/demo/acme/knowledge_graph_graphql `
  -Headers @{
    Authorization = "Bearer $token"
    "Part-Id" = "acme"
  } `
  -ContentType "application/json" `
  -Body '{"query":"{ __schema { queryType { name } } }"}'
```

## Route Configuration

Use `GATEWAY_ROUTES_CONFIG_PATH` for a deployment-owned YAML or JSON file, or
`GATEWAY_ROUTES_CONFIG_JSON` for an inline JSON array. The schema is documented
in the project README and demonstrated by
`silvaengine_gateway/routes.yaml`.

Custom dispatch paths are imported into the gateway process. Only use manifests
controlled by the deployment owner.

## Task State

The default task backend is in memory and supports only a single process. A
shared deployment must provide a durable `TaskBackend` implementation and
install it before requests are served:

```python
from silvaengine_gateway.tasks import TaskBackend, set_task_backend


class DynamoDBTaskBackend(TaskBackend):
    def create(self, task_id, meta):
        ...

    def get(self, task_id):
        ...

    def update(self, task_id, status, **fields):
        ...

    def delete(self, task_id):
        ...


set_task_backend(DynamoDBTaskBackend())
```

## Deployment Notes

The current `python -m silvaengine_gateway` entry point starts one Uvicorn
process. For ECS/Fargate, run one process per container unless a shared task
backend and an external multi-worker launch strategy are configured.

Lambda deployments should use `knowledge_graph_engine` directly; this FastAPI
gateway is intended for long-running HTTP services.
