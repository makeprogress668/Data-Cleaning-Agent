"""End-user authentication for API keys and OIDC access tokens."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from data_agent.tenancy import TenantIdentity, TenantRole, validate_tenant_id

_ROLES = frozenset({"viewer", "operator", "admin"})


@dataclass(frozen=True)
class OIDCConfiguration:
    issuer: str
    audience: str
    jwks_url: str | None
    public_key: str | None
    algorithms: tuple[str, ...]
    tenant_claim: str
    role_claim: str
    default_tenant: str | None
    default_role: TenantRole
    leeway_seconds: int


def oidc_configuration() -> OIDCConfiguration | None:
    """Read and validate the OIDC contract without accepting partial setup."""

    values = {
        "issuer": os.environ.get("DATA_AGENT_OIDC_ISSUER", "").strip(),
        "audience": os.environ.get("DATA_AGENT_OIDC_AUDIENCE", "").strip(),
        "jwks_url": os.environ.get("DATA_AGENT_OIDC_JWKS_URL", "").strip(),
        "public_key": os.environ.get("DATA_AGENT_OIDC_PUBLIC_KEY", "").strip(),
    }
    if not any(values.values()):
        return None
    if not values["issuer"] or not values["audience"]:
        raise RuntimeError("OIDC 鉴权必须同时配置 issuer 和 audience")
    if bool(values["jwks_url"]) == bool(values["public_key"]):
        raise RuntimeError("OIDC 鉴权必须且只能配置 JWKS URL 或静态公钥之一")
    if values["jwks_url"]:
        parsed_jwks = urlparse(values["jwks_url"])
        local_jwks = parsed_jwks.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed_jwks.scheme != "https" and not (
            parsed_jwks.scheme == "http" and local_jwks
        ):
            raise RuntimeError("OIDC JWKS URL 必须使用 HTTPS（本机开发除外）")

    algorithms = tuple(
        item.strip().upper()
        for item in os.environ.get("DATA_AGENT_OIDC_ALGORITHMS", "RS256").split(",")
        if item.strip()
    )
    if not algorithms or "NONE" in algorithms:
        raise RuntimeError("OIDC algorithms 必须是非空签名算法白名单")
    symmetric = {item.startswith("HS") for item in algorithms}
    if len(symmetric) > 1 or (values["jwks_url"] and True in symmetric):
        raise RuntimeError("OIDC 不允许混用对称与非对称算法，JWKS 不支持 HS 算法")
    if True in symmetric and len(values["public_key"].encode("utf-8")) < 32:
        raise RuntimeError("OIDC HS 签名密钥不得短于 32 字节")

    default_role_text = os.environ.get(
        "DATA_AGENT_OIDC_DEFAULT_ROLE", "viewer"
    ).strip().lower()
    if default_role_text not in _ROLES:
        raise RuntimeError("DATA_AGENT_OIDC_DEFAULT_ROLE 必须是 viewer/operator/admin")
    raw_default_tenant = os.environ.get("DATA_AGENT_OIDC_DEFAULT_TENANT", "").strip()
    try:
        leeway_seconds = max(
            0,
            int(os.environ.get("DATA_AGENT_OIDC_LEEWAY_SECONDS", "30")),
        )
    except ValueError as exc:
        raise RuntimeError("DATA_AGENT_OIDC_LEEWAY_SECONDS 必须是整数") from exc

    return OIDCConfiguration(
        issuer=values["issuer"],
        audience=values["audience"],
        jwks_url=values["jwks_url"] or None,
        public_key=(values["public_key"].replace("\\n", "\n") or None),
        algorithms=algorithms,
        tenant_claim=os.environ.get("DATA_AGENT_OIDC_TENANT_CLAIM", "tenant_id").strip()
        or "tenant_id",
        role_claim=os.environ.get("DATA_AGENT_OIDC_ROLE_CLAIM", "role").strip()
        or "role",
        default_tenant=(
            validate_tenant_id(raw_default_tenant) if raw_default_tenant else None
        ),
        default_role=default_role_text,  # type: ignore[arg-type]
        leeway_seconds=leeway_seconds,
    )


def oidc_auth_configured() -> bool:
    return oidc_configuration() is not None


def resolve_oidc_identity(authorization: str) -> TenantIdentity | None:
    """Verify a Bearer token and map trusted claims to tenant and role."""

    scheme, separator, token = authorization.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        return None
    config = oidc_configuration()
    if config is None:
        return None
    try:
        import jwt

        key = config.public_key
        if config.jwks_url:
            key = _jwks_client(config.jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(config.algorithms),
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.leeway_seconds,
            options={"require": ["exp", "sub"]},
        )
    except ImportError as exc:
        raise RuntimeError("OIDC 鉴权已配置，但 PyJWT 尚未安装") from exc
    except jwt.PyJWTError:
        return None

    raw_tenant = claims.get(config.tenant_claim) or config.default_tenant
    if not raw_tenant:
        return None
    try:
        tenant_id = validate_tenant_id(raw_tenant)
    except RuntimeError:
        return None
    role_text = str(claims.get(config.role_claim) or config.default_role).strip().lower()
    if role_text not in _ROLES:
        return None
    return TenantIdentity(tenant_id=tenant_id, role=role_text)  # type: ignore[arg-type]


@lru_cache(maxsize=8)
def _jwks_client(url: str):
    import jwt

    # The client caches the key set, avoiding one IdP request per API call while
    # still refreshing after key rotation.
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300, timeout=5)
