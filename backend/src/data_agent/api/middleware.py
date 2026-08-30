from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections import deque

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from data_agent.authentication import oidc_auth_configured, resolve_oidc_identity
from data_agent.tenancy import (
    configured_tenant_identities,
    reset_current_tenant,
    resolve_tenant_identity,
    role_allows,
    set_current_tenant,
    tenant_owns_payload,
)

logger = logging.getLogger("data_agent.api")

# Health checks never need credentials. Runtime configuration is a normal API
# resource and is protected whenever production authentication is enabled.
_AUTH_EXEMPT_PATHS = frozenset({"/health"})


def production_mode() -> bool:
    return os.environ.get("DATA_AGENT_ENV", "").strip().lower() == "production"


def tenant_auth_configured() -> bool:
    return bool(configured_tenant_identities())


def authentication_configured() -> bool:
    # Evaluate both sides so a valid legacy key cannot hide a broken partial OIDC
    # rollout. Production must fail startup before users receive unexplained 500s.
    key_configured = tenant_auth_configured()
    oidc_configured = oidc_auth_configured()
    return key_configured or oidc_configured


def validate_auth_configuration() -> None:
    """Fail production startup before a healthy but unusable deployment is exposed."""

    configured = authentication_configured()
    if production_mode() and not configured:
        raise RuntimeError("生产环境必须配置 API Key 或 OIDC 鉴权")


def allowed_origins() -> list[str]:
    raw = os.environ.get("DATA_AGENT_CORS_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id to every request, log start/finish, echo it back.

    Gives each request a stable trace id (``X-Request-ID``) that appears in logs
    and in the response header, so a user-reported failure can be found in logs.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "request %s %s %s failed after %.0fms",
                request_id,
                request.method,
                request.url.path,
                elapsed,
            )
            raise
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "request %s %s %s -> %s (%.0fms)",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        response.headers["X-Request-ID"] = request_id
        return response


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require a valid ``X-API-Key`` header when an API key is configured."""

    async def dispatch(self, request: Request, call_next):
        # Let CORSMiddleware answer browser preflight requests. The actual request
        # still needs a tenant key, and preflight never reaches business routes.
        if request.method.upper() == "OPTIONS":
            return await call_next(request)
        protected = request.url.path not in _AUTH_EXEMPT_PATHS
        try:
            auth_configured = authentication_configured()
        except RuntimeError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        if production_mode() and not auth_configured and protected:
            return JSONResponse(
                status_code=503,
                content={"detail": "服务鉴权尚未配置，暂不可用。"},
            )
        if not protected:
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        identity = (
            resolve_oidc_identity(authorization)
            if authorization
            else resolve_tenant_identity(request.headers.get("X-API-Key", ""))
        )
        if identity is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "缺少或无效的访问凭据。"},
            )
        if not role_allows(identity.role, request.method):
            return JSONResponse(
                status_code=403,
                content={"detail": "当前租户角色无权执行该操作。"},
            )
        request.state.tenant_id = identity.tenant_id
        request.state.tenant_role = identity.role
        if not _request_job_is_owned(request.url.path, identity.tenant_id):
            # Deliberately use 404 so one tenant cannot probe another tenant's ids.
            return JSONResponse(status_code=404, content={"detail": "任务不存在"})
        token = set_current_tenant(identity.tenant_id)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-client sliding-window rate limit to blunt abuse / accidental floods.

    In-process and best-effort (per worker); for hard multi-node limits use a
    gateway. Disabled when ``DATA_AGENT_RATE_LIMIT_PER_MINUTE`` <= 0.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._limit = int(os.environ.get("DATA_AGENT_RATE_LIMIT_PER_MINUTE", "600"))
        self._window = 60.0
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = 0.0

    async def dispatch(self, request: Request, call_next):
        if self._limit <= 0 or request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)
        client = str(
            getattr(request.state, "tenant_id", None)
            or (request.client.host if request.client else "unknown")
        )
        now = time.monotonic()
        self._sweep_idle_clients(now)
        bucket = self._hits.setdefault(client, deque())
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试。"},
            )
        bucket.append(now)
        return await call_next(request)

    def _sweep_idle_clients(self, now: float) -> None:
        """Drop buckets for clients that have gone quiet.

        Without this the map keeps one deque per source IP for the lifetime of the
        process, so a public deployment leaks memory in proportion to the number of
        distinct callers it has ever seen.
        """

        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for client in [
            client
            for client, bucket in self._hits.items()
            if not bucket or bucket[-1] < cutoff
        ]:
            del self._hits[client]


def _request_job_is_owned(path: str, tenant_id: str) -> bool:
    match = re.search(r"/jobs/([a-f0-9]{32})(?:/|$)", path)
    if match is None:
        return True
    try:
        from data_agent.api.job_store import read_job_status

        return tenant_owns_payload(tenant_id, read_job_status(match.group(1)))
    except HTTPException:
        return False


def install_middlewares(app) -> None:
    """Install security + observability middleware in the right order.

    Starlette runs middleware in reverse registration order, so the last one
    registered is the outermost. Execution order ends up: request-context (so even
    rejected requests get a logged trace id) -> API key -> rate limit -> route. CORS
    is added via the dedicated CORSMiddleware in app.py.
    """
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(RequestContextMiddleware)


def raise_http(status_code: int, detail: str) -> None:
    raise HTTPException(status_code=status_code, detail=detail)
