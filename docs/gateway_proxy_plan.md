# Gateway-to-Gateway Proxy Development Plan - SilvaEngine Gateway

> Plan status: Design only — not yet implemented. No code in this repository currently forwards a request to another gateway instance over HTTP; this document specifies what to build.

## 1. Purpose

Today every route in the manifest dispatches to an **in-process Python callable**: `router_builder.resolve_dispatch()` imports `package.module:function` via `importlib` and calls it directly in the shared thread pool (`silvaengine_gateway/router_builder.py`). There is no way to point a route at another *running* SilvaEngine Gateway instance over HTTP.

The goal is a new `handler_type: proxy` that reverse-proxies an inbound request to a remote SilvaEngine Gateway instance, so one gateway deployment can front several others — e.g. a regional edge gateway forwarding to per-region backend gateways, or a gateway forwarding a subset of routes to a partner-operated instance — without duplicating module registrations locally.

## 2. Current Baseline

Verified in this repository:

- `silvaengine_gateway/router_builder.py::resolve_dispatch()` only resolves in-process callables; there is no HTTP client / forwarding code path.
- `handler_type` is already a pluggable dispatch: `_make_sync_handler`, `_make_background_handler`, `_make_sse_handler`, `_make_websocket_handler`, and `_make_task_status_handler` are all selected in `build_router_from_manifest()` (`router_builder.py`).
- `httpx>=0.25.0` is already a declared dependency (`pyproject.toml`), unused directly by the gateway today.
- A narrower precedent exists one layer down: `CORE_ENGINE_GRAPHQL_URL` / `CORE_ENGINE_WS_URL` / `CORE_ENGINE_TOKEN` (`silvaengine_gateway/settings.yaml`, "Core Engine gateway bridge") are forwarded to the external `a2a_daemon_engine` package, which uses them to call out to another SilvaEngine instance for a single A2A agent type. That logic is internal to `a2a_daemon_engine`, not a generic gateway-level route proxy, and is not reusable for arbitrary modules/routes.
- The route manifest is loaded via `!include` splicing (`silvaengine_gateway/manifest.py`), and settings resolve from `settings.yaml` env-var declarations (`silvaengine_gateway/setting_builder.py`) — both patterns this feature reuses rather than replaces.

## 3. Target Architecture

```text
Client
  |
  |  POST /{endpoint_id}/remote/{target_id}/{proxy_path}
  v
This Gateway (FastAPI)
  - FlexJWTMiddleware / get_current_user   (auth: true, same as any route)
  - handler_type: proxy
      -> resolve target_id -> base_url via proxy_targets map
      -> check base_url host against GATEWAY_PROXY_ALLOWLIST
      -> forward via shared httpx.AsyncClient:
           method, headers (Authorization passed through as-is),
           query string, raw body bytes
      -> stream the upstream response back (status, headers, body)
  |
  |  POST {base_url}/{endpoint_id}/{proxy_path}
  v
Remote SilvaEngine Gateway instance
  - handles auth, partitioning (endpoint_id/Part-Id), and dispatch itself
```

Key property: the remote instance sees a request indistinguishable from a direct client call — same path shape (`/{endpoint_id}/...`), same headers, same body. Nothing on the remote side needs to know it arrived via a proxy.

`target_id` and `endpoint_id` are independent concerns:

- `target_id` — a routing key local to *this* gateway, selects which remote instance receives the request. Never forwarded.
- `endpoint_id` — forwarded unchanged; the remote gateway does its own tenant partitioning with it (as it would for a direct call).

## 4. Design Decisions

Settled during design discussion (2026-09-25):

| Decision | Choice | Reason |
|---|---|---|
| Auth to remote gateway | Forward the caller's own `Authorization` header as-is | Both instances trust the same identity provider; no separate service credential to provision/rotate. `auth: true` already validates the token locally before it's forwarded. |
| Target resolution | Static `proxy_targets` map (`target_id -> base_url`) in the module manifest, values sourced from `settings.yaml`/env | Matches this repo's "add config, not code" convention (see `settings.yaml` header comment: "Adding a setting = adding an entry here. No Python changes."). Avoids building a DB-backed dynamic lookup for a need that's currently static. |
| Path shape | `/{endpoint_id}/remote/{target_id}/{proxy_path:path}` | Keeps `endpoint_id` flowing through for remote-side tenant partitioning; `/remote/{target_id}/` is a local-only prefix that avoids colliding with any locally-dispatched module route under the same `endpoint_id`. |
| Body handling | Forward raw bytes, not re-parsed/re-serialized JSON | Avoids float/key-order drift and double JSON work; also lets non-JSON bodies pass through unmodified. |

## 5. Configuration Surface

### 5.1 `settings.yaml` additions

```yaml
  # ── Gateway-to-gateway proxy bridge ────────────────────────────────────
  REMOTE_GATEWAY_US_URL:
    env: REMOTE_GATEWAY_US_URL
  REMOTE_GATEWAY_EU_URL:
    env: REMOTE_GATEWAY_EU_URL
  GATEWAY_PROXY_ALLOWLIST:
    env: GATEWAY_PROXY_ALLOWLIST        # comma-separated hostnames; required for any proxy_targets entry to be reachable
  GATEWAY_PROXY_TIMEOUT:
    env: GATEWAY_PROXY_TIMEOUT
    default: "30"
    type: float
```

Each new remote target is one more `env:` entry here plus one more map line below — no Python changes.

### 5.2 `module_routes/remote_gateway_proxy.yaml` (new file)

```yaml
name: remote_gateway_proxy
package: remote_gateway_proxy
transport: rest
proxy_targets:
  us: "{setting:REMOTE_GATEWAY_US_URL}"
  eu: "{setting:REMOTE_GATEWAY_EU_URL}"
proxy_timeout: "{setting:GATEWAY_PROXY_TIMEOUT}"

routes:
  - path: "/{endpoint_id}/remote/{target_id}/{proxy_path:path}"
    handler_type: proxy
    methods: ["GET", "POST", "PUT", "DELETE", "PATCH"]
    auth: true
```

### 5.3 `routes.yaml`

```yaml
modules:
  - !include module_routes/remote_gateway_proxy.yaml
```

### 5.4 `.env`

```
REMOTE_GATEWAY_US_URL=https://us.example.com
REMOTE_GATEWAY_EU_URL=https://eu.example.com
GATEWAY_PROXY_ALLOWLIST=us.example.com,eu.example.com
GATEWAY_PROXY_TIMEOUT=30
```

## 6. Implementation Plan

### Phase 1 — Manifest schema (`router_builder.py`)

- `RouteSpec`: allow `handler_type: "proxy"` in the `Literal`/comment set; make `dispatch` optional for it in `_check_dispatch_required` (same treatment as `sse`/`task_status`/`websocket`).
- `ModuleSpec`: add
  - `proxy_targets: Dict[str, str] = Field(default_factory=dict)` — supports the existing `{setting:KEY}` indirection syntax already used by `config_overrides`.
  - `proxy_timeout: float = 30.0`
- Extend `validate_manifest()` to warn when a `handler_type: proxy` route's module has an empty `proxy_targets` map.

### Phase 2 — Setting indirection resolution

- The `{setting:KEY}` resolution currently lives inline inside `init_module_configs()` (`router_builder.py`, `config_overrides` handling). Factor it into a small shared helper (e.g. `_resolve_setting_ref(value, setting) -> Any`) so both `config_overrides` and the new `proxy_targets` map use the same resolution logic instead of duplicating it.
- Resolve `proxy_targets` and `proxy_timeout` once per module at `build_router_from_manifest()` time (not per-request).

### Phase 3 — Proxy handler factory (`router_builder.py`)

Add `_make_proxy_handler(proxy_targets: Dict[str, str], timeout: float, client: httpx.AsyncClient, allowlist: List[str])`:

1. Read `target_id` and `proxy_path` from `request.path_params`.
2. Look up `base_url = proxy_targets.get(target_id)`; `404` if unknown.
3. Parse `base_url`'s host, check against `allowlist`; `502` (not a silent pass-through) if not allowed — this is a deploy-time misconfiguration, not a client error, so it should be loud in logs.
4. Build the upstream URL: `f"{base_url}/{request.path_params['endpoint_id']}/{proxy_path}"` plus the original query string.
5. Copy headers, dropping hop-by-hop ones (`host`, `content-length`, `connection`) — everything else, including `Authorization` and `Part-Id`, passes through unchanged.
6. Read the raw request body via `await request.body()` (bytes, not re-parsed JSON).
7. Issue the request via the shared `httpx.AsyncClient` (`client.request(method, url, headers=..., content=..., params=...)`), with `timeout` from the module config.
8. Return a `StreamingResponse` (or `Response` for small bodies) with the upstream status code, upstream `content-type`, and streamed body — so this also covers proxying an SSE stream through unmodified.
9. Map connection failures / timeouts to `502`/`504` with a small JSON error body (mirrors the existing dispatch error handling style in `_make_sync_handler`), and log with the same `>> `/`<< ` request-log convention already used there.

### Phase 4 — Shared HTTP client lifecycle (`app.py`)

- In `create_app()`'s `lifespan`, construct one `httpx.AsyncClient` (connection pooling, `follow_redirects=False` — redirects across gateway instances should be explicit, not silently followed) and close it on shutdown, alongside the existing Cognito HTTP client cleanup.
- Pass the client into `build_router_from_manifest()` (new `http_client` parameter) so `_make_proxy_handler` doesn't create a client per request.
- Read `GATEWAY_PROXY_ALLOWLIST` from `setting` once at app build time and pass it through the same way.

### Phase 5 — Manifest + settings wiring

- Add `module_routes/remote_gateway_proxy.yaml`, the `settings.yaml` entries, and the `!include` line in `routes.yaml` as drafted in §5.
- Update `docs/gateway_setup.md`'s "Route Manifest" section (`Module Fields` / `Route Fields` tables) to document `proxy_targets`, `proxy_timeout`, and `handler_type: proxy`.

### Phase 6 — Tests

- `silvaengine_gateway/tests/test_router_builder.py` (or a new `test_proxy_handler.py`):
  - `target_id` resolves to the correct base URL and forwarded path.
  - Unknown `target_id` → `404`.
  - Host not in `GATEWAY_PROXY_ALLOWLIST` → `502`, request never sent (mock the client and assert it wasn't called).
  - `Authorization` header is forwarded byte-for-byte; hop-by-hop headers are stripped.
  - Request body bytes round-trip unmodified (no JSON re-encoding).
  - Upstream timeout → `504`; upstream connection error → `502`.
  - Query string is preserved on the forwarded URL.
- An integration-style test using `httpx.MockTransport` (or a local FastAPI test app playing the role of "remote gateway") to exercise a full round trip through `TestClient`.

### Phase 7 — Rollout

- Ship behind the manifest itself: a deployment simply doesn't `!include` `remote_gateway_proxy.yaml` (or defines an empty `proxy_targets`) if it doesn't need this feature — no separate feature flag required, consistent with how other optional modules (e.g. `marketing_engine`) opt in today.
- Document `GATEWAY_PROXY_ALLOWLIST` as a required companion to any `proxy_targets` entry in `docs/gateway_setup.md` so it isn't deployed without the SSRF guard.

## 7. File-by-File Change List

| File | Change |
|---|---|
| `silvaengine_gateway/router_builder.py` | `RouteSpec`/`ModuleSpec` fields for `proxy`; `_make_proxy_handler`; shared `{setting:KEY}` resolution helper; wire into `build_router_from_manifest` |
| `silvaengine_gateway/app.py` | Create/close shared `httpx.AsyncClient` in `lifespan`; pass client + allowlist into `build_router_from_manifest` |
| `silvaengine_gateway/module_routes/remote_gateway_proxy.yaml` | New — module manifest fragment |
| `silvaengine_gateway/routes.yaml` | `!include module_routes/remote_gateway_proxy.yaml` |
| `silvaengine_gateway/settings.yaml` | `REMOTE_GATEWAY_*_URL`, `GATEWAY_PROXY_ALLOWLIST`, `GATEWAY_PROXY_TIMEOUT` |
| `silvaengine_gateway/tests/` | New proxy handler tests |
| `docs/gateway_setup.md` | Document the new manifest fields |
| `.env.example` (if present) | Document the new env vars |

## 8. Security Considerations

- **SSRF**: `target_id` → `base_url` is a closed, operator-defined map — never derived from client input directly — but `GATEWAY_PROXY_ALLOWLIST` is still enforced defense-in-depth, matching the existing `A2A_PUSH_WEBHOOK_ALLOWLIST` pattern for outbound URLs.
- **Header leakage**: only hop-by-hop headers are stripped; everything else (including `Authorization`, `Part-Id`, cookies if any) forwards through deliberately. Since the remote target is operator-configured (not client-supplied), this is a trusted forward, not an open redirect.
- **Timeouts**: a per-module `proxy_timeout` bounds how long a stalled remote instance can hold a thread/connection from this gateway's pool.
- **Loop prevention**: nothing today stops `REMOTE_GATEWAY_*_URL` from pointing back at this same gateway, creating an infinite forwarding loop. Out of scope for v1 (operator error), but worth a startup-time warning if a configured `base_url` resolves to this process's own bind address.

## 9. Out of Scope for v1 (Future Work)

- **WebSocket proxying** — needs a bidirectional frame pump (e.g. `websockets` client) layered on `_make_websocket_handler`'s existing auth/connect flow; materially more complex than the HTTP request/response case above and not needed for the initial use case.
- **Dynamic per-tenant target resolution** (e.g. `endpoint_id` → `target_id` via a DB lookup) — deferred in favor of the static `proxy_targets` map; revisit if the number of tenants/targets grows past what's comfortable to hand-maintain in YAML.
- **Retries / circuit breaking** on upstream failures — v1 surfaces the failure as `502`/`504` and lets the caller retry.

## 10. Release Gates

- [ ] All Phase 6 tests passing.
- [ ] `GATEWAY_PROXY_ALLOWLIST` documented as required in `docs/gateway_setup.md`.
- [ ] Manual round-trip test: two local gateway instances on different ports, one proxying to the other, confirmed end-to-end for a real module route (e.g. `knowledge_graph_graphql`).
- [ ] Confirmed a request without a matching `target_id` returns `404`, and a `base_url` outside the allowlist returns `502` without an outbound call being made (verified via mock/log inspection, not just code review).
