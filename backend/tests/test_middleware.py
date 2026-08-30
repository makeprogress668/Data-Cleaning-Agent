"""Tests for the API security + observability middleware and friendly errors.

Covers P0-3 (API key auth, rate limit), P2-6 (friendly error mapping, no internal
leak) and P2-7 (request-id tracing).
"""

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from data_agent.api.middleware import install_middlewares
from data_agent.utils.errors import UserFacingError, to_user_message


def _build_app() -> FastAPI:
    app = FastAPI()
    install_middlewares(app)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/config")
    def config() -> dict:
        return {"ok": True}

    @app.get("/api/v1/jobs/ping")
    def ping() -> dict:
        return {"pong": True}

    @app.post("/api/v1/jobs/ping")
    def create_ping() -> dict:
        return {"pong": True}

    @app.delete("/api/v1/jobs/ping")
    def delete_ping() -> dict:
        return {"pong": True}

    @app.get("/api/v1/whoami")
    def whoami(request: Request) -> dict:
        return {
            "tenant_id": request.state.tenant_id,
            "role": request.state.tenant_role,
        }

    @app.get("/api/v1/jobs/{job_id}/status")
    def job_status(job_id: str) -> dict:
        return {"job_id": job_id}

    return app


# --- P2-7 request tracing ------------------------------------------------------


def test_request_id_is_echoed_in_response_header() -> None:
    client = TestClient(_build_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


def test_supplied_request_id_is_preserved() -> None:
    client = TestClient(_build_app())
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers.get("X-Request-ID") == "trace-123"


# --- P0-3 API key auth ---------------------------------------------------------


def test_auth_is_open_when_no_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_AGENT_API_KEY", raising=False)
    client = TestClient(_build_app())
    assert client.get("/api/v1/jobs/ping").status_code == 200


def test_protected_route_requires_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_API_KEY", "secret-key")
    client = TestClient(_build_app())

    # Missing key -> 401 with a Chinese, non-leaky message.
    missing = client.get("/api/v1/jobs/ping")
    assert missing.status_code == 401
    assert "访问凭据" in missing.json()["detail"]

    # Wrong key -> 401.
    wrong = client.get("/api/v1/jobs/ping", headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401

    # Correct key -> passes through.
    ok = client.get("/api/v1/jobs/ping", headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200


def test_only_health_is_exempt_from_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_API_KEY", "secret-key")
    client = TestClient(_build_app())
    # Health stays public for load balancers; runtime settings follow API auth.
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/config").status_code == 401
    assert (
        client.get("/api/v1/config", headers={"X-API-Key": "secret-key"}).status_code
        == 200
    )


def test_production_without_api_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_ENV", "production")
    monkeypatch.delenv("DATA_AGENT_API_KEY", raising=False)
    client = TestClient(_build_app())

    assert client.get("/health").status_code == 200
    response = client.get("/api/v1/jobs/ping")
    assert response.status_code == 503
    assert "鉴权" in response.json()["detail"]


def test_oidc_token_resolves_tenant_and_role(monkeypatch: pytest.MonkeyPatch) -> None:
    jwt = pytest.importorskip("jwt")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("DATA_AGENT_OIDC_ISSUER", "https://id.example.test/")
    monkeypatch.setenv("DATA_AGENT_OIDC_AUDIENCE", "data-agent")
    monkeypatch.setenv("DATA_AGENT_OIDC_PUBLIC_KEY", public_key)
    token = jwt.encode(
        {
            "iss": "https://id.example.test/",
            "aud": "data-agent",
            "sub": "user-1",
            "exp": 4_102_444_800,
            "tenant_id": "tenant-oidc",
            "role": "operator",
        },
        private_key,
        algorithm="RS256",
    )
    client = TestClient(_build_app())

    identity = client.get(
        "/api/v1/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert identity.status_code == 200
    assert identity.json() == {"tenant_id": "tenant-oidc", "role": "operator"}
    assert (
        client.delete(
            "/api/v1/jobs/ping",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 403
    )


def test_oidc_rejects_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    jwt = pytest.importorskip("jwt")
    monkeypatch.setenv("DATA_AGENT_OIDC_ISSUER", "https://id.example.test/")
    monkeypatch.setenv("DATA_AGENT_OIDC_AUDIENCE", "data-agent")
    signing_secret = "test-secret-that-is-at-least-32-bytes-long"
    monkeypatch.setenv("DATA_AGENT_OIDC_PUBLIC_KEY", signing_secret)
    monkeypatch.setenv("DATA_AGENT_OIDC_ALGORITHMS", "HS256")
    token = jwt.encode(
        {
            "iss": "https://id.example.test/",
            "aud": "another-api",
            "sub": "user-1",
            "exp": 4_102_444_800,
            "tenant_id": "tenant-oidc",
        },
        signing_secret,
        algorithm="HS256",
    )

    response = TestClient(_build_app()).get(
        "/api/v1/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_oidc_rejects_insecure_remote_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_OIDC_ISSUER", "https://id.example.test/")
    monkeypatch.setenv("DATA_AGENT_OIDC_AUDIENCE", "data-agent")
    monkeypatch.setenv("DATA_AGENT_OIDC_JWKS_URL", "http://id.example.test/jwks.json")

    response = TestClient(_build_app()).get("/api/v1/jobs/ping")
    assert response.status_code == 503
    assert "HTTPS" in response.json()["detail"]


def test_api_key_does_not_hide_partial_oidc_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_KEY", "valid-legacy-key")
    monkeypatch.setenv("DATA_AGENT_OIDC_ISSUER", "https://id.example.test/")

    response = TestClient(_build_app()).get(
        "/api/v1/jobs/ping",
        headers={"X-API-Key": "valid-legacy-key"},
    )
    assert response.status_code == 503
    assert "audience" in response.json()["detail"]


def test_tenant_key_resolves_identity_and_enforces_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATA_AGENT_TENANT_API_KEYS_JSON",
        '{"tenant-a":{"api_key":"key-a","role":"viewer"},'
        '"tenant-b":{"api_key":"key-b","role":"operator"},'
        '"tenant-c":{"api_key":"key-c","role":"admin"}}',
    )
    client = TestClient(_build_app())

    viewer = client.get("/api/v1/whoami", headers={"X-API-Key": "key-a"})
    assert viewer.json() == {"tenant_id": "tenant-a", "role": "viewer"}
    assert client.post("/api/v1/jobs/ping", headers={"X-API-Key": "key-a"}).status_code == 403
    assert client.post("/api/v1/jobs/ping", headers={"X-API-Key": "key-b"}).status_code == 200
    assert client.delete("/api/v1/jobs/ping", headers={"X-API-Key": "key-b"}).status_code == 403
    assert client.delete("/api/v1/jobs/ping", headers={"X-API-Key": "key-c"}).status_code == 200


def test_cross_tenant_job_lookup_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATA_AGENT_TENANT_API_KEYS_JSON",
        '{"tenant-a":"key-a","tenant-b":"key-b"}',
    )
    monkeypatch.setattr(
        "data_agent.api.job_store.read_job_status",
        lambda _job_id: {"tenant_id": "tenant-b"},
    )
    client = TestClient(_build_app())
    job_id = "a" * 32

    hidden = client.get(
        f"/api/v1/jobs/{job_id}/status",
        headers={"X-API-Key": "key-a"},
    )
    owned = client.get(
        f"/api/v1/jobs/{job_id}/status",
        headers={"X-API-Key": "key-b"},
    )
    assert hidden.status_code == 404
    assert owned.status_code == 200


# --- P0-3 rate limiting --------------------------------------------------------


def test_rate_limit_returns_429_when_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("DATA_AGENT_RATE_LIMIT_PER_MINUTE", "3")
    client = TestClient(_build_app())

    # First 3 pass, the 4th within the window is throttled.
    statuses = [client.get("/api/v1/jobs/ping").status_code for _ in range(4)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    throttled = client.get("/api/v1/jobs/ping")
    assert "请求过于频繁" in throttled.json()["detail"]


def test_rate_limit_exempts_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_RATE_LIMIT_PER_MINUTE", "1")
    client = TestClient(_build_app())
    # Health is never throttled regardless of frequency.
    for _ in range(5):
        assert client.get("/health").status_code == 200


# --- P2-6 friendly errors ------------------------------------------------------


def test_user_facing_error_message_is_returned_verbatim() -> None:
    exc = UserFacingError("上传的文件为空，请检查后重试。")
    assert to_user_message(exc) == "上传的文件为空，请检查后重试。"


def test_known_low_level_signatures_map_to_chinese_hints() -> None:
    assert "没有可识别的表格" in to_user_message(
        RuntimeError("No supported Excel/CSV tables were found")
    )
    assert "编码无法识别" in to_user_message(
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "codec can't decode byte")
    )


def test_value_errors_are_treated_as_user_actionable() -> None:
    assert to_user_message(ValueError("job_overrides must be a JSON object")) == (
        "job_overrides must be a JSON object"
    )


def test_unknown_internal_error_is_not_leaked() -> None:
    # An arbitrary internal exception must never reach the user as raw text.
    message = to_user_message(RuntimeError("Traceback: secret internal detail at 0xdeadbeef"))
    assert message == "处理过程中出现了内部错误，请稍后重试或联系管理员。"
    assert "0xdeadbeef" not in message
