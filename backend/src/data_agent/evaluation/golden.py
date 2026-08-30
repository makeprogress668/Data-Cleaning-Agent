"""Versioned synthetic tasks for deterministic cross-domain evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class GoldenColumnGenerator(BaseModel):
    kind: Literal["sequence", "split"]
    prefix: str = ""
    start: int = 1
    step: int = 1
    low_count: int = Field(default=0, ge=0)
    low: Any = None
    high: Any = None


class GoldenGeneratedTable(BaseModel):
    row_count: int = Field(ge=1)
    columns: dict[str, GoldenColumnGenerator]


class GoldenInputOptions(BaseModel):
    file_format: Literal["csv", "xlsx"] = "csv"
    header_rows: list[list[Any]] = Field(default_factory=list)
    merge_cells: list[str] = Field(default_factory=list)
    hidden_rows: list[int] = Field(default_factory=list)


class GoldenSheetExpectations(BaseModel):
    exact_columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    forbidden_columns: list[str] = Field(default_factory=list)
    row_assertions: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_rows: list[dict[str, Any]] = Field(default_factory=list)
    min_rows: int = Field(default=0, ge=0)
    exact_rows: int | None = Field(default=None, ge=0)


class GoldenExpectations(BaseModel):
    task_actions: list[str] = Field(default_factory=list)
    forbidden_task_actions: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    forbidden_capabilities: list[str] = Field(default_factory=list)
    sheets: list[str] = Field(default_factory=lambda: ["处理结果"])
    charts: list[str] = Field(default_factory=list)
    report_enabled: bool = False
    max_clarification_slots: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    required_columns: list[str] = Field(default_factory=list)
    forbidden_columns: list[str] = Field(default_factory=list)
    row_assertions: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_rows: list[dict[str, Any]] = Field(default_factory=list)
    sheet_expectations: dict[str, GoldenSheetExpectations] = Field(default_factory=dict)
    allow_formulas: bool = False


class GoldenTask(BaseModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    domain: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    tables: dict[str, dict[str, list[Any]]] = Field(default_factory=dict)
    generated_tables: dict[str, GoldenGeneratedTable] = Field(default_factory=dict)
    input_options: dict[str, GoldenInputOptions] = Field(default_factory=dict)
    expectations: GoldenExpectations
    description: str = ""
    scenario_id: str = ""
    variant_index: int = Field(default=1, ge=1)
    tags: list[str] = Field(default_factory=list)
    requires_llm: bool = False

    @model_validator(mode="after")
    def validate_inputs(self) -> GoldenTask:
        if not self.tables and not self.generated_tables:
            raise ValueError("golden task requires at least one input table")
        overlap = set(self.tables) & set(self.generated_tables)
        if overlap:
            raise ValueError(f"golden tables cannot be both static and generated: {overlap}")
        unknown_options = set(self.input_options) - (
            set(self.tables) | set(self.generated_tables)
        )
        if unknown_options:
            raise ValueError(f"input_options reference unknown tables: {unknown_options}")
        return self


def load_golden_tasks(path: Optional[Union[str, Path]] = None) -> list[GoldenTask]:
    suite_path = (
        Path(path)
        if path is not None
        else Path(__file__).with_name("golden_tasks.json")
    )
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    tasks: list[GoldenTask] = []
    for raw_item in payload.get("tasks", []):
        item = dict(raw_item)
        variants = [item.pop("goal"), *item.pop("goal_variants", [])]
        variant_expectations = {
            int(index): dict(value)
            for index, value in item.pop("variant_expectations", {}).items()
        }
        llm_variant_indexes = {
            int(index) for index in item.pop("llm_variant_indexes", [])
        }
        scenario_id = str(item["task_id"])
        for index, goal in enumerate(variants, start=1):
            expectations = _deep_merge(
                item["expectations"],
                variant_expectations.get(index, {}),
            )
            variant = {
                **item,
                "task_id": scenario_id if index == 1 else f"{scenario_id}_v{index:02d}",
                "scenario_id": scenario_id,
                "variant_index": index,
                "requires_llm": index in llm_variant_indexes,
                "goal": goal,
                "expectations": expectations,
            }
            tasks.append(GoldenTask.model_validate(variant))
    if not tasks:
        raise ValueError(f"golden task suite is empty: {suite_path}")
    task_ids = [task.task_id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("golden task ids must be unique")
    return tasks


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply a small per-paraphrase expectation override without mutating the suite."""

    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
