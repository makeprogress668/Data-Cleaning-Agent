from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Optional, Union

import pandas as pd
from pydantic import BaseModel, Field

from data_agent.agent import refine_config_with_llm
from data_agent.agent.goal_understanding import understand_goal
from data_agent.agent.llm_client import build_default_llm_planner, load_llm_config
from data_agent.agent.memory import record_task
from data_agent.agent.planner import (
    collect_clarification_questions,
    compile_task_spec_lookups,
    reconcile_planner_task_contract,
)
from data_agent.agent.prompts import GOAL_PROMPT_VERSION, PLANNER_PROMPT_VERSION
from data_agent.agent.reflection import (
    ATTEMPT_DIR_PREFIX,
    load_reflection_settings,
    run_with_bounded_reflection,
)
from data_agent.business_report import write_business_report
from data_agent.capabilities import (
    PLAN_COMPILER_VERSION,
    build_execution_plan,
    get_capability_registry,
    unmet_task_actions,
    validate_execution_plan,
    validate_job_config_binding,
    validate_task_action_coverage,
    validate_task_rule_preservation,
)
from data_agent.capabilities.artifacts import planned_primary_fields
from data_agent.observability import (
    ExecutionRecorder,
    StageRecord,
    bind_execution_events,
    current_llm_usage,
)
from data_agent.pipelines import run_job
from data_agent.planning.goal_interpreter import (
    apply_goal_to_job_config,
    validate_task_spec_operations,
)
from data_agent.schemas import (
    ChartRef,
    OutputFileRef,
    OutputSpec,
    build_output_spec,
)
from data_agent.schemas.job import JOB_CONFIG_OVERRIDE_KEYS, JobConfig
from data_agent.schemas.plan import (
    ExecutionPlan,
    InputFingerprint,
    PlannerMetadata,
    PlanSnapshot,
)
from data_agent.schemas.task import TaskSpec
from data_agent.semantic_layer import (
    apply_semantic_relationships,
    resolve_semantic_goal,
)
from data_agent.services.business_answer import (
    build_business_answer,
    recommended_actions_from_impact,
)
from data_agent.services.review_delivery import (
    snapshot_relative_path,
    write_result_snapshot,
)
from data_agent.tools import (
    analyze_business_data,
    analyze_exception_impact,
    apply_formulas,
    build_recommendation_package,
    build_template_validation,
    compute_quality_score,
    exception_summary_from_business_summary,
    extract_document_insights,
    generate_business_charts,
    lint_output,
    profile_dataset,
    read_tables_from_paths,
    write_recommended_job_config,
)
from data_agent.tools.delivery_view import (
    data_table_fields,
    delivered_rows,
    delivery_result_sheet,
    missing_template_field_count,
    review_rows,
    withheld_rows,
)
from data_agent.tools.issue_wording import display_issue_name
from data_agent.utils.collections import deep_merge, frame_records

# Alias to the canonical whitelist in schemas.job so the overrides gate here and the
# reflection layer share one source of truth (they previously drifted apart).
JOB_CONFIG_KEYS = JOB_CONFIG_OVERRIDE_KEYS


class ProcessingOptions(BaseModel):
    recursive: bool = True
    include_internal_sheets: bool = False
    result_limit: int = Field(default=100, ge=0)
    include_excel_base64: bool = False
    include_job_config: bool = False
    include_diagnostics: bool = False
    # When set, attach the in-memory result frames so callers can avoid re-reading
    # the workbook from disk. Not used by the API path (keeps the JSON response clean).
    include_result_frames: bool = False
    # When set, attach the deterministic quality-score frames (summary + deductions)
    # so the reflection loop can score a run and reason about its weaknesses. The
    # reflection wrapper pops these before returning, so the JSON response stays clean.
    include_quality_frames: bool = False
    goal: str = ""
    mode: str = "answer"
    output_mode: str = "business_answer"
    job_overrides: dict[str, Any] = Field(default_factory=dict)
    # A Recipe carries a previously validated semantic contract.  The new upload is
    # still profiled and compiled from scratch; only LLM understanding/refinement is
    # skipped, making reuse deterministic and inexpensive.
    recipe_id: str = ""
    recipe_version: int = Field(default=0, ge=0)
    recipe_goal_plan: dict[str, Any] = Field(default_factory=dict)
    semantic_model_id: str = ""
    semantic_model: dict[str, Any] = Field(default_factory=dict)
    connector_provenance: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class PlanResult:
    """Everything the planning phase produces, before the deterministic run.

    Splitting plan from execute lets the async job manager pause after planning to
    ask clarification questions, then resume execution with the same prepared
    JobConfig. ``process_input_paths`` simply chains the two phases, so every
    existing caller keeps its single-call behaviour.
    """

    options: ProcessingOptions
    work_path: Path
    config_path: Path
    output_file: Path
    tables: dict[str, pd.DataFrame]
    source_inventory: Any
    profile_sheets: dict[str, pd.DataFrame]
    document_insights: Any
    recommendation_sheets: dict[str, pd.DataFrame]
    job_config: dict[str, Any]
    execution_plan: ExecutionPlan
    output_spec: OutputSpec
    goal_plan: dict[str, Any]
    clarification_questions: list[dict[str, Any]] = dataclass_field(default_factory=list)
    llm_attempts: pd.DataFrame = dataclass_field(default_factory=pd.DataFrame)


@dataclass
class _PreparedInputContext:
    """Deterministic input context shared by fresh planning and snapshot hydration."""

    options: ProcessingOptions
    work_path: Path
    config_path: Path
    output_file: Path
    tables: dict[str, pd.DataFrame]
    source_inventory: Any
    profile_sheets: dict[str, pd.DataFrame]
    document_insights: Any
    recommendation_sheets: dict[str, pd.DataFrame]
    recommended_job_config: dict[str, Any]


def _prepare_input_context(
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
    config: Optional[Union[ProcessingOptions, dict[str, Any], str]],
) -> _PreparedInputContext:
    """Build deterministic profile/document/recommendation context once.

    Fresh LLM planning consumes this context, while PlanSnapshot hydration reuses the
    same preparation code and substitutes the already-approved JobConfig. Keeping this
    seam in one helper prevents confirmation/recovery from growing a second planner.
    """

    options = build_processing_options(config)
    work_path = Path(work_dir)
    config_path = work_path / "configs" / "recommended_cleaning_job.json"
    output_file = work_path / "output" / "final_result.xlsx"
    work_path.mkdir(parents=True, exist_ok=True)

    tables, source_inventory = read_tables_from_paths(
        input_paths,
        recursive=options.recursive,
    )
    if not tables:
        raise ValueError("No supported Excel/CSV tables were found in uploaded files")

    profile_sheets = profile_dataset(tables, source_inventory=source_inventory)
    document_insights = extract_document_insights(
        input_paths,
        tables=tables,
        source_inventory=source_inventory,
        recursive=options.recursive,
    )
    if not document_insights.document_inventory.empty:
        profile_sheets["supporting_docs"] = document_insights.document_inventory
    if not document_insights.field_definitions.empty:
        profile_sheets["document_field_definitions"] = document_insights.field_definitions
    if not document_insights.business_rules.empty:
        profile_sheets["document_business_rules"] = document_insights.business_rules
    if not document_insights.import_requirements.empty:
        profile_sheets["document_import_requirements"] = document_insights.import_requirements
    if not document_insights.template_validation.empty:
        profile_sheets["document_template_validation"] = document_insights.template_validation

    recommendation_sheets, recommended_job_config = build_recommendation_package(
        tables,
        profile_sheets,
        output_job_path=config_path,
        goal=options.goal,
    )
    recommended_job_config = _merge_document_rules(
        recommended_job_config,
        document_insights.generated_rules,
    )
    return _PreparedInputContext(
        options=options,
        work_path=work_path,
        config_path=config_path,
        output_file=output_file,
        tables=tables,
        source_inventory=source_inventory,
        profile_sheets=profile_sheets,
        document_insights=document_insights,
        recommendation_sheets=recommendation_sheets,
        recommended_job_config=recommended_job_config,
    )


def plan_input_paths(
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
    config: Optional[Union[ProcessingOptions, dict[str, Any], str]] = None,
) -> PlanResult:
    """Profile inputs, understand the goal, and prepare (but do not run) the job.

    This is the first half of the old ``process_input_paths``: everything up to and
    including writing the executable JobConfig. It also collects clarification
    questions (LLM-first, deterministic fallback) so a caller can decide whether to
    pause and ask before executing.
    """

    prepared = _prepare_input_context(input_paths, work_dir, config)
    options = prepared.options
    work_path = prepared.work_path
    config_path = prepared.config_path
    output_file = prepared.output_file
    tables = prepared.tables
    source_inventory = prepared.source_inventory
    profile_sheets = prepared.profile_sheets
    document_insights = prepared.document_insights
    recommendation_sheets = prepared.recommendation_sheets
    recommended_job_config = prepared.recommended_job_config
    # A recipe reuses only the typed intent. Every data-bound contract below is
    # rebuilt for this upload, which prevents stale paths, hashes and row budgets
    # from leaking across runs.
    recipe_goal_plan = copy.deepcopy(options.recipe_goal_plan)
    semantic_model = copy.deepcopy(options.semantic_model)
    if recipe_goal_plan:
        task = TaskSpec.model_validate(recipe_goal_plan.get("task_spec") or {})
        blocking = [slot for slot in task.missing_slots if slot.priority == "high"]
        if blocking:
            raise ValueError("Recipe 包含未解决的阻塞问题，不能直接执行。")
        recipe_goal_plan["task_spec"] = task.model_dump(mode="json")
        recipe_goal_plan["source"] = "recipe"
        recipe_goal_plan["goal"] = options.goal
        goal_plan = recipe_goal_plan
    elif semantic_model:
        goal_plan = resolve_semantic_goal(options.goal, tables, semantic_model)
    else:
        # LLM-first goal understanding: when an LLM is configured it reads the goal
        # against the real tables/columns and returns a validated semantic overlay;
        # otherwise this returns the deterministic interpretation unchanged.
        goal_plan = (
            understand_goal(
                options.goal,
                tables,
                column_profile=profile_sheets.get("column_profile"),
            )
            if options.goal
            else {}
        )
    clarification_questions: list[dict[str, Any]] = []
    llm_attempts = pd.DataFrame()
    if options.goal:
        recommended_job_config = apply_goal_to_job_config(
            recommended_job_config,
            goal=options.goal,
            tables=tables,
            goal_plan=goal_plan,
        )
        recommended_job_config = compile_task_spec_lookups(
            recommended_job_config,
            goal_plan,
            tables,
            profile_sheets,
        )
        if semantic_model:
            recommended_job_config = apply_semantic_relationships(
                recommended_job_config,
                goal_plan,
                tables,
                semantic_model,
            )
        task_baseline_config = copy.deepcopy(recommended_job_config)
        # Recipe plans already passed the semantic gate. Re-running the model would
        # both waste tokens and make the reusable contract non-repeatable.
        if not recipe_goal_plan and not semantic_model:
            # Two-layer agent: optionally let a configured LLM refine the plan. The
            # draft is validated against the whitelist/schema/profile and discarded
            # on failure, so it cannot reference unknown fields.
            recommended_job_config, llm_attempts = refine_config_with_llm(
                recommended_job_config,
                goal=options.goal,
                tables=tables,
                profile_sheets=profile_sheets,
                recommendation_sheets=recommendation_sheets,
            )
        recommended_job_config, goal_plan, _contract_error = (
            reconcile_planner_task_contract(
                recommended_job_config,
                task_baseline_config,
                goal_plan,
            )
        )
        validate_task_rule_preservation(
            goal_plan["task_spec"],
            task_baseline_config,
            recommended_job_config,
        )
        clarification_questions = collect_clarification_questions(
            goal=options.goal,
            goal_plan=goal_plan,
            job_config=recommended_job_config,
            profile_sheets=profile_sheets,
            recommendation_sheets=recommendation_sheets,
        )
    job_config = _prepare_job_config(
        recommended_job_config,
        output_file=output_file,
        options=options,
    )
    goal_plan = copy.deepcopy(goal_plan)
    goal_plan["impact_preview"] = build_task_impact_preview(
        goal_plan.get("task_spec") or {},
        job_config,
        tables,
    )
    output_spec = build_output_spec(
        goal_plan.get("task_spec") if goal_plan else None,
        include_audit=options.include_internal_sheets,
        report_formats=list(job_config.get("report", {}).get("formats", [])),
        formula_source_tables=_formula_source_tables(job_config),
        primary_fields=planned_primary_fields(job_config, tables),
        source_artifact_fields={
            name: [str(column) for column in frame.columns]
            for name, frame in tables.items()
        },
    )
    execution_plan = build_execution_plan(
        job_config,
        task_spec=goal_plan.get("task_spec") if goal_plan else None,
        output_spec=output_spec,
        impact_preview=goal_plan["impact_preview"],
        include_result_frames=options.include_result_frames,
    )
    if goal_plan.get("task_spec"):
        validate_task_spec_operations(goal_plan["task_spec"], job_config)
        validate_task_action_coverage(goal_plan["task_spec"], execution_plan)
    write_recommended_job_config(config_path, job_config)

    return PlanResult(
        options=options,
        work_path=work_path,
        config_path=config_path,
        output_file=output_file,
        tables=tables,
        source_inventory=source_inventory,
        profile_sheets=profile_sheets,
        document_insights=document_insights,
        recommendation_sheets=recommendation_sheets,
        job_config=job_config,
        execution_plan=execution_plan,
        output_spec=output_spec,
        goal_plan=goal_plan,
        clarification_questions=clarification_questions,
        llm_attempts=llm_attempts,
    )


def build_task_impact_preview(
    task_spec: TaskSpec | dict[str, Any] | None,
    job_config: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Estimate user-visible row and field impact without exporting artifacts."""

    task = (
        task_spec
        if isinstance(task_spec, TaskSpec)
        else TaskSpec.model_validate(task_spec)
        if task_spec
        else None
    )
    job = JobConfig.model_validate(job_config)
    base_table = job.base_table or next(iter(tables), "")
    base = tables.get(base_table)
    input_rows = len(base) if base is not None else 0
    output_rows = input_rows
    estimate_available = base is not None
    if base is not None:
        destructive = [
            formula
            for formula in job.formulas
            if formula.op in {"filter_rows", "dedupe"}
        ]
        try:
            output_rows = len(apply_formulas(base, destructive))
        except (KeyError, ValueError):
            estimate_available = False

    source_fields = {str(column) for column in base.columns} if base is not None else set()
    added_fields = {
        str(formula.output)
        for formula in job.formulas
        if formula.op not in {"filter_rows", "dedupe"}
        and formula.output not in source_fields
    }
    for lookup in [*([job.lookup] if job.lookup else []), *job.lookups]:
        for field in lookup.fields:
            output_field = str(lookup.field_aliases.get(field, field))
            if output_field in source_fields:
                output_field = f"{output_field}{lookup.suffix}"
            added_fields.add(output_field)
    affected_fields: set[str] = set()
    if task is not None:
        affected_fields.update(
            str(item.field) for item in task.target_fields if item.field
        )
        affected_fields.update(
            str(item.get("field"))
            for item in task.filters
            if item.get("field")
        )
        for item in task.deduplication:
            affected_fields.update(str(field) for field in item.get("fields", []))

    removed_rows = max(0, input_rows - output_rows) if estimate_available else None
    removal_ratio = (
        removed_rows / input_rows
        if estimate_available and removed_rows is not None and input_rows
        else 0.0 if estimate_available else None
    )
    return {
        "base_table": base_table,
        "input_rows": input_rows,
        "estimated_output_rows": output_rows if estimate_available else None,
        "estimated_removed_rows": removed_rows,
        "estimated_removal_ratio": removal_ratio,
        "estimate_available": estimate_available,
        "affected_fields": sorted(affected_fields),
        "added_fields": sorted(added_fields),
    }


def build_plan_snapshot(
    plan: PlanResult,
    input_paths: list[Union[str, Path]],
) -> PlanSnapshot:
    """Freeze the validated plan for confirmation, retry and recovery."""

    normalized_input_paths = [Path(path) for path in input_paths]
    schema_fingerprint = _schema_fingerprint(plan.tables)
    input_fingerprints = _input_fingerprints(
        normalized_input_paths,
        recursive=plan.options.recursive,
    )
    job_config = JobConfig.model_validate(plan.job_config).model_dump(mode="json")
    execution_plan = validate_execution_plan(plan.execution_plan)
    planner_metadata = _planner_metadata()
    goal_plan = copy.deepcopy(plan.goal_plan)
    task_spec_hash = _json_hash(goal_plan.get("task_spec") or {})
    plan_id = uuid.uuid4().hex
    return PlanSnapshot(
        plan_id=plan_id,
        plan_hash=_plan_hash(
            job_config,
            execution_plan,
            plan.output_spec,
            planner_metadata,
            schema_fingerprint,
            plan_id=plan_id,
            task_spec_hash=task_spec_hash,
            input_fingerprints=input_fingerprints,
            goal_plan=goal_plan,
            compiler_version=PLAN_COMPILER_VERSION,
        ),
        task_spec_hash=task_spec_hash,
        schema_fingerprint=schema_fingerprint,
        input_fingerprints=input_fingerprints,
        compiler_version=PLAN_COMPILER_VERSION,
        input_paths=normalized_input_paths,
        processing_options=plan.options.model_dump(mode="json"),
        job_config=job_config,
        execution_plan=execution_plan,
        output_spec=plan.output_spec,
        planner_metadata=planner_metadata,
        llm_usage=current_llm_usage(),
        goal_plan=goal_plan,
        clarification_questions=copy.deepcopy(plan.clarification_questions),
    )


def build_planning_response(
    plan: PlanResult,
    input_paths: list[Union[str, Path]],
) -> dict[str, Any]:
    """Project discovery/plan-only modes without executing or exporting data."""

    job_config = plan.job_config
    return {
        "status": "success",
        "input": {
            "file_count": len({str(Path(path)) for path in input_paths}),
            "table_count": len(plan.tables),
            "tables": _dataframe_records(plan.profile_sheets["table_profile"]),
        },
        "processing": {
            "base_table": job_config.get("base_table", ""),
            "lookup_count": len(job_config.get("lookups", []))
            + (1 if job_config.get("lookup") else 0),
            "formula_count": len(job_config.get("formulas", [])),
            "anomaly_rule_count": len(job_config.get("anomaly_rules", [])),
            "document_rule_count": len(plan.document_insights.generated_rules),
            "include_internal_sheets": False,
        },
        "goal_plan": plan.goal_plan,
        "execution_plan": plan.execution_plan.model_dump(mode="json"),
        "output_spec": plan.output_spec.model_dump(mode="json"),
        "clarification_questions": plan.clarification_questions,
        "counts": {},
        "quality_score": None,
        "charts": [],
        "summary": [],
        "result_rows": [],
        "review_items": [],
        "review_item_count": 0,
        "review_items_truncated": False,
        "result_row_count": 0,
        "files": {"job_config_path": str(plan.config_path)},
        "discovery": build_discovery_view(
            plan.profile_sheets,
            plan.document_insights,
            base_table=job_config.get("base_table", ""),
        ),
        "plan": build_plan_view(job_config, plan.document_insights),
        "execution_events": [],
        "llm_usage": current_llm_usage(),
    }


def hydrate_plan_snapshot(
    snapshot: PlanSnapshot | dict[str, Any],
    work_dir: Union[str, Path],
) -> PlanResult:
    """Rebuild deterministic execution context without asking the LLM to re-plan."""

    frozen = (
        snapshot
        if isinstance(snapshot, PlanSnapshot)
        else PlanSnapshot.model_validate(snapshot)
    )
    execution_plan = validate_execution_plan(
        frozen.execution_plan,
        allow_legacy_confirmation_policy=frozen.version in {3, 4},
    )
    if frozen.version in {4, 5, 6, 7}:
        current_task_spec_hash = _json_hash(frozen.goal_plan.get("task_spec") or {})
        if current_task_spec_hash != frozen.task_spec_hash:
            raise ValueError("计划快照校验失败：TaskSpec hash 不一致。")
        expected_hash = _plan_hash(
            frozen.job_config,
            execution_plan,
            frozen.output_spec,
            frozen.planner_metadata,
            frozen.schema_fingerprint,
            plan_id=frozen.plan_id,
            task_spec_hash=frozen.task_spec_hash,
            input_fingerprints=frozen.input_fingerprints,
            goal_plan=frozen.goal_plan,
            legacy_confirmation_policy=frozen.version == 4,
            compiler_version=(frozen.compiler_version if frozen.version == 7 else None),
        )
    else:
        expected_hash = _legacy_plan_hash(
            frozen.job_config,
            execution_plan,
            frozen.output_spec,
            frozen.planner_metadata,
            frozen.schema_fingerprint,
        )
    if frozen.plan_hash != expected_hash:
        raise ValueError("计划快照校验失败：plan hash 不一致。")

    options = ProcessingOptions.model_validate(frozen.processing_options)
    prepared = _prepare_input_context(
        list(frozen.input_paths),
        work_dir,
        options,
    )
    current_fingerprint = _schema_fingerprint(prepared.tables)
    if current_fingerprint != frozen.schema_fingerprint:
        raise ValueError("输入数据结构已变化，请重新生成并确认处理计划。")
    if frozen.version in {4, 5, 6, 7}:
        portable_fingerprints = all(
            not Path(item.path).is_absolute() for item in frozen.input_fingerprints
        )
        current_inputs = _input_fingerprints(
            list(frozen.input_paths),
            recursive=options.recursive,
            portable=portable_fingerprints,
        )
        if current_inputs != frozen.input_fingerprints:
            raise ValueError("输入文件内容已变化，请重新生成并确认处理计划。")

    job_config = copy.deepcopy(frozen.job_config)
    job_config.setdefault("export", {})
    job_config["export"]["output_file"] = str(prepared.output_file)
    validated_config = JobConfig.model_validate(job_config).model_dump(mode="json")
    write_recommended_job_config(prepared.config_path, validated_config)

    return PlanResult(
        options=options,
        work_path=prepared.work_path,
        config_path=prepared.config_path,
        output_file=prepared.output_file,
        tables=prepared.tables,
        source_inventory=prepared.source_inventory,
        profile_sheets=prepared.profile_sheets,
        document_insights=prepared.document_insights,
        recommendation_sheets=prepared.recommendation_sheets,
        job_config=validated_config,
        execution_plan=execution_plan,
        output_spec=frozen.output_spec,
        goal_plan=copy.deepcopy(frozen.goal_plan),
        clarification_questions=copy.deepcopy(frozen.clarification_questions),
    )


def _schema_fingerprint(tables: dict[str, pd.DataFrame]) -> str:
    schema = [
        {
            "table": str(table_name),
            "columns": [
                {"name": str(column), "dtype": str(frame[column].dtype)}
                for column in frame.columns
            ],
        }
        for table_name, frame in sorted(tables.items(), key=lambda item: str(item[0]))
    ]
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plan_hash(
    job_config: dict[str, Any],
    execution_plan: ExecutionPlan,
    output_spec: OutputSpec,
    planner_metadata: PlannerMetadata,
    schema_fingerprint: str,
    *,
    plan_id: str,
    task_spec_hash: str,
    input_fingerprints: list[InputFingerprint],
    goal_plan: dict[str, Any],
    legacy_confirmation_policy: bool = False,
    compiler_version: str | None = None,
) -> str:
    payload = {
        "plan_id": plan_id,
        "job_config": job_config,
        "execution_plan": _execution_plan_hash_payload(
            execution_plan,
            legacy_confirmation_policy=legacy_confirmation_policy,
        ),
        "output_spec": output_spec.model_dump(mode="json"),
        "planner_metadata": planner_metadata.model_dump(mode="json"),
        "goal_plan": goal_plan,
        "task_spec_hash": task_spec_hash,
        "schema_fingerprint": schema_fingerprint,
        "input_fingerprints": [
            fingerprint.model_dump(mode="json") for fingerprint in input_fingerprints
        ],
    }
    if compiler_version is not None:
        payload["compiler_version"] = compiler_version
    return _json_hash(payload)


def _legacy_plan_hash(
    job_config: dict[str, Any],
    execution_plan: ExecutionPlan,
    output_spec: OutputSpec,
    planner_metadata: PlannerMetadata,
    schema_fingerprint: str,
) -> str:
    """Verify snapshots written before intent and input content joined the envelope."""

    return _json_hash(
        {
            "job_config": job_config,
            "execution_plan": _execution_plan_hash_payload(
                execution_plan,
                legacy_confirmation_policy=True,
            ),
            "output_spec": output_spec.model_dump(mode="json"),
            "planner_metadata": planner_metadata.model_dump(mode="json"),
            "schema_fingerprint": schema_fingerprint,
        }
    )


def _execution_plan_hash_payload(
    execution_plan: ExecutionPlan,
    *,
    legacy_confirmation_policy: bool,
) -> dict[str, Any]:
    payload = execution_plan.model_dump(mode="json")
    if legacy_confirmation_policy:
        for step in payload.get("steps", []):
            step.pop("confirmation_reasons", None)
            step.pop("impact_estimate", None)
    return payload


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _input_fingerprints(
    input_paths: list[Path],
    *,
    recursive: bool,
    portable: bool = True,
) -> list[InputFingerprint]:
    """Hash every input that can affect planning or deterministic execution."""

    from data_agent.tools.document_rules import discover_document_files
    from data_agent.tools.excel_reader import discover_input_files

    files = sorted(
        set(discover_input_files(input_paths, recursive=recursive))
        | set(discover_document_files(input_paths, recursive=recursive)),
        key=lambda path: (path.name.casefold(), str(path.resolve())),
    )
    fingerprints: list[InputFingerprint] = []
    for index, path in enumerate(files):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        fingerprints.append(
            InputFingerprint(
                path=f"{index}:{path.name}" if portable else str(path.resolve()),
                size_bytes=path.stat().st_size,
                sha256=digest.hexdigest(),
            )
        )
    return fingerprints


def _planner_metadata() -> PlannerMetadata:
    llm_config = load_llm_config()
    return PlannerMetadata(
        goal_prompt_version=GOAL_PROMPT_VERSION,
        planner_prompt_version=PLANNER_PROMPT_VERSION,
        model=llm_config.model if llm_config else "deterministic",
        capability_registry_hash=get_capability_registry().fingerprint(),
    )


def execute_planned_job(
    plan: PlanResult,
    input_paths: list[Union[str, Path]],
) -> dict[str, Any]:
    """Run the prepared job and assemble the business response.

    This is the second half of the old ``process_input_paths``: it executes the
    JobConfig produced by :func:`plan_input_paths` and never re-plans, so a resumed
    job runs exactly the plan the user saw.
    """

    options = plan.options
    job_config = plan.job_config
    tables = plan.tables
    source_inventory = plan.source_inventory
    profile_sheets = plan.profile_sheets
    document_insights = plan.document_insights
    recommendation_sheets = plan.recommendation_sheets
    config_path = plan.config_path
    goal_plan = plan.goal_plan

    execution_started = perf_counter()
    validate_job_config_binding(plan.execution_plan, job_config)
    telemetry = ExecutionRecorder(plan.execution_plan)
    # Template validation is computed before execution so the executor can emit the
    # declared 可导入数据 sheet itself; every entry point then gets the same workbook.
    pre_execution_fields = data_table_fields(tables, source_inventory)
    # Document extraction already ran during planning; record it as observed (zero
    # extracted rules is a real result, not a skipped step).
    if plan.execution_plan.version == 1 or any(
        step.capability_id == "extract_document_rules"
        for step in plan.execution_plan.steps
    ):
        telemetry.record(
            StageRecord(
                capability_id="extract_document_rules",
                output_rows=len(document_insights.generated_rules),
                metrics={
                    "document_count": int(len(document_insights.document_inventory))
                },
            )
        )
    result = run_job(
        JobConfig.model_validate(job_config),
        output_spec=plan.output_spec,
        task_spec=goal_plan.get("task_spec"),
        import_requirements=document_insights.import_requirements,
        template_validation=build_template_validation(
            document_insights.import_requirements,
            pre_execution_fields,
        ),
        # Planning already profiled exactly these tables; without this the executor
        # re-ran the whole profiling + relationship scan on every job.
        profile_sheets=profile_sheets,
        recorder=telemetry,
    )
    # Use the in-memory result frames instead of re-reading the workbook from disk.
    business_summary = result.business_summary
    business_result = result.business_result
    template_validation = build_template_validation(
        document_insights.import_requirements,
        _fields_from_frame(business_result) | pre_execution_fields,
    )

    delivery_result = delivery_result_sheet(
        business_result,
        {str(column) for frame in tables.values() for column in frame.columns},
    )
    result_rows = _limited_records(delivery_result, options.result_limit)
    review_item_count = int(len(review_rows(business_result)))
    all_review_items = _review_items(business_result, 0)
    review_items = (
        all_review_items
        if options.result_limit == 0
        else all_review_items[: options.result_limit]
    )
    review_catalog_path = plan.work_path / "review_items.json"
    review_catalog_path.write_text(
        json.dumps(all_review_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Persist the pre-split result so saved review decisions can later be applied to
    # a new workbook without re-planning or re-executing anything. No-op for a run
    # with nothing to review.
    write_result_snapshot(plan.work_path, business_result)
    total_records = _summary_metric(result.summary, "total")
    # One scoring口径 for every entry point: the dirty-data axis comes from the same
    # in-executor run the CLI reads, so Web and CLI cannot report different scores for
    # the same input and goal.
    # 用户要求了、但计划确实没做的动作。它是评分里最重的一条轴，也会写进结论。
    unmet = unmet_task_actions(goal_plan["task_spec"], plan.execution_plan) if (
        goal_plan.get("task_spec")
    ) else []
    with telemetry.stage("score_quality", input_rows=total_records) as stage:
        quality_result = compute_quality_score(
            total_records=total_records,
            usable_records=result.cleaned_count,
            exception_records=result.abnormal_count,
            exception_summary=exception_summary_from_business_summary(business_summary),
            dirty_issue_summary=result.dirty_issue_summary,
            relationship_candidates=profile_sheets.get("relationship_candidates"),
            unmet_actions=unmet,
        )
        stage.metrics["score"] = quality_result.score
        stage.metrics["unmet_actions"] = unmet
    generated_charts = []
    final_result = delivered_rows(business_result)
    # A reported issue may remain in the primary result when the user only asked to
    # list or mark it. Business counts use the rows actually withheld; the review API
    # still uses review_rows() so those reported rows remain actionable.
    needs_review = withheld_rows(business_result)
    exception_summary = exception_summary_from_business_summary(business_summary)
    exception_impact = pd.DataFrame()
    analysis_result = None
    # Charts, the report and the delivery layer all need the analysis pass, so run it
    # once when any of them is in scope rather than only for charts (which left the
    # report with a four-number stub while the CLI produced a full conclusion).
    if (
        plan.output_spec.charts
        or plan.output_spec.report.enabled
        or options.include_result_frames
    ):
        exception_impact = analyze_exception_impact(
            exception_summary,
            result.dirty_issue_summary,
        )
        with telemetry.stage("summarize", input_rows=len(business_result)) as stage:
            analysis_result = analyze_business_data(
                business_result=business_result,
                final_result=final_result,
                needs_review=needs_review,
                data_quality_summary=quality_result.summary,
                exception_impact=exception_impact,
                dirty_issue_summary=result.dirty_issue_summary,
                relationship_candidates=profile_sheets.get("relationship_candidates"),
                goal=options.goal,
            )
            stage.output_rows = len(analysis_result.summary)
    if plan.output_spec.charts:
        with telemetry.stage("generate_chart") as stage:
            generated_charts = generate_business_charts(
                analysis_result.details,
                charts_dir=plan.output_file.parent / "charts",
                output_dir=plan.work_path,
                requested_chart_ids={
                    chart.chart_id for chart in plan.output_spec.charts
                },
            )
            stage.metrics["chart_count"] = len(generated_charts)
    lint_output(
        plan.output_spec,
        chart_ids=[chart.path.stem for chart in generated_charts],
    )
    response = {
        "status": "success",
        "input": {
            "file_count": len({str(Path(path)) for path in input_paths}),
            "table_count": len(tables),
            "tables": _dataframe_records(profile_sheets["table_profile"]),
        },
        "processing": {
            "base_table": job_config.get("base_table", ""),
            "lookup_count": len(job_config.get("lookups", []))
            + (1 if job_config.get("lookup") else 0),
            "formula_count": len(job_config.get("formulas", [])),
            "anomaly_rule_count": len(job_config.get("anomaly_rules", [])),
            "document_rule_count": len(document_insights.generated_rules),
            "import_requirement_count": len(document_insights.import_requirements),
            "missing_import_field_count": missing_template_field_count(template_validation),
            "include_internal_sheets": job_config["export"]["include_internal_sheets"],
        },
        "goal_plan": goal_plan,
        "execution_plan": plan.execution_plan.model_dump(mode="json"),
        "output_spec": plan.output_spec.model_dump(mode="json"),
        "clarification_questions": plan.clarification_questions,
        "counts": {
            "total": total_records,
            "valid": result.cleaned_count,
            "abnormal": result.abnormal_count,
        },
        "quality_score": quality_result.score,
        "charts": [
            {
                "chart_id": chart.path.stem,
                "title": chart.title,
                "file_name": chart.path.name,
                "chart_type": chart.chart_type,
                "description": chart.description,
            }
            for chart in generated_charts
        ],
        "summary": _dataframe_records(business_summary),
        "result_rows": result_rows,
        "review_items": review_items,
        "review_item_count": review_item_count,
        "review_items_truncated": len(review_items) < review_item_count,
        "review_catalog_path": str(review_catalog_path),
        "result_row_count": len(delivery_result),
        "result_limit": options.result_limit,
        "result_truncated": (
            options.result_limit > 0 and len(delivery_result) > options.result_limit
        ),
        "files": {
            "excel_path": str(result.output_file),
            "job_config_path": str(config_path),
            **(
                {"internal_workbook_path": str(result.internal_workbook)}
                if result.internal_workbook
                else {}
            ),
        },
    }
    for chart in generated_charts:
        response["files"][f"chart_{chart.path.stem}"] = str(
            plan.work_path / chart.path
        )
    artifact_names = ["final_result.xlsx"]
    if plan.output_spec.report.enabled:
        report_refs = [
            OutputFileRef(
                name=(
                    "business_answer.md"
                    if report_format == "markdown"
                    else "business_answer.html"
                ),
                path=str(
                    plan.output_file.parent
                    / (
                        "business_answer.md"
                        if report_format == "markdown"
                        else "business_answer.html"
                    )
                ),
                description="按 OutputSpec 生成的任务结果报告",
            )
            for report_format in plan.output_spec.report.formats
        ]
        artifact_names.extend(item.name for item in report_refs)
        task_actions = set(goal_plan.get("task_spec", {}).get("actions", []))
        # Same conclusion builder the CLI uses, so the report a Web user downloads is
        # the report a CLI user downloads for the same job.
        report_answer = build_business_answer(
            goal=options.goal,
            input_tables=response["input"]["tables"],
            total_records=total_records,
            summary=business_summary,
            final_result=final_result,
            needs_review=needs_review,
            data_quality_summary=quality_result.summary,
            exception_summary=exception_summary,
            exception_impact=exception_impact,
            dirty_issue_summary=result.dirty_issue_summary,
            analysis_summary=(
                analysis_result.summary
                if analysis_result is not None and task_actions & {"analyze", "chart"}
                else pd.DataFrame()
            ),
            analysis_findings=(
                analysis_result.findings
                if analysis_result is not None
                and task_actions & {"analyze", "chart", "review"}
                else []
            ),
            document_rule_count=len(document_insights.generated_rules),
            missing_template_field_count=missing_template_field_count(template_validation),
            recommended_actions=recommended_actions_from_impact(exception_impact),
            charts=[
                ChartRef(
                    title=chart.title,
                    path=str(
                        (plan.work_path / chart.path).relative_to(
                            plan.output_file.parent
                        )
                    ),
                    chart_type=chart.chart_type,
                    description=chart.description,
                )
                for chart in generated_charts
            ],
            output_files=[
                OutputFileRef(
                    name="final_result.xlsx",
                    path=str(result.output_file),
                    description="任务处理结果",
                ),
                *report_refs,
            ],
        )
        with telemetry.stage("export_report") as stage:
            report_paths = write_business_report(
                report_answer,
                plan.output_file.parent,
                formats=plan.output_spec.report.formats,
            )
            stage.metrics["formats"] = list(plan.output_spec.report.formats)
        for report_format, report_path in report_paths.items():
            response["files"][f"report_{report_format}"] = str(report_path)
    lint_output(
        plan.output_spec,
        artifact_names=artifact_names,
        primary_artifact_name="final_result.xlsx",
    )
    response["execution_duration_ms"] = round(
        (perf_counter() - execution_started) * 1000,
        2,
    )
    # Observed telemetry, bound to the plan steps it belongs to. Steps with no
    # recorded stage are reported as skipped rather than as invented successes.
    response["execution_events"] = [
        event.model_dump(mode="json")
        for event in bind_execution_events(plan.execution_plan, telemetry.stages)
    ]
    response["llm_usage"] = current_llm_usage()
    # Long-term memory (opt-in via DATA_AGENT_MEMORY_ENABLED). Recorded here rather
    # than in the delivery layer so console jobs accumulate memory too — previously
    # only CLI runs did, while the planner fed the memory summary to both.
    record_task(job_config, goal=options.goal)

    if options.include_job_config:
        response["job_config"] = job_config
    if options.include_diagnostics:
        response["discovery"] = build_discovery_view(
            profile_sheets,
            document_insights,
            base_table=job_config.get("base_table", ""),
        )
        response["plan"] = build_plan_view(job_config, document_insights)
        response["diagnostics"] = {
            "recommended_summary": _dataframe_records(
                recommendation_sheets["recommendation_summary"]
            ),
            "relationship_candidates": _dataframe_records(
                profile_sheets["relationship_candidates"]
            ),
            "document_business_rules": _dataframe_records(
                document_insights.business_rules
            ),
            "template_validation": _dataframe_records(template_validation),
        }
    if options.include_excel_base64:
        response["files"]["excel_base64"] = base64.b64encode(
            result.output_file.read_bytes()
        ).decode("ascii")
    if options.include_result_frames:
        # Everything delivery needs from this run, so it never re-derives (and never
        # re-diverges from) the executor's own numbers.
        response["result_frames"] = {
            "business_summary": business_summary,
            "business_result": business_result,
            "dirty_issue_summary": result.dirty_issue_summary,
            "dirty_record_issues": result.dirty_record_issues,
            "dirty_fix_audit": result.dirty_fix_audit,
            "quality_summary": quality_result.summary,
            "quality_deductions": quality_result.deductions,
            "exception_summary": exception_summary,
            "exception_impact": exception_impact,
            "template_validation": template_validation,
            "analysis_summary": (
                analysis_result.summary if analysis_result is not None else pd.DataFrame()
            ),
            "analysis_findings": (
                list(analysis_result.findings) if analysis_result is not None else []
            ),
            "analysis_details": (
                analysis_result.details if analysis_result is not None else None
            ),
            "relationship_candidates": profile_sheets.get(
                "relationship_candidates", pd.DataFrame()
            ),
            "unmet_actions": list(unmet),
        }
    if options.include_quality_frames:
        # The reflection loop needs the run's quality signals to score it and to
        # feed the model a targeted diagnosis. Kept out of the API JSON unless asked.
        response["job_config"] = job_config
        response["quality_frames"] = {
            "summary": quality_result.summary,
            "deductions": quality_result.deductions,
        }
        response["relationship_candidates"] = profile_sheets.get(
            "relationship_candidates", pd.DataFrame()
        )

    return response


def _review_items(
    business_result: pd.DataFrame,
    limit: int,
) -> list[dict[str, Any]]:
    if business_result.empty or "处理状态" not in business_result.columns:
        return []
    review_frame = review_rows(business_result)
    if limit > 0:
        review_frame = review_frame.head(limit)
    items: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, row in review_frame.iterrows():
        source_row = row.get("源行号", index)
        base_id = f"ROW-{source_row}"
        row_id = base_id
        suffix = 2
        while row_id in used_ids:
            row_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(row_id)
        items.append(
            {
                "id": row_id,
                # Positional key back into the persisted result snapshot. Review ids
                # are not unique per source row, so re-materialisation keys on this.
                "result_index": int(index),
                "issue_type": str(row.get("问题类型") or "需要人工确认"),
                "severity": "medium",
                "source_row": str(source_row),
                "affected_field": str(row.get("影响字段") or ""),
                "current_value": (
                    "" if pd.isna(row.get("当前值")) else str(row.get("当前值") or "")
                ),
                "recommended_action": str(
                    row.get("建议处理") or "核对原始记录和规则命中原因后处理"
                ),
            }
        )
    return items


class PlanNotConfirmedError(ValueError):
    """Raised when the layered risk policy requires approval before execution."""


DestructivePlanNotConfirmedError = PlanNotConfirmedError


def require_plan_confirmation(plan: PlanResult, *, confirmed: bool) -> None:
    """Apply the same risk-escalation gate for the CLI, sync API and async API.

    The async Job API pauses on ``ExecutionPlan.requires_confirmation`` and shows the
    reasons. Callers of non-interactive entry points pass ``confirmed=True`` once the
    user approved them (``data-agent answer --yes`` / the backward-compatible
    ``"confirm_destructive": true`` setting).
    """

    if confirmed or not plan.execution_plan.requires_confirmation:
        return
    impact = plan.goal_plan.get("impact_preview") or {}
    reason_codes = {
        reason
        for step in plan.execution_plan.steps
        for reason in step.confirmation_reasons
    }
    details: list[str] = []
    if "missing_authorization" in reason_codes:
        details.append("删行规则缺少可引用的用户原话")
    if "impact_unknown" in reason_codes:
        details.append("暂时无法可靠估算会影响多少行")
    if "high_removal_ratio" in reason_codes:
        removed = impact.get("estimated_removed_rows")
        input_rows = impact.get("input_rows")
        ratio = impact.get("estimated_removal_ratio")
        if isinstance(removed, int) and isinstance(input_rows, int):
            percentage = f"{float(ratio or 0) * 100:.1f}%"
            details.append(
                f"预计移除 {removed} 行（共 {input_rows} 行，约 {percentage}）"
            )
        else:
            details.append("预计移除比例达到高影响阈值")
    if "fuzzy_matching" in reason_codes:
        details.append("关联步骤使用模糊匹配，可能把相似但不同的记录连在一起")
    summary = "；".join(details) or "计划触发了风险复核规则"
    raise PlanNotConfirmedError(
        f"本次处理需要先确认：{summary}。"
        "确认后重新执行：CLI 加 --yes，同步接口在 config 中传 "
        "confirm_destructive=true，或改用异步任务接口在页面上确认。"
    )


def require_destructive_confirmation(plan: PlanResult, *, confirmed: bool) -> None:
    """Backward-compatible name for :func:`require_plan_confirmation`."""

    require_plan_confirmation(plan, confirmed=confirmed)


def process_input_paths(
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
    config: Optional[Union[ProcessingOptions, dict[str, Any], str]] = None,
) -> dict[str, Any]:
    """Profile uploaded files, recommend a job, run cleaning, and return business results.

    A thin composition of :func:`plan_input_paths` + :func:`execute_planned_job`, so
    every existing caller (API sync endpoint, reflection wrapper, CLI) keeps the same
    single-call behaviour while the async path can pause between the two halves.
    """

    plan = plan_input_paths(input_paths, work_dir, config=config)
    return execute_planned_job(plan, input_paths)


class _ApiScoredRun:
    """A :class:`~data_agent.agent.reflection.ScoredRun` view over one API run.

    Wraps the dict returned by :func:`process_input_paths` so the shared bounded
    reflection loop can score API runs and reason about their weaknesses exactly
    the way the CLI delivery path does, without the API response ever having to
    change shape.
    """

    __slots__ = ("response", "work_dir")

    def __init__(self, response: dict[str, Any], work_dir: Path) -> None:
        self.response = response
        self.work_dir = work_dir

    @property
    def score(self) -> int:
        return int(self.response.get("quality_score") or 0)

    @property
    def job_config(self) -> dict[str, Any]:
        return self.response.get("job_config", {})

    @property
    def quality_summary(self) -> pd.DataFrame:
        return self.response.get("quality_frames", {}).get("summary", pd.DataFrame())

    @property
    def quality_deductions(self) -> pd.DataFrame:
        return self.response.get("quality_frames", {}).get("deductions", pd.DataFrame())

    @property
    def match_rate_value(self) -> float:
        # Mean lookup match rate across relationship candidates; higher is better.
        # Used only as a tie-breaker when two runs score equally.
        candidates = self.response.get("relationship_candidates")
        if not isinstance(candidates, pd.DataFrame) or candidates.empty:
            return 0.0
        if "left_match_rate" not in candidates.columns:
            return 0.0
        rates = pd.to_numeric(candidates["left_match_rate"], errors="coerce").dropna()
        return float(rates.mean()) if not rates.empty else 0.0

    @property
    def missing_field_count(self) -> int:
        return int(self.response.get("processing", {}).get("missing_import_field_count", 0) or 0)


# Keys the reflection wrapper attaches for scoring but that must not leak into the
# API JSON response the console reads back.
_REFLECTION_INTERNAL_KEYS = ("quality_frames", "relationship_candidates")


def process_input_paths_with_reflection(
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
    config: Optional[Union[ProcessingOptions, dict[str, Any], str]] = None,
    *,
    confirm_destructive: bool = False,
) -> dict[str, Any]:
    """Plan once, apply the shared risk gate, then run the reflection loop."""

    work_path = Path(work_dir)
    plan = plan_input_paths(input_paths, work_path, config=config)
    require_plan_confirmation(plan, confirmed=confirm_destructive)
    return process_planned_job_with_reflection(plan, input_paths, work_path)


def process_planned_job_with_reflection(
    plan: PlanResult,
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
) -> dict[str, Any]:
    """Execute one prepared plan, optionally refining it without re-planning.

    The base run and every reflection candidate reuse the same profiled tables,
    document context and approved JobConfig. Reflection may apply only validated
    structural overrides; it never calls goal understanding or the planner again.
    """

    work_path = Path(work_dir)
    settings = load_reflection_settings()
    llm_planner = build_default_llm_planner()

    if not (settings.enabled and settings.max_rounds > 0 and llm_planner is not None):
        return execute_planned_job(plan, input_paths)

    def _execute_and_score(run_config: dict[str, Any], attempt_dir: Path) -> _ApiScoredRun:
        attempt_plan = _plan_for_attempt(
            plan,
            attempt_dir,
            overrides=dict(run_config.get("job_overrides", {})),
        )
        response = execute_planned_job(attempt_plan, input_paths)
        return _ApiScoredRun(response, attempt_dir)

    relationship_candidates = plan.profile_sheets.get(
        "relationship_candidates",
        pd.DataFrame(),
    )
    best, attempts = run_with_bounded_reflection(
        _execute_and_score,
        base_config={},
        output_path=work_path,
        validation_tables=plan.tables,
        relationship_candidates=relationship_candidates,
        goal=plan.options.goal,
        settings=settings,
        llm_planner=llm_planner,
    )

    response = _promote_winning_run(best, work_path)
    if attempts:
        response["reflection_attempts"] = attempts
    response["llm_usage"] = current_llm_usage()
    _remove_attempt_directories(work_path)
    return response


def _remove_attempt_directories(work_path: Path) -> None:
    """Delete the reflection attempts once the winner has been promoted.

    Each attempt holds a full copy of every artifact, so leaving them behind both
    confuses anyone browsing the output folder and grows the job directory linearly
    with the number of rounds.
    """

    for directory in work_path.glob(f"{ATTEMPT_DIR_PREFIX}*"):
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)


def _plan_for_attempt(
    plan: PlanResult,
    attempt_dir: Path,
    *,
    overrides: dict[str, Any],
) -> PlanResult:
    """Clone a plan into an isolated attempt directory with validated overrides."""

    job_config = copy.deepcopy(plan.job_config)
    if overrides:
        job_config = deep_merge(job_config, overrides)
    output_file = attempt_dir / "output" / "final_result.xlsx"
    config_path = attempt_dir / "configs" / "recommended_cleaning_job.json"
    job_config.setdefault("export", {})
    job_config["export"]["output_file"] = str(output_file)
    validated = JobConfig.model_validate(job_config).model_dump(mode="json")
    task_spec = plan.goal_plan.get("task_spec")
    attempt_goal_plan = copy.deepcopy(plan.goal_plan)
    attempt_goal_plan["impact_preview"] = build_task_impact_preview(
        task_spec or {},
        validated,
        plan.tables,
    )
    execution_plan = build_execution_plan(
        validated,
        task_spec=task_spec,
        output_spec=plan.output_spec,
        impact_preview=attempt_goal_plan["impact_preview"],
        include_result_frames=plan.options.include_result_frames,
    )
    if task_spec:
        validate_task_rule_preservation(task_spec, plan.job_config, validated)
        validate_task_spec_operations(task_spec, validated)
        validate_task_action_coverage(task_spec, execution_plan)
    write_recommended_job_config(config_path, validated)
    options = plan.options.model_copy(update={"include_quality_frames": True})
    return PlanResult(
        options=options,
        work_path=attempt_dir,
        config_path=config_path,
        output_file=output_file,
        tables=plan.tables,
        source_inventory=plan.source_inventory,
        profile_sheets=plan.profile_sheets,
        document_insights=plan.document_insights,
        recommendation_sheets=plan.recommendation_sheets,
        job_config=validated,
        execution_plan=execution_plan,
        output_spec=plan.output_spec,
        goal_plan=attempt_goal_plan,
        clarification_questions=plan.clarification_questions,
    )


def _promote_winning_run(best: _ApiScoredRun, work_path: Path) -> dict[str, Any]:
    """Move the winning attempt's artifacts to the canonical job work dir.

    The reflection loop runs each attempt in its own sub-directory. The API
    download endpoints look for the workbook and config at fixed paths under the
    job dir, so copy the winning run's outputs there and rewrite the response
    paths to match. Then strip the scoring-only internal keys.
    """

    response = dict(best.response)
    winner_dir = best.work_dir
    if winner_dir.resolve() != work_path.resolve():
        promoted_files = (
            Path("output") / "final_result.xlsx",
            Path("output") / "business_answer.md",
            Path("output") / "business_answer.html",
            Path("configs") / "recommended_cleaning_job.json",
            Path("review_items.json"),
            # The winning attempt's result snapshot must travel with it, or review
            # write-back would re-materialise from a losing run's rows.
            snapshot_relative_path(),
        )
        for rel in promoted_files:
            src = winner_dir / rel
            dest = work_path / rel
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            else:
                # Never leave a previous round's artifact standing in for one the
                # winning attempt deliberately did not produce.
                dest.unlink(missing_ok=True)
        winner_charts = winner_dir / "output" / "charts"
        canonical_charts = work_path / "output" / "charts"
        if winner_charts.exists():
            if canonical_charts.exists():
                shutil.rmtree(canonical_charts)
            shutil.copytree(winner_charts, canonical_charts)
        files = dict(response.get("files", {}))
        canonical_excel = work_path / "output" / "final_result.xlsx"
        canonical_config = work_path / "configs" / "recommended_cleaning_job.json"
        if canonical_excel.exists():
            files["excel_path"] = str(canonical_excel)
        if canonical_config.exists():
            files["job_config_path"] = str(canonical_config)
        for key, value in list(files.items()):
            if key.startswith("chart_") and isinstance(value, str):
                chart_path = canonical_charts / Path(value).name
                if chart_path.exists():
                    files[key] = str(chart_path)
            if key.startswith("report_") and isinstance(value, str):
                report_path = work_path / "output" / Path(value).name
                if report_path.exists():
                    files[key] = str(report_path)
        response["files"] = files
        canonical_review_catalog = work_path / "review_items.json"
        if canonical_review_catalog.exists():
            response["review_catalog_path"] = str(canonical_review_catalog)

    for key in _REFLECTION_INTERNAL_KEYS:
        response.pop(key, None)
    return response


def build_processing_options(
    config: Optional[Union[ProcessingOptions, dict[str, Any], str]] = None,
) -> ProcessingOptions:
    if config is None:
        return ProcessingOptions()
    if isinstance(config, ProcessingOptions):
        return config
    if isinstance(config, str):
        payload = json.loads(config) if config.strip() else {}
    else:
        payload = dict(config)

    if not isinstance(payload, dict):
        raise ValueError("Processing config must be a JSON object")

    if "return_excel_base64" in payload and "include_excel_base64" not in payload:
        payload["include_excel_base64"] = payload["return_excel_base64"]

    direct_job_overrides = {
        key: payload.pop(key) for key in list(payload) if key in JOB_CONFIG_KEYS
    }
    explicit_overrides = payload.get("job_overrides") or {}
    if not isinstance(explicit_overrides, dict):
        raise ValueError("job_overrides must be a JSON object")
    metadata = _extract_goal_metadata(payload, explicit_overrides)
    explicit_overrides = {
        key: value for key, value in explicit_overrides.items() if key in JOB_CONFIG_KEYS
    }
    if direct_job_overrides:
        payload["job_overrides"] = deep_merge(direct_job_overrides, explicit_overrides)
    else:
        payload["job_overrides"] = explicit_overrides
    payload.update(metadata)

    return ProcessingOptions.model_validate(payload)


def _extract_goal_metadata(
    payload: dict[str, Any],
    job_overrides: dict[str, Any],
) -> dict[str, str]:
    goal = str(job_overrides.get("goal") or payload.get("goal") or "").strip()
    mode = str(job_overrides.get("mode") or payload.get("mode") or "answer").strip() or "answer"
    output_mode = (
        str(job_overrides.get("output_mode") or payload.get("output_mode") or "business_answer")
        .strip()
        or "business_answer"
    )
    return {"goal": goal, "mode": mode, "output_mode": output_mode}


def _prepare_job_config(
    recommended_job_config: dict[str, Any],
    output_file: Path,
    options: ProcessingOptions,
) -> dict[str, Any]:
    job_config = copy.deepcopy(recommended_job_config)
    generated_sources = copy.deepcopy(job_config.get("sources", {}))

    if options.job_overrides:
        job_config = deep_merge(job_config, options.job_overrides)

    # API execution must always run against the uploaded files and write into this job folder.
    job_config["sources"] = generated_sources
    if options.goal:
        job_config["goal"] = options.goal
    job_config["output_mode"] = options.output_mode
    job_config.setdefault("export", {})
    job_config["export"]["output_file"] = str(output_file)
    job_config["export"]["include_internal_sheets"] = bool(
        options.include_internal_sheets
        or job_config["export"].get("include_internal_sheets", False)
    )
    job_config["export"]["include_source_tables"] = bool(
        job_config["export"].get("include_source_tables", False)
    )
    return job_config


def _merge_document_rules(
    job_config: dict[str, Any],
    document_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    if not document_rules:
        return job_config
    merged = copy.deepcopy(job_config)
    merged.setdefault("anomaly_rules", [])
    existing_rules = merged["anomaly_rules"]
    existing_names = {rule.get("name") for rule in existing_rules}
    # Index existing rules by the *condition* they evaluate, not just their name.
    # A profile-derived blank rule (e.g. supplier_id_null_values) and a document
    # rule (doc_rule_supplier_id_required) can carry different names yet check the
    # exact same condition; without fingerprint dedup the same blank cell is
    # flagged twice and counted as two "critical" issue types in the score.
    existing_by_condition = {
        _rule_condition_fingerprint(rule): rule for rule in existing_rules
    }
    for rule in document_rules:
        fingerprint = _rule_condition_fingerprint(rule)
        duplicate = existing_by_condition.get(fingerprint)
        if duplicate is not None:
            # Document rules are authoritative business requirements: keep a single
            # rule but never let dedup weaken the severity that either side asked for.
            if _severity_rank(rule.get("severity")) > _severity_rank(duplicate.get("severity")):
                duplicate["severity"] = rule.get("severity")
            continue
        if rule.get("name") in existing_names:
            continue
        stripped = _strip_rule_source(rule)
        existing_rules.append(stripped)
        existing_names.add(stripped.get("name"))
        existing_by_condition[fingerprint] = stripped
    return merged


def _rule_condition_fingerprint(rule: dict[str, Any]) -> tuple[Any, ...]:
    condition = rule.get("condition") or {}
    values = condition.get("values") or []
    return (
        str(condition.get("field", "")),
        str(condition.get("op", "")),
        repr(condition.get("value")),
        repr(tuple(values)),
        bool(condition.get("case_sensitive", False)),
    )


def _severity_rank(severity: Any) -> int:
    return {"error": 2, "warn": 1}.get(str(severity), 0)


def _strip_rule_source(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in rule.items() if key != "source"}


def _fields_from_frame(df: pd.DataFrame) -> set[str]:
    return {str(column) for column in df.columns}


def build_discovery_view(
    profile_sheets: dict[str, pd.DataFrame],
    document_insights: Any,
    base_table: str,
) -> dict[str, Any]:
    """Assemble a business-facing discovery summary the console can render directly.

    Reuses the deterministic profiler output (table/key/relationship candidates and
    quality issues) so the Discovery page shows the same evidence the planner used,
    instead of front-end mock data.
    """

    table_profile = profile_sheets.get("table_profile", pd.DataFrame())
    key_candidates = profile_sheets.get("key_candidates", pd.DataFrame())
    relationships = profile_sheets.get("relationship_candidates", pd.DataFrame())
    profile_issues = profile_sheets.get("profile_issues", pd.DataFrame())

    tables = [
        {
            "name": str(row.get("table", "")),
            "rows": _int_or_zero(row.get("row_count")),
            "columns": _int_or_zero(row.get("column_count")),
            "role": "主表" if str(row.get("table", "")) == base_table else "关联表候选",
        }
        for row in frame_records(table_profile)
    ]

    keys = [
        {
            "table": str(row.get("table", "")),
            "field": str(row.get("field", "")),
            "confidence": _confidence_label(row.get("recommendation")),
            "uniqueness": _percent(row.get("uniqueness_rate")),
        }
        for row in frame_records(key_candidates)
        if str(row.get("recommendation", "")) != "low_confidence"
    ][:12]

    relationship_rows = [
        {
            "from": f"{row.get('left_table', '')}.{row.get('left_field', '')}",
            "to": f"{row.get('right_table', '')}.{row.get('right_field', '')}",
            "match_rate": _percent(row.get("left_match_rate")),
            "recommendation": str(row.get("recommendation", "")),
        }
        for row in frame_records(relationships)
        if str(row.get("recommendation", "")) == "recommended_lookup"
    ][:12]

    issues = _discovery_issue_lines(profile_issues)
    files = _discovery_file_list(profile_sheets.get("source_inventory", pd.DataFrame()))
    doc_inventory = getattr(document_insights, "document_inventory", pd.DataFrame())
    for row in frame_records(doc_inventory):
        name = str(row.get("file_name", ""))
        if name and name not in files:
            files.append(name)

    return {
        "files": files,
        "tables": tables,
        "keys": keys,
        "relationships": relationship_rows,
        "issues": issues,
    }


def _discovery_file_list(source_inventory: pd.DataFrame) -> list[str]:
    files: list[str] = []
    for row in frame_records(source_inventory):
        name = str(row.get("file_name", ""))
        if name and name not in files:
            files.append(name)
    return files


def build_plan_view(job_config: dict[str, Any], document_insights: Any) -> dict[str, Any]:
    """Render the executed JobConfig as a human-readable plan for the review page.

    This exposes the *actual* deterministic plan that ran (base table, lookups,
    formulas, anomaly rules and document-derived rules), so the Plan Review page
    reflects real execution rather than a static mock.
    """

    lookups = list(job_config.get("lookups", []))
    single_lookup = job_config.get("lookup")
    if single_lookup:
        lookups = [single_lookup, *lookups]

    lookup_lines = [_describe_lookup(lookup) for lookup in lookups]
    formula_lines = [_describe_formula(formula) for formula in job_config.get("formulas", [])]
    anomaly_lines = [_describe_anomaly_rule(rule) for rule in job_config.get("anomaly_rules", [])]
    anomaly_lines = [line for line in anomaly_lines if line]

    doc_rule_count = 0
    if document_insights is not None:
        doc_rule_count = len(getattr(document_insights, "generated_rules", []) or [])
    document_lines = (
        [f"从说明文档/模板中抽取了 {doc_rule_count} 条校验规则"] if doc_rule_count else []
    )

    return {
        "main_table": str(job_config.get("base_table", "")),
        "lookups": lookup_lines,
        "formulas": formula_lines,
        "anomaly_rules": anomaly_lines,
        "document_rules": document_lines,
        "risks": _plan_risks(lookups, anomaly_lines),
    }


def _describe_lookup(lookup: dict[str, Any]) -> str:
    left = lookup.get("left_keys") or lookup.get("left_key") or ""
    right = lookup.get("right_keys") or lookup.get("right_key") or ""
    left_text = "+".join(left) if isinstance(left, list) else str(left)
    right_text = "+".join(right) if isinstance(right, list) else str(right)
    source = lookup.get("source_table") or lookup.get("name") or "关联表"
    return f"{left_text} → {source}.{right_text}".strip(" .→")


def _describe_formula(formula: dict[str, Any]) -> str:
    output = str(formula.get("output", ""))
    op = str(formula.get("op", ""))
    return f"{output} = {op}" if output else op


def _describe_anomaly_rule(rule: dict[str, Any]) -> str:
    """Render an anomaly rule as business-readable text.

    The generated ``reason`` is often just the internal rule ``name`` (e.g.
    ``doc_rule_amount_min_value`` / ``*_not_found``). Run whichever text we pick
    through the shared display mapping, which translates internal labels into
    Chinese and leaves genuine human sentences untouched, so the plan view never
    leaks internal labels.
    """
    text = str(rule.get("reason") or rule.get("name") or "").strip()
    return display_issue_name(text) if text else ""


def _plan_risks(lookups: list[dict[str, Any]], anomaly_lines: list[str]) -> list[str]:
    risks = []
    for lookup in lookups:
        if str(lookup.get("match_mode", "")) == "fuzzy":
            source = lookup.get("source_table") or lookup.get("name") or "关联表"
            risks.append(f"{source} 使用模糊匹配，建议复核低置信度命中")
        if str(lookup.get("duplicate_strategy", "")) in {"review", "list", "aggregate"}:
            source = lookup.get("source_table") or lookup.get("name") or "关联表"
            risks.append(f"{source} 存在一对多匹配，需确认重复键处理策略")
    if not anomaly_lines:
        risks.append("未配置异常规则，结果仅做匹配与公式处理")
    return risks


def _discovery_issue_lines(profile_issues: pd.DataFrame) -> list[str]:
    if profile_issues.empty:
        return []
    labels = {"null_values": "存在空值", "empty_strings": "存在空字符串"}
    lines = []
    for row in frame_records(profile_issues):
        label = labels.get(str(row.get("issue_type", "")), str(row.get("issue_type", "")))
        count = _int_or_zero(row.get("count"))
        lines.append(f"{row.get('table', '')}.{row.get('field', '')} {label}（{count} 处）")
    return lines[:20]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _percent(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "-"


def _confidence_label(recommendation: Any) -> str:
    mapping = {
        "primary_key_candidate": "高",
        "lookup_key_candidate": "中",
    }
    return mapping.get(str(recommendation), "低")


def _summary_metric(summary: pd.DataFrame, metric: str) -> int:
    if summary.empty:
        return 0
    rows = summary[summary["metric"].eq(metric)]
    if rows.empty:
        return 0
    return int(rows.iloc[0]["value"])


def _limited_records(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    if limit == 0:
        return _dataframe_records(df)
    return _dataframe_records(df.head(limit))


def _dataframe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    normalized = df.astype(object).where(pd.notna(df), None)
    return [
        {str(key): _json_safe_value(value) for key, value in row.items()}
        for row in normalized.to_dict(orient="records")
    ]


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            return value
    return value


def _formula_source_tables(job_config: dict[str, Any]) -> list[str]:
    """Lookup tables a formula fill will point at, so the spec can declare them."""

    if not job_config.get("formula_output"):
        return []
    lookups = list(job_config.get("lookups") or [])
    if job_config.get("lookup"):
        lookups.insert(0, job_config["lookup"])
    names: list[str] = []
    for lookup in lookups:
        name = str((lookup or {}).get("source_table") or "").strip()
        if name and name not in names:
            names.append(name)
    return names
