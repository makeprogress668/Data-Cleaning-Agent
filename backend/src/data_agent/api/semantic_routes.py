"""CRUD API for tenant-scoped business semantic models."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from data_agent.semantic_layer import (
    SemanticModelDefinition,
    create_semantic_model,
    delete_semantic_model,
    list_semantic_models,
    read_semantic_model,
)
from data_agent.tenancy import request_tenant_id

router = APIRouter(prefix="/api/v1/semantic-models", tags=["semantic-models"])


@router.get("")
def get_semantic_models(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> dict:
    return {
        "semantic_models": list_semantic_models(
            tenant_id=request_tenant_id(request),
            limit=limit,
        )
    }


@router.post("", status_code=201)
def save_semantic_model(request: Request, payload: SemanticModelDefinition) -> dict:
    return create_semantic_model(payload, tenant_id=request_tenant_id(request))


@router.get("/{semantic_model_id}")
def get_semantic_model(request: Request, semantic_model_id: str) -> dict:
    return read_semantic_model(
        semantic_model_id,
        tenant_id=request_tenant_id(request),
    )


@router.delete("/{semantic_model_id}")
def remove_semantic_model(request: Request, semantic_model_id: str) -> dict:
    delete_semantic_model(semantic_model_id, tenant_id=request_tenant_id(request))
    return {"semantic_model_id": semantic_model_id, "status": "deleted"}
