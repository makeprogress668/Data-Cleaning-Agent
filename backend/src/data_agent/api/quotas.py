"""Per-tenant admission and bounded local-resource checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from fastapi import HTTPException

from data_agent.api.job_store import api_work_dir, tenant_resource_usage


@dataclass(frozen=True)
class TenantQuota:
    max_active_jobs: int
    max_stored_bytes: int
    max_batch_upload_bytes: int
    min_free_temp_bytes: int


def configured_tenant_quota() -> TenantQuota:
    """Read quota policy at admission time so tests and operators can tune it."""

    return TenantQuota(
        max_active_jobs=_non_negative_env("DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT", 0),
        max_stored_bytes=_non_negative_env(
            "DATA_AGENT_MAX_STORED_BYTES_PER_TENANT",
            0,
        ),
        max_batch_upload_bytes=_non_negative_env(
            "DATA_AGENT_MAX_BATCH_UPLOAD_BYTES",
            500 * 1024 * 1024,
        ),
        min_free_temp_bytes=_non_negative_env(
            "DATA_AGENT_MIN_FREE_TEMP_BYTES",
            256 * 1024 * 1024,
        ),
    )


def enforce_tenant_admission(tenant_id: str) -> dict[str, int]:
    """Reject work before upload when tenant or temporary-disk limits are full."""

    quota = configured_tenant_quota()
    usage = tenant_resource_usage(tenant_id)
    if quota.max_active_jobs and usage["active_jobs"] >= quota.max_active_jobs:
        raise HTTPException(
            status_code=429,
            detail=(
                f"当前租户同时进行的任务已达到 {quota.max_active_jobs} 个上限，"
                "请等待任务完成或取消后重试。"
            ),
        )
    if quota.max_stored_bytes and usage["stored_bytes"] >= quota.max_stored_bytes:
        raise HTTPException(
            status_code=507,
            detail="当前租户的存储配额已用完，请删除历史任务后重试。",
        )

    root = api_work_dir()
    root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(root).free
    if quota.min_free_temp_bytes and free_bytes < quota.min_free_temp_bytes:
        raise HTTPException(
            status_code=507,
            detail="服务临时磁盘空间不足，暂时无法接收新任务。",
        )
    return usage


def enforce_batch_upload_bytes(total_bytes: int) -> None:
    limit = configured_tenant_quota().max_batch_upload_bytes
    if limit and total_bytes > limit:
        limit_mb = limit / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"本批文件合计超过 {limit_mb:.0f} MB 上传上限。",
        )


def enforce_tenant_storage_growth(
    tenant_id: str,
    additional_bytes: int,
    *,
    current_stored_bytes: int | None = None,
) -> None:
    """Reject a batch that would cross the tenant's configured storage ceiling."""

    quota = configured_tenant_quota()
    if not quota.max_stored_bytes:
        return
    stored_bytes = (
        tenant_resource_usage(tenant_id)["stored_bytes"]
        if current_stored_bytes is None
        else max(0, int(current_stored_bytes))
    )
    projected = stored_bytes + max(0, int(additional_bytes))
    if projected > quota.max_stored_bytes:
        raise HTTPException(
            status_code=507,
            detail="本批文件会超过当前租户的存储配额，请减小文件或删除历史任务。",
        )


def quota_projection(tenant_id: str) -> dict[str, int | None]:
    quota = configured_tenant_quota()
    usage = tenant_resource_usage(tenant_id)
    return {
        **usage,
        "max_active_jobs": quota.max_active_jobs or None,
        "max_stored_bytes": quota.max_stored_bytes or None,
        "max_batch_upload_bytes": quota.max_batch_upload_bytes or None,
        "min_free_temp_bytes": quota.min_free_temp_bytes or None,
    }


def _non_negative_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是非负整数") from exc
    if value < 0:
        raise RuntimeError(f"{name} 必须是非负整数")
    return value
