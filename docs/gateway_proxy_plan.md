# Gateway-to-Gateway Proxy Development Plan - SilvaEngine Gateway

> Plan status: Implemented (2026-09-25) on `feature/gateway-proxy`, not yet merged. `handler_type: proxy` forwards to another SilvaEngine Gateway instance via a dynamic `GATEWAY_PROXY_TARGETS` JSON map (see §4 for why this superseded the original per-target `settings.yaml` design). All Phase 6 tests pass (`test_proxy_handler.py`, `test_setting_builder.py`); WebSocket proxying and DB-backed per-tenant target routing remain out of scope (§9).

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
| Target resolution | Dynamic `target_id -> base_url` map, held in a single `GATEWAY_PROXY_TARGETS` env var (JSON-encoded), not one `settings.yaml` entry per target | A per-target `settings.yaml` entry (the original design) requires a YAML/code change to add each remote gateway. A single JSON-map env var makes adding, removing, or repointing a target a one-line deploy-time change — no `settings.yaml`, `module_routes/*.yaml`, or Python edits. `ModuleSpec.proxy_targets` accepts this as a single `"{setting:GATEWAY_PROXY_TARGETS}"` reference; a literal per-key dict is still supported for tests/static cases. |
| Allowlist source | Derived from `GATEWAY_PROXY_TARGETS`' own hosts by default; `GATEWAY_PROXY_ALLOWLIST` only needed to *narrow* it | Keeping the allowlist in lockstep with the targets by default avoids a second env var an operator must remember to update alongside every target change — the exact kind of manual-sync friction the dynamic-map decision above is meant to eliminate. |
| Path shape | `/{endpoint_id}/remote/{target_id}/{proxy_path:path}` | Keeps `endpoint_id` flowing through for remote-side tenant partitioning; `/remote/{target_id}/` is a local-only prefix that avoids colliding with any locally-dispatched module route under the same `endpoint_id`. |
| Body handling | Forward raw bytes, not re-parsed/re-serialized JSON | Avoids float/key-order drift and double JSON work; also lets non-JSON bodies pass through unmodified. |

## 5. Configuration Surface

### 5.1 `settings.yaml` additions

```yaml
  # ── Gateway-to-gateway proxy bridge ────────────────────────────────────
  # Dynamic target_id -> base_url map, JSON-encoded. Adding, removing, or
  # repointing a remote gateway is a single env var change.
  GATEWAY_PROXY_TARGETS:
    env: GATEWAY_PROXY_TARGETS
    default: "{}"
    type: json
  # Comma-separated hostnames a resolved target's base URL must match
  # (anti-SSRF guard). Optional — unset derives the allowlist from
  # GATEWAY_PROXY_TARGETS' own hosts, so there's nothing to keep in sync by
  # hand. Set this only to enforce a narrower list than the configured targets.
  GATEWAY_PROXY_ALLOWLIST:
    env: GATEWAY_PROXY_ALLOWLIST
  GATEWAY_PROXY_TIMEOUT:
    env: GATEWAY_PROXY_TIMEOUT
    default: "30"
    type: float
```

`type: json` is a new coercion added to `setting_builder._coerce()` alongside the existing `int`/`float`/`bool` — a generically useful addition, not specific to this feature.

### 5.2 `module_routes/remote_gateway_proxy.yaml` (new file)

```yaml
name: remote_gateway_proxy
package: remote_gateway_proxy
transport: rest
proxy_targets: "{setting:GATEWAY_PROXY_TARGETS}"
proxy_timeout: "{setting:GATEWAY_PROXY_TIMEOUT}"

routes:
  - path: "/{endpoint_id}/remote/{target_id}/{proxy_path:path}"
    handler_type: proxy
    methods: ["GET", "POST", "PUT", "DELETE", "PATCH"]
    auth: true
```

No target-specific lines here — every target lives in the one `GATEWAY_PROXY_TARGETS` env var.

### 5.3 `routes.yaml`

```yaml
modules:
  - !include module_routes/remote_gateway_proxy.yaml
```

### 5.4 `.env`

```
GATEWAY_PROXY_TARGETS={"us":"https://us.example.com","eu":"https://eu.example.com"}
GATEWAY_PROXY_TIMEOUT=30
```

`GATEWAY_PROXY_ALLOWLIST` is intentionally omitted here — it defaults to `us.example.com,eu.example.com`, derived automatically from the hosts in `GATEWAY_PROXY_TARGETS`.

## 6. Implementation Plan

### Phase 1 — Manifest schema (`router_builder.py`)

- `RouteSpec`: allow `handler_type: "proxy"` in the `Literal`/comment set; make `dispatch` optional for it in `_check_dispatch_required` (same treatment as `sse`/`task_status`/`websocket`).
- `ModuleSpec`: add
  - `proxy_targets: Union[Dict[str, str], str] = Field(default_factory=dict)` — either a single `"{setting:GATEWAY_PROXY_TARGETS}"` reference resolving to a whole `target_id -> base_url` map (the dynamic path), or a literal per-key dict (tests/static cases). Per-key values may still use `{setting:KEY}` indirection, same syntax already used by `config_overrides`.
  - `proxy_timeout: Any = 30.0`
- Extend `validate_manifest()` to warn when a `handler_type: proxy` route's module has an empty `proxy_targets`.

### Phase 2 — Setting indirection resolution

- The `{setting:KEY}` resolution currently lives inline inside `init_module_configs()` (`router_builder.py`, `config_overrides` handling). Factor it into a small shared helper (`_resolve_setting_ref(value, setting) -> Any`) so both `config_overrides` and the new `proxy_targets` use the same resolution logic instead of duplicating it. Add `_resolve_timeout(value, setting, default) -> float` on top of it for `proxy_timeout`.
- Add a `json` type to `setting_builder._coerce()` (alongside `int`/`float`/`bool`) so `GATEWAY_PROXY_TARGETS` parses from its JSON-encoded env var into a real dict once, at setting-load time — not per-request.
- At `build_router_from_manifest()` time (not per-request): if `module.proxy_targets` is a string, resolve it via `_resolve_setting_ref` to get the whole map; if it's a dict, resolve each value individually (same helper). Either way, drop entries that resolve to nothing.

### Phase 3 — Proxy handler factory (`router_builder.py`)

Add `_make_proxy_handler(proxy_targets: Dict[str, str], timeout: float, client: httpx.AsyncClient, allowlist: List[str])`:

1. Read `target_id` and `proxy_path` from `request.path_params`.
2. Look up `base_url = proxy_targets.get(target_id)`; `404` if unknown.
3. Parse `base_url`'s host, check against `allowlist`; `502` (not a silent pass-through) if not allowed — this is a deploy-time misconfiguration, not a client error, so it should be loud in logs.
4. Build the upstream URL: `f"{base_url}/{request.path_params['endpoint_id']}/{proxy_path}"` plus the original query string.
5. Copy headers, dropping hop-by-hop ones (`host`, `content-length`, `connection`) — everything else, including `Authorization` and `Part-Id`, passes through unchanged.
6. Read the raw request body via `await request.body()` (bytes, not re-parsed JSON).
7. Issue the request via the shared `httpx.AsyncClient` (`client.build_request(..., timeout=...)` + `client.send(request, stream=True)` — `timeout` is a `build_request` kwarg, not a `send` kwarg, in this httpx version), with `timeout` from the module config.
8. Return a `StreamingResponse` (or `Response` for small bodies) with the upstream status code, upstream `content-type`, and streamed body — so this also covers proxying an SSE stream through unmodified.
9. Map connection failures / timeouts to `502`/`504` with a small JSON error body (mirrors the existing dispatch error handling style in `_make_sync_handler`), and log with the same `>> `/`<< ` request-log convention already used there.

### Phase 4 — Shared HTTP client lifecycle (`app.py`)

- In `create_app()`, construct one `httpx.AsyncClient` (connection pooling, `follow_redirects=False` — redirects across gateway instances should be explicit, not silently followed); close it in `lifespan` shutdown, alongside the existing Cognito HTTP client cleanup.
- Pass the client into `build_router_from_manifest()` (new `http_client` parameter) so `_make_proxy_handler` doesn't create a client per request.
- Compute the allowlist once at app-build time: use `GATEWAY_PROXY_ALLOWLIST` if set; otherwise derive it from the hosts of `GATEWAY_PROXY_TARGETS`' resolved base URLs. Pass it through to `build_router_from_manifest()` the same way.

### Phase 5 — Manifest + settings wiring

- Add `module_routes/remote_gateway_proxy.yaml`, the `settings.yaml` entries, and the `!include` line in `routes.yaml` as drafted in §5.
- Update `docs/gateway_setup.md`'s "Route Manifest" section (`Module Fields` / `Route Fields` tables) to document `proxy_targets`, `proxy_timeout`, and `handler_type: proxy`.

### Phase 6 — Tests

- `silvaengine_gateway/tests/test_proxy_handler.py`:
  - `target_id` resolves to the correct base URL and forwarded path, both for a literal dict `proxy_targets` and for the whole-map `"{setting:GATEWAY_PROXY_TARGETS}"` form.
  - Unknown `target_id` → `404`.
  - Host not in the (explicit or derived) allowlist → `502`, request never sent (mock the client and assert it wasn't called).
  - `Authorization` header is forwarded byte-for-byte; hop-by-hop headers (including the local gateway's own `Host`) are stripped.
  - Request body bytes round-trip unmodified (no JSON re-encoding).
  - Upstream timeout → `504`; upstream connection error → `502`.
  - Query string is preserved on the forwarded URL.
  - No `http_client` configured, or `proxy_targets` resolves to nothing usable (unset `{setting:KEY}`, or a `GATEWAY_PROXY_TARGETS` value that isn't a JSON object) → the route is skipped entirely, not registered.
  - Integration-style round trip through `TestClient` using `httpx.MockTransport` as the shared client's transport.
- `silvaengine_gateway/tests/test_setting_builder.py`: the new `json` setting type parses a valid JSON object, falls back to the raw string on invalid JSON, and resolves end-to-end via `_resolve_setting()`.

### Phase 7 — Rollout

- Ship behind the manifest itself: a deployment simply doesn't `!include` `remote_gateway_proxy.yaml` (or leaves `GATEWAY_PROXY_TARGETS` unset, defaulting to `{}`) if it doesn't need this feature — no separate feature flag required, consistent with how other optional modules (e.g. `marketing_engine`) opt in today.
- Document `GATEWAY_PROXY_TARGETS` and the allowlist auto-derivation behavior in `docs/gateway_setup.md` so operators know one env var is enough to add a remote gateway, and know when they'd need `GATEWAY_PROXY_ALLOWLIST` instead.

## 7. File-by-File Change List

| File | Change |
|---|---|
| `silvaengine_gateway/router_builder.py` | `RouteSpec`/`ModuleSpec` fields for `proxy` (`proxy_targets` accepts a whole-map `{setting:KEY}` string or a literal dict); `_make_proxy_handler`; shared `_resolve_setting_ref`/`_resolve_timeout` helpers; wire into `build_router_from_manifest` |
| `silvaengine_gateway/setting_builder.py` | `json` type added to `_coerce()`, used by `GATEWAY_PROXY_TARGETS` |
| `silvaengine_gateway/app.py` | Create/close shared `httpx.AsyncClient`; compute the allowlist (explicit `GATEWAY_PROXY_ALLOWLIST` or derived from `GATEWAY_PROXY_TARGETS` hosts); pass client + allowlist + `setting` into `build_router_from_manifest` |
| `silvaengine_gateway/module_routes/remote_gateway_proxy.yaml` | New — module manifest fragment |
| `silvaengine_gateway/routes.yaml` | `!include module_routes/remote_gateway_proxy.yaml` |
| `silvaengine_gateway/settings.yaml` | `GATEWAY_PROXY_TARGETS` (`type: json`), `GATEWAY_PROXY_ALLOWLIST`, `GATEWAY_PROXY_TIMEOUT` |
| `silvaengine_gateway/tests/test_proxy_handler.py` | New — proxy handler tests |
| `silvaengine_gateway/tests/test_setting_builder.py` | New — `json` setting type tests |
| `docs/gateway_setup.md` | Document the new manifest fields |
| `silvaengine_gateway/tests/.env.example` | Document `GATEWAY_PROXY_TARGETS`, `GATEWAY_PROXY_ALLOWLIST`, `GATEWAY_PROXY_TIMEOUT` |

## 8. Security Considerations

- **SSRF**: `target_id` → `base_url` is a closed, operator-defined map — never derived from client input directly. The allowlist check still runs as defense-in-depth (matching the existing `A2A_PUSH_WEBHOOK_ALLOWLIST` pattern), but since it now defaults to the target hosts themselves, it mainly guards against a malformed/unexpected `GATEWAY_PROXY_TARGETS` value rather than an independently-maintained list going stale. Set `GATEWAY_PROXY_ALLOWLIST` explicitly when a narrower policy than "any configured target" is required.
- **Header leakage**: only hop-by-hop headers are stripped; everything else (including `Authorization`, `Part-Id`, cookies if any) forwards through deliberately. Since the remote target is operator-configured (not client-supplied), this is a trusted forward, not an open redirect.
- **Timeouts**: a per-module `proxy_timeout` bounds how long a stalled remote instance can hold a thread/connection from this gateway's pool.
- **Malformed `GATEWAY_PROXY_TARGETS`**: a value that isn't valid JSON is caught by `_coerce()` (falls back to the raw string, logged); a value that's valid JSON but not an object is caught in `build_router_from_manifest()` (logged, route skipped) — neither crashes router construction.
- **Loop prevention**: nothing today stops a `GATEWAY_PROXY_TARGETS` entry from pointing back at this same gateway, creating an infinite forwarding loop. Out of scope for v1 (operator error), but worth a startup-time warning if a configured `base_url` resolves to this process's own bind address.

## 9. Out of Scope for v1 (Future Work)

- **WebSocket proxying** — needs a bidirectional frame pump (e.g. `websockets` client) layered on `_make_websocket_handler`'s existing auth/connect flow; materially more complex than the HTTP request/response case above and not needed for the initial use case.
- **Dynamic per-tenant target resolution** (e.g. `endpoint_id` → `target_id` via a DB lookup) — `GATEWAY_PROXY_TARGETS` already makes the *set of remote gateways* dynamic (one env var, no deploy-time YAML/code change); what's still static is which `target_id` a given request uses, which the caller supplies explicitly in the path today. A DB-backed `endpoint_id -> target_id` lookup is a separate future step if tenants need to be routed automatically without the caller naming a target.
- **Retries / circuit breaking** on upstream failures — v1 surfaces the failure as `502`/`504` and lets the caller retry.

## 10. Release Gates

- [x] All Phase 6 tests passing (41/41 in `test_proxy_handler.py`, `test_setting_builder.py`, `test_router_builder.py`, `test_app.py`; pre-existing unrelated failures in `test_websocket_manager.py`/`test_mcp_e2e.py` confirmed present on the unmodified baseline too).
- [x] `GATEWAY_PROXY_TARGETS` and the allowlist auto-derivation behavior documented in `docs/gateway_setup.md`.
- [ ] Manual round-trip test: two local gateway instances on different ports, one proxying to the other, confirmed end-to-end for a real module route (e.g. `knowledge_graph_graphql`). Not yet done — everything so far is verified via `TestClient` + `httpx.MockTransport`, not two live processes.
- [x] Confirmed a request without a matching `target_id` returns `404`, and a `base_url` outside the allowlist returns `502` without an outbound call being made (verified via `TestClient`/mock assertions in `test_proxy_handler.py`).
