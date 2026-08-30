from pathlib import Path

import pytest
from fastapi import HTTPException

import data_agent.api.job_store as job_store_module
from data_agent.agent.memory import memory_path
from data_agent.api.artifact_store import artifact_key
from data_agent.api.job_store import (
    STATUS_PENDING,
    STATUS_SUCCEEDED,
    admit_job_status,
    list_job_statuses,
    tenant_resource_usage,
    write_job_status,
)
from data_agent.api.metadata_repository import InMemoryMetadataRepository
from data_agent.api.quotas import (
    enforce_batch_upload_bytes,
    enforce_tenant_admission,
    enforce_tenant_storage_growth,
)
from data_agent.tenancy import tenant_scope


def _tenant_config(tenant_id: str) -> dict:
    return {"_tenant_id": tenant_id, "goal": "测试租户隔离"}


def test_status_listing_and_usage_are_tenant_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "jobs"))
    a_job = "a" * 32
    b_job = "b" * 32
    write_job_status(a_job, STATUS_PENDING, config=_tenant_config("tenant-a"))
    write_job_status(b_job, STATUS_SUCCEEDED, config=_tenant_config("tenant-b"))
    payload = tmp_path / "jobs" / a_job / "uploads" / "input.csv"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"12345")

    assert [item["job_id"] for item in list_job_statuses(tenant_id="tenant-a")] == [a_job]
    assert [item["job_id"] for item in list_job_statuses(tenant_id="tenant-b")] == [b_job]
    usage = tenant_resource_usage("tenant-a")
    assert usage["active_jobs"] == 1
    assert usage["stored_bytes"] >= 5


def test_admission_rejects_active_and_storage_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DATA_AGENT_MIN_FREE_TEMP_BYTES", "0")
    monkeypatch.setenv("DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT", "1")
    write_job_status(
        "a" * 32,
        STATUS_PENDING,
        config=_tenant_config("tenant-a"),
    )
    with pytest.raises(HTTPException) as active:
        enforce_tenant_admission("tenant-a")
    assert active.value.status_code == 429

    monkeypatch.setenv("DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT", "0")
    monkeypatch.setenv("DATA_AGENT_MAX_STORED_BYTES_PER_TENANT", "1")
    with pytest.raises(HTTPException) as stored:
        enforce_tenant_admission("tenant-a")
    assert stored.value.status_code == 507


def test_repository_admission_is_atomic_and_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    monkeypatch.setattr(
        job_store_module,
        "configured_metadata_repository",
        lambda: repository,
    )
    admit_job_status(
        "a" * 32,
        STATUS_PENDING,
        config=_tenant_config("tenant-a"),
        max_active_jobs=1,
    )
    with pytest.raises(HTTPException) as same_tenant:
        admit_job_status(
            "b" * 32,
            STATUS_PENDING,
            config=_tenant_config("tenant-a"),
            max_active_jobs=1,
        )
    assert same_tenant.value.status_code == 429

    admitted = admit_job_status(
        "c" * 32,
        STATUS_PENDING,
        config=_tenant_config("tenant-b"),
        max_active_jobs=1,
    )
    assert admitted["tenant_id"] == "tenant-b"


def test_batch_quota_rejects_the_first_byte_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_MAX_BATCH_UPLOAD_BYTES", "10")
    enforce_batch_upload_bytes(10)
    with pytest.raises(HTTPException) as oversized:
        enforce_batch_upload_bytes(11)
    assert oversized.value.status_code == 413


def test_storage_quota_projects_the_new_batch_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_MAX_STORED_BYTES_PER_TENANT", "10")
    enforce_tenant_storage_growth("tenant-a", 4, current_stored_bytes=6)
    with pytest.raises(HTTPException) as oversized:
        enforce_tenant_storage_growth(
            "tenant-a",
            5,
            current_stored_bytes=6,
        )
    assert oversized.value.status_code == 507


def test_memory_and_artifacts_use_separate_tenant_namespaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_TENANT_API_KEYS_JSON", '{"a":"ka","b":"kb"}')
    monkeypatch.setenv("DATA_AGENT_MEMORY_PATH", str(tmp_path / "memory.json"))
    job_id = "c" * 32
    with tenant_scope("a"):
        a_memory = memory_path()
        a_key = artifact_key(job_id, "output/result.xlsx")
    with tenant_scope("b"):
        b_memory = memory_path()
        b_key = artifact_key(job_id, "output/result.xlsx")

    assert a_memory != b_memory
    assert a_memory.parts[-3:] == ("tenants", "a", "memory.json")
    assert "/tenants/a/" in f"/{a_key}/"
    assert "/tenants/b/" in f"/{b_key}/"
