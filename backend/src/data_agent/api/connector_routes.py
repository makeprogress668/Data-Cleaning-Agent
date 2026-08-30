"""Create jobs from validated Connector SDK sources."""

from __future__ import annotations

import shutil
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from data_agent.api.job_manager import get_job_manager
from data_agent.api.job_store import api_work_dir, delete_job
from data_agent.api.quotas import (
    enforce_batch_upload_bytes,
    enforce_tenant_admission,
    enforce_tenant_storage_growth,
)
from data_agent.connectors import ConnectorContext, get_connector_registry
from data_agent.tenancy import request_tenant_id

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


class ConnectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    mode: Literal["discover", "plan", "answer"] = "answer"
    sources: list[ConnectorInput] = Field(min_length=1, max_length=50)
    semantic_model_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


@router.get("")
def list_connectors() -> dict:
    return {"connectors": get_connector_registry().descriptors()}


@router.post("/jobs", status_code=202)
async def create_connector_job(request: Request, payload: ConnectorJobRequest) -> dict:
    tenant_id = request_tenant_id(request)
    usage = enforce_tenant_admission(tenant_id)
    manager = get_job_manager()
    job_id = manager.new_job_id()
    job_root = api_work_dir() / job_id
    upload_dir = job_root / "uploads"
    registry = get_connector_registry()
    provenance = []
    try:
        paths = []
        for source in payload.sources:
            materialized = registry.materialize(
                source.connector_id,
                source.config,
                upload_dir,
                ConnectorContext(tenant_id=tenant_id, job_id=job_id),
            )
            paths.extend(materialized)
            provenance.append(
                {
                    "connector_id": source.connector_id,
                    "files": [path.name for path in materialized],
                }
            )
        sizes = [path.stat().st_size for path in paths]
        enforce_batch_upload_bytes(sum(sizes))
        enforce_tenant_storage_growth(
            tenant_id,
            sum(sizes),
            current_stored_bytes=usage["stored_bytes"],
        )
        config: dict[str, Any] = {
            "_tenant_id": tenant_id,
            "include_diagnostics": True,
            "connector_provenance": provenance,
            "job_overrides": {"goal": payload.goal, "mode": payload.mode},
        }
        if payload.semantic_model_id:
            from data_agent.semantic_layer import read_semantic_model

            config["semantic_model_id"] = payload.semantic_model_id
            config["semantic_model"] = read_semantic_model(
                payload.semantic_model_id,
                tenant_id=tenant_id,
            )
        await manager.submit(job_id, paths, config)
    except HTTPException:
        delete_job(job_id)
        raise
    except Exception as exc:
        if job_root.exists():
            shutil.rmtree(job_root, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "status": "pending",
        "connector_provenance": provenance,
        "files": {"download_url": f"/api/v1/cleaning/jobs/{job_id}/result.xlsx"},
    }
