# Gateway Setup Guide

## Deployment

### Fargate/ECS

The gateway is designed to run as a Fargate/ECS service. Each container runs:
- `python -m silvaengine_gateway`

Environment variables configure auth, core module connections (Neo4j, DynamoDB, LLM), and the route manifest.

### Lambda (Core Only)

For Lambda deployments, use the core `knowledge_graph_engine` package directly:
```python
from knowledge_graph_engine import KnowledgeGraphEngine, deploy
```
Lambda bypasses the gateway entirely — AWS API Gateway routes directly to the Lambda function.

## Authentication

### Local JWT

Set `GATEWAY_AUTH_PROVIDER=local` (default). Users authenticate via `/auth/token`.

### AWS Cognito

Set `GATEWAY_AUTH_PROVIDER=cognito` and configure:
- `COGNITO_USER_POOL_ID`
- `COGNITO_APP_CLIENT_ID`
- `COGNITO_APP_SECRET`
- AWS credentials (`region_name`, `aws_access_key_id`, `aws_secret_access_key`)

## Task State Backend

By default, background task state is in-memory. For multi-replica deployments,
swap in a DynamoDB backend:

```python
from silvaengine_gateway.tasks.backend import set_task_backend, TaskBackend

class DynamoDBTaskBackend(TaskBackend):
    # Implement get/put/delete against a DynamoDB table
    ...

set_task_backend(DynamoDBTaskBackend())
```