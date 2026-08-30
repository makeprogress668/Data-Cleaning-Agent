"""HTTP contract for tenant-scoped reusable recipes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from data_agent.api.job_store import read_job_status, read_plan_snapshot
from data_agent.api.recipe_store import (
    create_recipe,
    delete_recipe,
    list_recipes,
    read_recipe,
)
from data_agent.tenancy import request_tenant_id

router = APIRouter(prefix="/api/v1/recipes", tags=["recipes"])


class CreateRecipeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    source_job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.get("")
def get_recipes(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> dict:
    return {"recipes": list_recipes(tenant_id=request_tenant_id(request), limit=limit)}


@router.post("", status_code=201)
def save_recipe(request: Request, payload: CreateRecipeRequest) -> dict:
    tenant_id = request_tenant_id(request)
    source = read_job_status(payload.source_job_id)
    if source.get("status") not in {"succeeded", "completed", "success"}:
        raise HTTPException(status_code=409, detail="只有成功任务可以保存为 Recipe。")
    try:
        return create_recipe(
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            source_job=source,
            plan_snapshot=read_plan_snapshot(payload.source_job_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{recipe_id}")
def get_recipe(request: Request, recipe_id: str) -> dict:
    return read_recipe(recipe_id, tenant_id=request_tenant_id(request))


@router.delete("/{recipe_id}")
def remove_recipe(request: Request, recipe_id: str) -> dict:
    delete_recipe(recipe_id, tenant_id=request_tenant_id(request))
    return {"recipe_id": recipe_id, "status": "deleted"}
