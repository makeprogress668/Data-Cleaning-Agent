"""Tenant-scoped reusable task contracts.

A recipe stores the validated semantic contract from a successful job, not its
input paths or executable snapshot.  Applying it profiles the new upload and
recompiles a fresh ExecutionPlan, so content hashes, row budgets and confirmation
requirements always belong to the new data.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from data_agent.api.job_store import api_work_dir, atomic_write_json, read_store_text
from data_agent.api.metadata_repository import configured_metadata_repository
from data_agent.schemas.task import TaskSpec
from data_agent.tenancy import validate_tenant_id

_RECIPE_DOCUMENT = "recipe"
_RECIPE_ID = re.compile(r"^[a-f0-9]{32}$")
_file_lock = threading.RLock()


def create_recipe(
    *,
    tenant_id: str,
    name: str,
    description: str,
    source_job: dict[str, Any],
    plan_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Freeze a successful job's typed intent as a reusable recipe."""

    tenant = validate_tenant_id(tenant_id)
    recipe_name = name.strip()
    if not recipe_name:
        raise ValueError("Recipe name cannot be empty")
    goal_plan = copy.deepcopy(
        plan_snapshot.get("goal_plan") or source_job.get("goal_plan") or {}
    )
    raw_task = goal_plan.get("task_spec")
    if not isinstance(raw_task, dict):
        raise ValueError("该任务没有可复用的 TaskSpec。")
    task = TaskSpec.model_validate(raw_task)
    blocking = [slot for slot in task.missing_slots if slot.priority == "high"]
    if blocking:
        raise ValueError("该任务仍有阻塞性问题，不能保存为 Recipe。")

    now = _now_iso()
    recipe_id = uuid.uuid4().hex
    document = {
        "schema_version": 1,
        "recipe_id": recipe_id,
        # MetadataRepository is job-shaped; keep both keys so its generic tenant
        # filtering and sort logic remain valid without a second database table.
        "job_id": recipe_id,
        "tenant_id": tenant,
        "name": recipe_name,
        "description": description.strip(),
        "source_job_id": str(source_job.get("job_id") or ""),
        "goal": str(task.objective or source_job.get("goal") or ""),
        "goal_plan": goal_plan,
        "task_spec": task.model_dump(mode="json"),
        "source_plan_hash": str(plan_snapshot.get("plan_hash") or ""),
        "source_schema_fingerprint": str(
            plan_snapshot.get("schema_fingerprint") or ""
        ),
        "compiler_version": str(plan_snapshot.get("compiler_version") or ""),
        "planner_metadata": copy.deepcopy(
            plan_snapshot.get("planner_metadata") or {}
        ),
        "created_at": now,
        "updated_at": now,
    }
    return _write_recipe(document)


def list_recipes(*, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    tenant = validate_tenant_id(tenant_id)
    repository = configured_metadata_repository()
    if repository is not None:
        return [
            _public_recipe(item)
            for item in repository.list_documents(
                _RECIPE_DOCUMENT,
                tenant_id=tenant,
                limit=max(1, min(limit, 100)),
            )
        ]
    root = _tenant_recipe_dir(tenant)
    if not root.exists():
        return []
    documents = []
    with _file_lock:
        for path in root.glob("*.json"):
            try:
                document = _read_recipe_file(path)
            except (OSError, ValueError):
                continue
            documents.append(document)
    documents.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return [_public_recipe(item) for item in documents[: max(1, min(limit, 100))]]


def read_recipe(recipe_id: str, *, tenant_id: str) -> dict[str, Any]:
    _validate_recipe_id(recipe_id)
    tenant = validate_tenant_id(tenant_id)
    repository = configured_metadata_repository()
    if repository is not None:
        document = repository.read_document(recipe_id, _RECIPE_DOCUMENT)
    else:
        path = _tenant_recipe_dir(tenant) / f"{recipe_id}.json"
        document = _read_recipe_file(path) if path.exists() else None
    if not document or str(document.get("tenant_id") or "default") != tenant:
        # Match job isolation: cross-tenant identifiers are indistinguishable from
        # missing resources.
        raise HTTPException(status_code=404, detail="Recipe 不存在")
    return copy.deepcopy(document)


def delete_recipe(recipe_id: str, *, tenant_id: str) -> bool:
    document = read_recipe(recipe_id, tenant_id=tenant_id)
    repository = configured_metadata_repository()
    if repository is not None:
        return repository.delete_job(recipe_id)
    path = _tenant_recipe_dir(str(document["tenant_id"])) / f"{recipe_id}.json"
    with _file_lock:
        path.unlink(missing_ok=True)
    return True


def recipe_processing_config(recipe: dict[str, Any]) -> dict[str, Any]:
    """Return only the trusted fields the planning service consumes."""

    task = TaskSpec.model_validate(recipe.get("task_spec") or {})
    goal_plan = copy.deepcopy(recipe.get("goal_plan") or {})
    goal_plan["task_spec"] = task.model_dump(mode="json")
    return {
        "recipe_id": str(recipe.get("recipe_id") or ""),
        "recipe_version": int(recipe.get("schema_version") or 1),
        "recipe_goal_plan": goal_plan,
        "goal": str(recipe.get("goal") or task.objective),
    }


def _write_recipe(document: dict[str, Any]) -> dict[str, Any]:
    recipe_id = str(document["recipe_id"])
    repository = configured_metadata_repository()
    if repository is not None:
        stored = repository.write_document(recipe_id, _RECIPE_DOCUMENT, document)
    else:
        path = _tenant_recipe_dir(str(document["tenant_id"])) / f"{recipe_id}.json"
        with _file_lock:
            atomic_write_json(path, document)
        stored = document
    return _public_recipe(stored)


def _public_recipe(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in document.items()
        if key != "job_id"
    }


def _tenant_recipe_dir(tenant_id: str) -> Path:
    return api_work_dir().parent / "recipes" / tenant_id


def _read_recipe_file(path: Path) -> dict[str, Any]:
    import json

    decoded = json.loads(read_store_text(path))
    if not isinstance(decoded, dict):
        raise ValueError("invalid recipe document")
    return decoded


def _validate_recipe_id(recipe_id: str) -> None:
    if not _RECIPE_ID.fullmatch(recipe_id):
        raise HTTPException(status_code=404, detail="Recipe 不存在")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
