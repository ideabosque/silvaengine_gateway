"""RateLimitMiddleware: internal loopback hops don't consume the client budget."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from silvaengine_gateway.middleware.rate_limit import (
    InMemoryRateLimitStore,
    RateLimitMiddleware,
)


def _client(client_host: str, exempt_loopback: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=2,
        window_seconds=60,
        store=InMemoryRateLimitStore(),
        exempt_loopback=exempt_loopback,
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app, client=(client_host, 50000))


def _codes(client: TestClient, n: int) -> list[int]:
    return [client.get("/ping").status_code for _ in range(n)]


def test_external_client_is_limited():
    assert _codes(_client("203.0.113.7"), 4) == [200, 200, 429, 429]


def test_loopback_internal_hops_are_exempt_by_default():
    assert _codes(_client("127.0.0.1"), 5) == [200] * 5
    assert _codes(_client("::1"), 5) == [200] * 5


def test_loopback_exemption_can_be_disabled():
    assert _codes(_client("127.0.0.1", exempt_loopback=False), 3) == [200, 200, 429]


def _user_client(authenticated_max_requests=None):
    """App whose stand-in auth middleware sets request.state.user from X-User."""
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=2,
        window_seconds=60,
        store=InMemoryRateLimitStore(),
        authenticated_max_requests=authenticated_max_requests,
    )

    class FakeAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.user = {"sub": user}
            return await call_next(request)

    app.add_middleware(FakeAuth)  # outermost, like FlexJWT before RateLimit

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app, client=("172.23.0.1", 50000))


def test_authenticated_users_get_separate_buckets_behind_one_ip():
    client = _user_client()
    a = [client.get("/ping", headers={"x-user": "alice"}).status_code for _ in range(3)]
    b = [client.get("/ping", headers={"x-user": "bob"}).status_code for _ in range(2)]
    assert a == [200, 200, 429]
    assert b == [200, 200]


def test_authenticated_budget_is_separate_and_larger():
    client = _user_client(authenticated_max_requests=5)
    codes = [client.get("/ping", headers={"x-user": "alice"}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    # anonymous traffic from the same IP keeps the strict per-IP limit
    assert _codes(client, 3) == [200, 200, 429]
