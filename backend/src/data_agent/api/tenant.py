"""Compatibility re-export; shared tenant context lives outside the API package."""

from data_agent.tenancy import (
    TenantIdentity,
    TenantRole,
    configured_tenant_identities,
    current_tenant_id,
    default_tenant_id,
    request_tenant_id,
    reset_current_tenant,
    resolve_tenant_identity,
    role_allows,
    set_current_tenant,
    tenant_from_config,
    tenant_owns_payload,
    tenant_scope,
    validate_tenant_id,
)

__all__ = [
    "TenantIdentity",
    "TenantRole",
    "configured_tenant_identities",
    "current_tenant_id",
    "default_tenant_id",
    "request_tenant_id",
    "reset_current_tenant",
    "resolve_tenant_identity",
    "role_allows",
    "set_current_tenant",
    "tenant_from_config",
    "tenant_owns_payload",
    "tenant_scope",
    "validate_tenant_id",
]
