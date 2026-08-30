"""Tenant identity and context shared by API, workers, cache and memory."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request

TenantRole = Literal["viewer", "operator", "admin"]
_TENANT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CURRENT_TENANT: ContextVar[str | None] = ContextVar(
    "data_agent_tenant",
    default=None,
)


@dataclass(frozen=True)
class TenantIdentity:
    tenant_id: str
    role: TenantRole = "admin"


def default_tenant_id() -> str:
    return validate_tenant_id(os.environ.get("DATA_AGENT_TENANT_ID", "default"))


def validate_tenant_id(value: object) -> str:
    tenant_id = str(value or "").strip()
    if not _TENANT_PATTERN.fullmatch(tenant_id):
        raise RuntimeError(
            "tenant id 只能包含字母、数字、点、下划线和连字符，最长 64 个字符"
        )
    return tenant_id


def configured_tenant_identities() -> dict[str, tuple[str, TenantRole]]:
    """Read tenant credentials, preserving the legacy single-key deployment."""

    raw = os.environ.get("DATA_AGENT_TENANT_API_KEYS_JSON", "").strip()
    if not raw:
        legacy_key = os.environ.get("DATA_AGENT_API_KEY", "").strip()
        return {default_tenant_id(): (legacy_key, "admin")} if legacy_key else {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("DATA_AGENT_TENANT_API_KEYS_JSON 必须是合法 JSON") from exc
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError("DATA_AGENT_TENANT_API_KEYS_JSON 必须是非空对象")

    identities: dict[str, tuple[str, TenantRole]] = {}
    seen_keys: set[str] = set()
    for raw_tenant, raw_entry in payload.items():
        tenant_id = validate_tenant_id(raw_tenant)
        if isinstance(raw_entry, str):
            api_key = raw_entry.strip()
            role: TenantRole = "admin"
        elif isinstance(raw_entry, dict):
            api_key = str(raw_entry.get("api_key") or "").strip()
            role_text = str(raw_entry.get("role") or "admin").strip().lower()
            if role_text not in {"viewer", "operator", "admin"}:
                raise RuntimeError(f"租户 {tenant_id} 的 role 无效")
            role = role_text  # type: ignore[assignment]
        else:
            raise RuntimeError(f"租户 {tenant_id} 的认证配置无效")
        if not api_key:
            raise RuntimeError(f"租户 {tenant_id} 缺少 api_key")
        if api_key in seen_keys:
            raise RuntimeError("不同租户不能使用相同 API Key")
        seen_keys.add(api_key)
        identities[tenant_id] = (api_key, role)
    return identities


def resolve_tenant_identity(api_key: str) -> TenantIdentity | None:
    identities = configured_tenant_identities()
    if not identities:
        return TenantIdentity(default_tenant_id())
    for tenant_id, (expected, role) in identities.items():
        if secrets.compare_digest(api_key, expected):
            return TenantIdentity(tenant_id, role)
    return None


def current_tenant_id() -> str:
    return _CURRENT_TENANT.get() or default_tenant_id()


def set_current_tenant(tenant_id: str) -> Token[str | None]:
    return _CURRENT_TENANT.set(validate_tenant_id(tenant_id))


def reset_current_tenant(token: Token[str | None]) -> None:
    _CURRENT_TENANT.reset(token)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    token = set_current_tenant(tenant_id)
    try:
        yield
    finally:
        reset_current_tenant(token)


def request_tenant_id(request: Request) -> str:
    return validate_tenant_id(
        getattr(request.state, "tenant_id", None) or default_tenant_id()
    )


def tenant_from_config(config: dict[str, Any] | None) -> str:
    raw = (config or {}).get("_tenant_id")
    return validate_tenant_id(raw or current_tenant_id())


def tenant_owns_payload(tenant_id: str, payload: dict[str, Any]) -> bool:
    owner = validate_tenant_id(payload.get("tenant_id") or default_tenant_id())
    return secrets.compare_digest(validate_tenant_id(tenant_id), owner)


def role_allows(role: TenantRole, method: str) -> bool:
    method = method.upper()
    if role == "admin":
        return True
    if role == "viewer":
        return method in {"GET", "HEAD", "OPTIONS"}
    return method != "DELETE"
