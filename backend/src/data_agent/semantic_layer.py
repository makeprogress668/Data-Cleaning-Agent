"""Tenant-scoped business semantics compiled into the typed task contract."""

from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_agent.agent.task_spec import build_task_spec
from data_agent.planning.goal_interpreter import derive_capabilities, interpret_goal
from data_agent.schemas.job import PivotAggregation
from data_agent.tenancy import validate_tenant_id

_DOCUMENT_KIND = "semantic_model"
_IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
_file_lock = threading.RLock()


class SemanticDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    field: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SemanticMeasure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    field: str | None = None
    aggregation: PivotAggregation = "sum"
    output_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SemanticEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    primary_key: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    dimensions: list[SemanticDimension] = Field(default_factory=list)
    measures: list[SemanticMeasure] = Field(default_factory=list)


class SemanticRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_entity: str = Field(min_length=1)
    to_entity: str = Field(min_length=1)
    from_keys: list[str] = Field(min_length=1)
    to_keys: list[str] = Field(min_length=1)
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many"] = "many_to_one"

    @model_validator(mode="after")
    def validate_keys(self) -> SemanticRelationship:
        if len(self.from_keys) != len(self.to_keys):
            raise ValueError("Semantic relationship key counts must match")
        return self


class SemanticModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    entities: list[SemanticEntity] = Field(min_length=1)
    relationships: list[SemanticRelationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_names(self) -> SemanticModelDefinition:
        entity_names = [entity.name for entity in self.entities]
        if len(entity_names) != len(set(entity_names)):
            raise ValueError("Semantic entity names must be unique")
        known = set(entity_names)
        for relation in self.relationships:
            if relation.from_entity not in known or relation.to_entity not in known:
                raise ValueError("Semantic relationship references an unknown entity")
        terms: list[str] = []
        for entity in self.entities:
            for item in [*entity.dimensions, *entity.measures]:
                terms.append(item.name.casefold())
                terms.extend(alias.casefold() for alias in item.aliases)
        if len(terms) != len(set(terms)):
            raise ValueError("Semantic dimension, measure names and aliases must be unique")
        return self


def create_semantic_model(
    definition: SemanticModelDefinition | dict[str, Any],
    *,
    tenant_id: str,
) -> dict[str, Any]:
    tenant = validate_tenant_id(tenant_id)
    model = (
        definition
        if isinstance(definition, SemanticModelDefinition)
        else SemanticModelDefinition.model_validate(definition)
    )
    identifier = uuid.uuid4().hex
    now = _now_iso()
    document = {
        "schema_version": 1,
        "semantic_model_id": identifier,
        "job_id": identifier,
        "tenant_id": tenant,
        **model.model_dump(mode="json"),
        "created_at": now,
        "updated_at": now,
    }
    return _write(document)


def list_semantic_models(*, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    tenant = validate_tenant_id(tenant_id)
    repository = _metadata_repository()
    if repository is not None:
        documents = repository.list_documents(
            _DOCUMENT_KIND,
            tenant_id=tenant,
            limit=max(1, min(limit, 100)),
        )
    else:
        root = _tenant_dir(tenant)
        documents = []
        if root.exists():
            with _file_lock:
                for path in root.glob("*.json"):
                    try:
                        documents.append(_read_file(path))
                    except (OSError, ValueError):
                        continue
        documents.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return [_public(item) for item in documents[: max(1, min(limit, 100))]]


def read_semantic_model(identifier: str, *, tenant_id: str) -> dict[str, Any]:
    if not _IDENTIFIER.fullmatch(identifier):
        raise HTTPException(status_code=404, detail="业务语义模型不存在")
    tenant = validate_tenant_id(tenant_id)
    repository = _metadata_repository()
    if repository is not None:
        document = repository.read_document(identifier, _DOCUMENT_KIND)
    else:
        path = _tenant_dir(tenant) / f"{identifier}.json"
        document = _read_file(path) if path.exists() else None
    if not document or str(document.get("tenant_id") or "default") != tenant:
        raise HTTPException(status_code=404, detail="业务语义模型不存在")
    return _public(document)


def delete_semantic_model(identifier: str, *, tenant_id: str) -> bool:
    document = read_semantic_model(identifier, tenant_id=tenant_id)
    repository = _metadata_repository()
    if repository is not None:
        return repository.delete_job(identifier)
    path = _tenant_dir(str(document["tenant_id"])) / f"{identifier}.json"
    with _file_lock:
        path.unlink(missing_ok=True)
    return True


def resolve_semantic_goal(
    goal: str,
    tables: dict[str, pd.DataFrame],
    raw_model: dict[str, Any],
) -> dict[str, Any]:
    """Resolve named measures/dimensions without an LLM call."""

    model = SemanticModelDefinition.model_validate(_definition_payload(raw_model))
    selected_measures: list[tuple[SemanticEntity, SemanticMeasure]] = []
    selected_dimensions: list[tuple[SemanticEntity, SemanticDimension]] = []
    for entity in model.entities:
        for measure in entity.measures:
            if _mentions(goal, measure.name, measure.aliases):
                selected_measures.append((entity, measure))
        for dimension in entity.dimensions:
            if _mentions(goal, dimension.name, dimension.aliases):
                selected_dimensions.append((entity, dimension))
    if not selected_measures:
        raise ValueError("目标没有引用所选语义模型中的业务指标。")
    base_names = {entity.name for entity, _measure in selected_measures}
    if len(base_names) != 1:
        raise ValueError("一次任务暂不支持跨多个事实实体计算业务指标。")
    base_entity = selected_measures[0][0]
    _validate_physical_schema(model, tables)

    plan = interpret_goal(goal, tables)
    capabilities = derive_capabilities(goal, tables)
    capabilities = {**capabilities, "needs_analysis": True}
    if any(entity.name != base_entity.name for entity, _item in selected_dimensions):
        capabilities["needs_lookup"] = True
    metrics = [
        {
            "column": measure.field,
            "agg": measure.aggregation,
            "output_name": measure.output_name,
        }
        for _entity, measure in selected_measures
    ]
    group_by = [dimension.field for _entity, dimension in selected_dimensions]
    target_fields = [
        {"table": entity.table, "field": item.field}
        for entity, item in [*selected_dimensions, *selected_measures]
        if item.field
    ]
    plan.update(
        {
            "goal": goal,
            "capabilities": capabilities,
            "suggested_base_table": base_entity.table,
            "target_fields": target_fields,
            "aggregation": {"group_by": group_by, "metrics": metrics},
            "understanding_source": "semantic_model",
            "understanding_confidence": "high",
            "is_generic_fallback": False,
            "clarification_questions": [],
            "assumptions": [f"业务口径来自语义模型：{model.name}"],
            "semantic_resolution": {
                "model_id": str(raw_model.get("semantic_model_id") or ""),
                "model_name": model.name,
                "base_entity": base_entity.name,
                "measures": [measure.name for _entity, measure in selected_measures],
                "dimensions": [
                    {"entity": entity.name, "name": item.name, "field": item.field}
                    for entity, item in selected_dimensions
                ],
            },
        }
    )
    plan["task_spec"] = build_task_spec(goal, plan, tables).model_dump(mode="json")
    return plan


def apply_semantic_relationships(
    job_config: dict[str, Any],
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    raw_model: dict[str, Any],
) -> dict[str, Any]:
    """Bind cross-entity dimensions through declared relationships."""

    resolution = goal_plan.get("semantic_resolution") or {}
    if not resolution:
        return job_config
    model = SemanticModelDefinition.model_validate(_definition_payload(raw_model))
    entities = {item.name: item for item in model.entities}
    base_name = str(resolution.get("base_entity") or "")
    base = entities[base_name]
    planned = copy.deepcopy(job_config)
    existing = list(planned.get("lookups") or [])
    for dimension in resolution.get("dimensions") or []:
        target_name = str(dimension.get("entity") or "")
        if not target_name or target_name == base_name:
            continue
        target = entities[target_name]
        relation, reversed_keys = _relationship(model, base_name, target_name)
        left_keys = relation.to_keys if reversed_keys else relation.from_keys
        right_keys = relation.from_keys if reversed_keys else relation.to_keys
        field = str(dimension.get("field") or "")
        lookup = {
            "name": f"semantic_{base.name}_{target.name}",
            "source_table": target.table,
            "left_keys": list(left_keys),
            "right_keys": list(right_keys),
            "fields": [field],
            "match_mode": "exact",
            "duplicate_strategy": "error",
        }
        existing_index = next(
            (
                index
                for index, item in enumerate(existing)
                if isinstance(item, dict)
                and str(item.get("source_table") or "") == target.table
                and field in (item.get("fields") or [])
            ),
            None,
        )
        if existing_index is None:
            existing.append(lookup)
        else:
            # The selected semantic model is an explicit business contract. Replace
            # an inferred fuzzy/normalised join with its declared exact relationship.
            existing[existing_index] = lookup
    planned["lookups"] = existing
    return planned


def _relationship(
    model: SemanticModelDefinition,
    base_name: str,
    target_name: str,
) -> tuple[SemanticRelationship, bool]:
    for relation in model.relationships:
        if relation.from_entity == base_name and relation.to_entity == target_name:
            return relation, False
        if relation.to_entity == base_name and relation.from_entity == target_name:
            return relation, True
    raise ValueError(f"语义实体 {base_name} 与 {target_name} 之间没有声明关系。")


def _validate_physical_schema(
    model: SemanticModelDefinition,
    tables: dict[str, pd.DataFrame],
) -> None:
    for entity in model.entities:
        if entity.table not in tables:
            raise ValueError(f"语义实体 {entity.name} 对应的数据表不存在：{entity.table}")
        available = {str(column) for column in tables[entity.table].columns}
        fields = [*entity.primary_key]
        fields.extend(item.field for item in entity.dimensions)
        fields.extend(item.field for item in entity.measures if item.field)
        missing = sorted(set(fields) - available)
        if missing:
            raise ValueError(f"数据表 {entity.table} 缺少语义字段：{', '.join(missing)}")


def _mentions(goal: str, name: str, aliases: list[str]) -> bool:
    lowered = goal.casefold()
    return any(term.casefold() in lowered for term in [name, *aliases] if term.strip())


def _definition_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(document.get(key))
        for key in ("name", "description", "entities", "relationships")
    }


def _write(document: dict[str, Any]) -> dict[str, Any]:
    identifier = str(document["semantic_model_id"])
    repository = _metadata_repository()
    if repository is not None:
        stored = repository.write_document(identifier, _DOCUMENT_KIND, document)
    else:
        path = _tenant_dir(str(document["tenant_id"])) / f"{identifier}.json"
        with _file_lock:
            _atomic_write_json(path, document)
        stored = document
    return _public(stored)


def _public(document: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in document.items() if key != "job_id"}


def _tenant_dir(tenant_id: str) -> Path:
    from data_agent.api.job_store import api_work_dir

    return api_work_dir().parent / "semantic_models" / tenant_id


def _read_file(path: Path) -> dict[str, Any]:
    from data_agent.api.job_store import read_store_text

    decoded = json.loads(read_store_text(path))
    if not isinstance(decoded, dict):
        raise ValueError("invalid semantic model")
    return decoded


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata_repository() -> Any:
    # The semantic compiler is imported by the processing service. Import storage
    # only when persistence is used so service-only CLI/evaluation imports do not
    # create a services -> semantic layer -> API -> services cycle.
    from data_agent.api.metadata_repository import configured_metadata_repository

    return configured_metadata_repository()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    from data_agent.api.job_store import atomic_write_json

    atomic_write_json(path, payload)
