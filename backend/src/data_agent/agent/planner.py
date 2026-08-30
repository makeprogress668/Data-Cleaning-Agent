from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

import pandas as pd

from data_agent.agent.data_context import (
    build_column_context,
    build_profile_column_context,
    group_columns_by_table,
    policy_filtered_records,
    relationship_context,
)
from data_agent.agent.goal_understanding import understand_goal
from data_agent.agent.llm_client import build_default_llm_planner
from data_agent.agent.memory import load_memory_summary
from data_agent.agent.prompts import PLANNER_SYSTEM_PROMPT
from data_agent.capabilities import (
    build_execution_plan,
    get_capability_registry,
    validate_task_action_coverage,
    validate_task_rule_preservation,
)
from data_agent.capabilities.artifacts import planned_primary_fields
from data_agent.planning.goal_interpreter import (
    apply_goal_semantic_defaults,
    apply_task_spec_operations,
    needs_lookup_from_goal,
    validate_task_spec_operations,
)
from data_agent.schemas import OutputSpec, build_output_spec
from data_agent.schemas.job import (
    REFLECTION_OVERRIDE_KEYS,
    FormulaConfig,
    JobConfig,
    LookupConfig,
    SourceConfig,
)
from data_agent.schemas.plan import ExecutionPlan
from data_agent.schemas.task import TaskSpec
from data_agent.tools import (
    apply_formulas,
    build_recommendation_package,
    export_workbook,
    profile_dataset,
    read_tables_from_paths,
    write_recommended_job_config,
)
from data_agent.tools.recommender import recommend_lookups
from data_agent.utils.collections import deep_merge, frame_records, unique

LLMPlannerCallable = Callable[[list[dict[str, str]]], str]


@dataclass(frozen=True)
class PlannerResult:
    """Reviewable plan package generated from a business goal and input files."""

    goal: str
    job_config: dict[str, Any]
    plan_summary: pd.DataFrame
    clarification_questions: pd.DataFrame
    profile_sheets: dict[str, pd.DataFrame]
    recommendation_sheets: dict[str, pd.DataFrame]
    execution_plan: ExecutionPlan
    output_spec: OutputSpec
    llm_attempts: pd.DataFrame = field(default_factory=pd.DataFrame)
    llm_prompt_messages: list[dict[str, str]] = field(default_factory=list)

    def to_sheets(self) -> dict[str, pd.DataFrame]:
        sheets = {
            "plan_summary": self.plan_summary,
            "clarification_questions": self.clarification_questions,
            "recommendation_summary": self.recommendation_sheets.get(
                "recommendation_summary",
                pd.DataFrame(),
            ),
            "recommended_lookups": self.recommendation_sheets.get(
                "recommended_lookups",
                pd.DataFrame(),
            ),
            "recommended_formulas": self.recommendation_sheets.get(
                "recommended_formulas",
                pd.DataFrame(),
            ),
            "recommended_anomaly_rules": self.recommendation_sheets.get(
                "recommended_anomaly_rules",
                pd.DataFrame(),
            ),
            "field_cleaning_plan": self.recommendation_sheets.get(
                "field_cleaning_plan",
                pd.DataFrame(),
            ),
            "approval_checklist": self.recommendation_sheets.get(
                "approval_checklist",
                pd.DataFrame(),
            ),
            "table_profile": self.profile_sheets.get("table_profile", pd.DataFrame()),
            "relationship_candidates": self.profile_sheets.get(
                "relationship_candidates",
                pd.DataFrame(),
            ),
            "llm_planner_attempts": self.llm_attempts,
            "execution_plan": pd.DataFrame(
                [
                    {
                        "step_id": step.step_id,
                        "capability": step.capability_id,
                        "version": step.capability_version,
                        "depends_on": ", ".join(step.depends_on),
                        "risk": step.risk_level,
                        "requires_confirmation": step.requires_confirmation,
                    }
                    for step in self.execution_plan.steps
                ]
            ),
            "output_spec": pd.DataFrame(
                [
                    {
                        "primary_artifact": self.output_spec.primary_artifact.name,
                        "secondary_artifacts": ", ".join(
                            item.name
                            for item in self.output_spec.secondary_artifacts
                        ),
                        "charts": ", ".join(
                            item.chart_id for item in self.output_spec.charts
                        ),
                        "report": self.output_spec.report.enabled,
                        "audit": self.output_spec.include_audit,
                    }
                ]
            ),
        }
        return {name: sheet for name, sheet in sheets.items() if not sheet.empty}


def plan_from_goal(
    input_paths: list[Union[str, Path]],
    goal: str,
    output_file: str | Path = "data/output/planned_cleaning_result.xlsx",
    output_job_path: str | Path = "configs/planned_cleaning_job.json",
    recursive: bool = True,
    llm_candidate: dict[str, Any] | str | None = None,
    llm_planner: LLMPlannerCallable | None = None,
    max_llm_rounds: int = 2,
) -> PlannerResult:
    """Build a reviewable JobConfig draft from a natural-language goal.

    The deterministic recommendation is always created first. If an LLM candidate
    or callable is provided, its output is treated as an untrusted JobConfig draft:
    it is merged through a strict whitelist, validated against the input profile,
    and discarded if it fails validation.
    """

    normalized_goal = goal.strip()
    if not normalized_goal:
        raise ValueError("goal cannot be empty")

    tables, source_inventory = read_tables_from_paths(input_paths, recursive=recursive)
    if not tables:
        raise ValueError("No supported Excel/CSV tables were found in input paths")

    profile_sheets = profile_dataset(tables, source_inventory=source_inventory)
    recommendation_sheets, recommended_config = build_recommendation_package(
        tables,
        profile_sheets,
        output_job_path=output_job_path,
        goal=normalized_goal,
    )
    # LLM-first goal understanding, same as the API path, so `data-agent plan`
    # gates capabilities by semantics (not keywords) and stays consistent with the
    # service path. Falls back to deterministic understanding without an LLM.
    goal_plan = understand_goal(
        normalized_goal,
        tables,
        column_profile=profile_sheets.get("column_profile"),
        llm_planner=llm_planner,
    )
    planned_config = _apply_goal_to_job_config(
        recommended_config,
        goal=normalized_goal,
        tables=tables,
        input_paths=input_paths,
        output_file=output_file,
        goal_plan=goal_plan,
    )
    planned_config = compile_task_spec_lookups(
        planned_config,
        goal_plan,
        tables,
        profile_sheets,
    )
    task_baseline_config = copy.deepcopy(planned_config)
    llm_messages = build_llm_planner_messages(
        goal=normalized_goal,
        deterministic_config=planned_config,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets,
        tables=tables,
    )
    # Real two-layer agent: when no explicit LLM planner is supplied, try to load a
    # pluggable one from environment config. If none is configured (or it fails),
    # the deterministic plan above is used unchanged.
    if llm_candidate is None and llm_planner is None:
        llm_planner = build_default_llm_planner()

    llm_attempts = pd.DataFrame()
    if llm_candidate is not None or llm_planner is not None:
        planned_config, llm_attempts = _apply_llm_planning(
            deterministic_config=planned_config,
            tables=tables,
            prompt_messages=llm_messages,
            llm_candidate=llm_candidate,
            llm_planner=llm_planner,
            max_llm_rounds=max_llm_rounds,
        )
    # Reconcile only after the semantic planner had a chance to complete a capability
    # the keyword baseline could not compile. Dropping lookup before this point made a
    # correct LLM understanding irreversible: the planner saw a TaskSpec from which
    # lookup had already disappeared and therefore had no permission to add it.
    planned_config, goal_plan, contract_error = reconcile_planner_task_contract(
        planned_config,
        task_baseline_config,
        goal_plan,
    )
    if contract_error:
        llm_attempts = pd.concat(
            [
                llm_attempts,
                pd.DataFrame(
                    [
                        {
                            "round": len(llm_attempts) + 1,
                            "source": "task_contract_fallback",
                            "status": "accepted",
                            "message": (
                                "LLM candidate violated TaskSpec; "
                                f"used deterministic plan: {contract_error}"
                            ),
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    plan_summary = _build_plan_summary(
        goal=normalized_goal,
        config=planned_config,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets,
    )
    questions = _build_clarification_questions(
        goal=normalized_goal,
        config=planned_config,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets,
    )

    validated_config = JobConfig.model_validate(planned_config)
    output_spec = build_output_spec(
        goal_plan.get("task_spec"),
        include_audit=validated_config.audit.default_visible,
        report_formats=validated_config.report.formats,
        formula_source_tables=_formula_source_tables(planned_config),
        primary_fields=planned_primary_fields(validated_config, tables),
        source_artifact_fields={
            name: [str(column) for column in frame.columns]
            for name, frame in tables.items()
        },
    )
    execution_plan = build_execution_plan(
        validated_config,
        task_spec=goal_plan.get("task_spec"),
        output_spec=output_spec,
    )
    validate_task_rule_preservation(
        goal_plan["task_spec"],
        task_baseline_config,
        planned_config,
    )
    validate_task_spec_operations(goal_plan["task_spec"], planned_config)
    validate_task_action_coverage(goal_plan["task_spec"], execution_plan)
    return PlannerResult(
        goal=normalized_goal,
        job_config=planned_config,
        plan_summary=plan_summary,
        clarification_questions=questions,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets,
        execution_plan=execution_plan,
        output_spec=output_spec,
        llm_attempts=llm_attempts,
        llm_prompt_messages=llm_messages,
    )


def drop_unsupported_actions(
    goal_plan: dict[str, Any],
    job_config: dict[str, Any],
) -> dict[str, Any]:
    """Remove requested actions the uploaded data cannot support, with an assumption.

    ``validate_task_action_coverage`` is right to reject a plan that quietly omits
    something the user asked for. But when the deterministic recommender found no
    usable join key at all, *no* plan can cover a ``lookup`` action — so both the LLM
    draft and its deterministic fallback failed and the job aborted with an internal
    error. Telling the user "跨表关联未执行，因为没有找到可用的关联键" is the honest
    outcome; the note travels on the plan and shows up on the confirmation page and in
    the report.
    """

    task_spec = goal_plan.get("task_spec")
    if not isinstance(task_spec, dict) or "lookup" not in (task_spec.get("actions") or []):
        return goal_plan
    # A rollup is a cross-table operation too. Judging only by `lookups` dropped the
    # lookup action from a plan that had a rollup in it, and coverage validation then
    # rejected the rollup as an action nobody asked for.
    if job_config.get("lookup") or job_config.get("lookups") or job_config.get("rollups"):
        return goal_plan

    updated = copy.deepcopy(goal_plan)
    spec = updated["task_spec"]
    spec["actions"] = [action for action in spec.get("actions", []) if action != "lookup"]
    spec["join_requirements"] = []
    # Typed operations are authoritative in TaskSpec v2. Rebuild the compatibility
    # projection after dropping an unsupported action instead of leaving two truths.
    spec.pop("operations", None)
    updated["task_spec"] = TaskSpec.model_validate(spec).model_dump(mode="json")
    spec = updated["task_spec"]
    # Only report it as unmet if the *user* asked for it. The LLM proposes a join
    # whenever it sees two tables, so "帮我分析一下这份数据" came back saying
    # "你要求的跨表关联本次未能执行" — about something the user never requested — and
    # the quality score was docked 20 points for it. An action nobody asked for cannot
    # go unmet; it is simply not part of the job.
    if not _goal_asked_for_lookup(updated):
        return updated

    spec["unmet_actions"] = unique([*spec.get("unmet_actions", []), "lookup"])
    note = "目标提到跨表关联，但上传数据中没有找到可用的关联键，本次未执行匹配。"
    spec["assumptions"] = [*spec.get("assumptions", []), note]
    updated["assumptions"] = [*updated.get("assumptions", []), note]
    return updated


def compile_task_spec_lookups(
    job_config: dict[str, Any],
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    profile_sheets: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Compile schema-grounded lookup intent without reading the goal wording again."""

    task_raw = goal_plan.get("task_spec")
    if not isinstance(task_raw, dict):
        return job_config
    task = TaskSpec.model_validate(task_raw)
    if "lookup" not in task.actions or job_config.get("lookup") or job_config.get("lookups"):
        return job_config
    base_table = task.primary_entity or str(job_config.get("base_table") or "")
    if base_table not in tables:
        return job_config

    objective = task.objective.lower()
    rollup_measures: dict[str, set[str]] = {}
    for rollup in task.rollups:
        source = str(rollup.get("source_table") or "")
        measure = str(rollup.get("measure") or "")
        if source and measure:
            rollup_measures.setdefault(source, set()).add(measure)
    wanted_by_table: dict[str, set[str]] = {}
    for target in task.target_fields:
        if target.table and target.table != base_table:
            if target.table in rollup_measures and (
                target.field in rollup_measures[target.table]
                or target.field.lower() not in objective
            ):
                # The rollup consumes its measure and semantic field matching may
                # identify other detail columns from the table name. Neither is a
                # request to copy raw detail fields onto the destination table.
                continue
            wanted_by_table.setdefault(target.table, set()).add(target.field)
    if not wanted_by_table:
        return job_config

    compiled: list[dict[str, Any]] = []
    for candidate in recommend_lookups(base_table, tables, profile_sheets):
        source_table = str(candidate.get("source_table") or "")
        wanted = wanted_by_table.get(source_table, set())
        fields = [str(field) for field in candidate.get("fields") or [] if field in wanted]
        if not fields:
            continue
        lookup = {**candidate, "fields": fields}
        compiled.append(LookupConfig.model_validate(lookup).model_dump(mode="json"))
    if not compiled:
        return job_config
    planned = copy.deepcopy(job_config)
    planned["lookups"] = compiled
    return planned


def reconcile_planner_task_contract(
    candidate_config: dict[str, Any],
    baseline_config: dict[str, Any],
    goal_plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Finalize semantic planning without weakening the original TaskSpec.

    Unsupported-action reconciliation must happen after semantic planning, while an
    unsafe semantic draft must still fall back atomically. Keeping both decisions in
    one function prevents the API/service and CLI planner entry points from drifting.
    """

    candidate_goal_plan = drop_unsupported_actions(goal_plan, candidate_config)
    contract_error = ""
    try:
        _validate_config_task_contract(
            candidate_config,
            candidate_goal_plan["task_spec"],
            baseline_config,
        )
    except ValueError as exc:
        contract_error = str(exc)
        candidate_config = copy.deepcopy(baseline_config)
        resolved_goal_plan = drop_unsupported_actions(goal_plan, candidate_config)
    else:
        resolved_goal_plan = candidate_goal_plan
    finalized, fallback_error = enforce_planner_task_contract(
        candidate_config,
        baseline_config,
        resolved_goal_plan,
    )
    return finalized, resolved_goal_plan, contract_error or fallback_error


def _goal_asked_for_lookup(goal_plan: dict[str, Any]) -> bool:
    """Whether the join came from the user's words rather than the model's initiative."""

    # Read the goal text, not the derived capabilities: with a model in the loop those
    # capabilities carry the model's own suggestion, and "你要求的跨表关联未能执行" then
    # attributes to the user something only the model proposed.
    goal = goal_plan.get("goal")
    if goal:
        return needs_lookup_from_goal(str(goal))
    capabilities = goal_plan.get("capabilities")
    return bool(isinstance(capabilities, dict) and capabilities.get("needs_lookup"))



def enforce_planner_task_contract(
    candidate_config: dict[str, Any],
    deterministic_config: dict[str, Any],
    goal_plan: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Fall back atomically when an LLM draft exceeds the validated TaskSpec."""

    task_spec = goal_plan.get("task_spec")
    if not isinstance(task_spec, dict):
        return candidate_config, ""

    candidate = apply_task_spec_operations(
        copy.deepcopy(candidate_config),
        goal_plan,
    )
    try:
        _validate_config_task_contract(candidate, task_spec, deterministic_config)
    except ValueError as exc:
        fallback = apply_task_spec_operations(
            copy.deepcopy(deterministic_config),
            goal_plan,
        )
        _validate_config_task_contract(fallback, task_spec, deterministic_config)
        return fallback, str(exc)
    return candidate, ""


def _validate_config_task_contract(
    config: dict[str, Any],
    task_spec: dict[str, Any],
    baseline_config: dict[str, Any],
) -> None:
    """Every check a candidate plan must pass before it may replace the baseline.

    Rule preservation belongs here: a draft that silently drops validation rules used
    to pass this gate and then fail the identical check further down the pipeline,
    which aborted the whole job instead of falling back to the deterministic plan the
    fallback exists for.
    """

    job = JobConfig.model_validate(config)
    output_spec = build_output_spec(
        task_spec,
        include_audit=job.audit.default_visible,
        report_formats=job.report.formats,
        formula_source_tables=_formula_source_tables(config),
    )
    execution_plan = build_execution_plan(
        job,
        task_spec=task_spec,
        output_spec=output_spec,
    )
    validate_task_spec_operations(task_spec, config)
    validate_task_action_coverage(task_spec, execution_plan)
    validate_task_rule_preservation(task_spec, baseline_config, config)


def build_llm_planner_messages(
    goal: str,
    deterministic_config: dict[str, Any],
    profile_sheets: dict[str, pd.DataFrame],
    recommendation_sheets: dict[str, pd.DataFrame],
    tables: dict[str, pd.DataFrame] | None = None,
) -> list[dict[str, str]]:
    """Build a provider-agnostic prompt payload for an LLM planner."""

    column_profile = profile_sheets.get("column_profile", pd.DataFrame())
    if tables:
        columns, data_access = build_column_context(
            tables,
            column_profile=column_profile,
        )
    else:
        columns, data_access = build_profile_column_context(column_profile)
    context = {
        "goal": goal,
        "data_access": data_access,
        "contract": {
            "response_format": {
                "job_config": "JobConfig-compatible JSON object",
                "reasoning_steps": ["short trace of intent -> table -> keys -> fields -> plan"],
                "assumptions": ["short assumptions"],
                "clarification_questions": ["questions for user before execution"],
            },
            "rules": [
                "Do not invent file paths, table names, field names, or sheet names.",
                "Use only tables in available_tables and columns in available_columns.",
                "Do not mutate data directly; only produce a JobConfig candidate.",
                (
                    "Locate the exact target fields the goal needs; keep only those. "
                    "Zero redundancy: drop lookups, fields, and views the goal does "
                    "not require."
                ),
                (
                    "Prefer deterministic lookup, formula, anomaly_rule, "
                    "quality_score, analysis, charts."
                ),
                (
                    "Keep audit.default_visible false unless the user explicitly "
                    "asks for debug output."
                ),
            ],
        },
        "available_tables": frame_records(profile_sheets.get("table_profile", pd.DataFrame())),
        "available_columns": group_columns_by_table(columns),
        "relationship_candidates": relationship_context(
            profile_sheets.get("relationship_candidates", pd.DataFrame())
        ),
        "recommended_lookups": policy_filtered_records(
            recommendation_sheets.get("recommended_lookups", pd.DataFrame())
        ),
        "recommended_formulas": policy_filtered_records(
            recommendation_sheets.get("recommended_formulas", pd.DataFrame())
        ),
        "recommended_anomaly_rules": policy_filtered_records(
            recommendation_sheets.get("recommended_anomaly_rules", pd.DataFrame())
        ),
        "available_capabilities": [
            {
                "capability_id": item["capability_id"],
                "version": item["version"],
                "risk_level": item["risk_level"],
                "required_parameters": item["required_parameters"],
            }
            for item in get_capability_registry().descriptors()
        ],
        "deterministic_job_config": _planner_safe_config(deterministic_config),
    }
    memory_hint = load_memory_summary()
    if memory_hint:
        context["user_memory"] = memory_hint
    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Plan the data-cleaning job for the goal below. Follow the five "
                "reasoning steps: understand intent, locate the main table, locate "
                "related tables and join keys, locate the exact target fields using "
                "available_columns, then assemble a minimal zero-redundancy plan. "
                "Use only the tables in available_tables and the columns in "
                "available_columns. Return strict JSON only:\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"
            ),
        },
    ]


def _planner_safe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Project structural planning context without paths or data literals."""

    def lookup_view(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        allowed = (
            "name",
            "source_table",
            "left_key",
            "right_key",
            "left_keys",
            "right_keys",
            "fields",
            "field_aliases",
            "suffix",
            "match_field",
            "confidence_field",
            "explanation_field",
            "ambiguity_field",
            "match_mode",
            "duplicate_strategy",
            "fuzzy_threshold",
        )
        return {key: copy.deepcopy(raw[key]) for key in allowed if key in raw}

    def formula_view(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        allowed = ("output", "op", "required", "source", "columns", "sep")
        return {key: copy.deepcopy(raw[key]) for key in allowed if key in raw}

    rules = []
    for raw in config.get("anomaly_rules", []):
        if not isinstance(raw, dict):
            continue
        condition = raw.get("condition") if isinstance(raw.get("condition"), dict) else {}
        rules.append(
            {
                "name": str(raw.get("name") or ""),
                "condition": {
                    key: copy.deepcopy(condition[key])
                    for key in ("field", "op", "case_sensitive")
                    if key in condition
                },
                "severity": raw.get("severity", "error"),
            }
        )

    field_mapping = config.get("field_mapping")
    field_mapping = field_mapping if isinstance(field_mapping, dict) else {}
    single_lookup = lookup_view(config.get("lookup"))
    return {
        "name": config.get("name"),
        "goal": config.get("goal"),
        "business_views": copy.deepcopy(config.get("business_views", [])),
        "base_table": config.get("base_table"),
        "lookup": single_lookup or None,
        "lookups": [
            item
            for raw in config.get("lookups", [])
            if (item := lookup_view(raw))
        ],
        "field_mapping": {
            "rename": copy.deepcopy(field_mapping.get("rename", {})),
            "value_map_fields": sorted(
                str(key) for key in field_mapping.get("value_maps", {})
            ),
        },
        "formulas": [
            item
            for raw in config.get("formulas", [])
            if (item := formula_view(raw))
        ],
        "anomaly_rules": rules,
        **{
            key: copy.deepcopy(config[key])
            for key in (
                "abnormal_field",
                "pivot",
                "dirty_data",
                "matching",
                "analysis",
                "charts",
                "quality_score",
                "exception_policy",
                "report",
                "audit",
            )
            if key in config
        },
    }


def write_plan_artifacts(
    result: PlannerResult,
    report_output: str | Path = "data/output/planning_review.xlsx",
    job_output: str | Path = "configs/planned_cleaning_job.json",
) -> dict[str, Path]:
    """Write a planner review workbook and executable JobConfig draft."""

    report_path = export_workbook(report_output, result.to_sheets())
    job_path = write_recommended_job_config(job_output, result.job_config)
    return {"planning_review": report_path, "job_config": job_path}


def refine_config_with_llm(
    deterministic_config: dict[str, Any],
    goal: str,
    tables: dict[str, pd.DataFrame],
    profile_sheets: dict[str, pd.DataFrame],
    recommendation_sheets: dict[str, pd.DataFrame],
    *,
    llm_planner: LLMPlannerCallable | None = None,
    max_llm_rounds: int = 2,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Optionally refine a deterministic JobConfig using the pluggable LLM layer.

    Shared by both the plan and answer paths so goal understanding stays consistent.
    When no LLM is configured/available, the deterministic config is returned
    unchanged. Any accepted LLM draft has already passed the strict whitelist,
    JobConfig schema, and profile reference validation.
    """

    if llm_planner is None:
        llm_planner = build_default_llm_planner()
    if llm_planner is None:
        return deterministic_config, pd.DataFrame()

    messages = build_llm_planner_messages(
        goal=goal,
        deterministic_config=deterministic_config,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets,
        tables=tables,
    )
    return _apply_llm_planning(
        deterministic_config=deterministic_config,
        tables=tables,
        prompt_messages=messages,
        llm_candidate=None,
        llm_planner=llm_planner,
        max_llm_rounds=max_llm_rounds,
    )


def validate_reflection_overrides(
    job_config: dict[str, Any],
    overrides: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> tuple[dict[str, Any], str]:
    """Safely apply reflection-proposed overrides onto an executed JobConfig.

    Phase-2 reflection lets the LLM look at execution quality signals and propose
    JobConfig adjustments. Those proposals are untrusted, exactly like the planning
    draft, so they pass the same gate: a whitelist merge, JobConfig schema
    validation, and profile field-existence checks. Returns ``(merged, "")`` when
    accepted, or ``(job_config, error)`` when the proposal is rejected, so the
    caller keeps the previous config and never degrades.
    """

    if not isinstance(overrides, dict) or not overrides:
        return job_config, "reflection overrides must be a non-empty JSON object"
    allowed = {*REFLECTION_OVERRIDE_KEYS, "quality_score", "business_views"}
    unexpected = sorted(set(overrides) - allowed)
    if unexpected:
        return job_config, "reflection cannot change: " + ", ".join(unexpected)
    return _merge_and_validate_llm_config(job_config, overrides, tables)


def make_lookup_job(
    main_file: str | Path,
    lookup_file: str | Path,
    left_key: str,
    right_key: str,
    fields: list[str],
    output_file: str | Path = "data/output/cleaned_result.xlsx",
    main_sheet: str | int | None = None,
    lookup_sheet: str | int | None = None,
    name: str = "lookup-cleaning-job",
) -> JobConfig:
    """Build a generic deterministic lookup cleaning job."""

    return JobConfig(
        name=name,
        main_source=SourceConfig(path=Path(main_file), sheet=main_sheet),
        lookup=LookupConfig(
            source=SourceConfig(path=Path(lookup_file), sheet=lookup_sheet),
            left_key=left_key,
            right_key=right_key,
            fields=fields,
        ),
        export={"output_file": Path(output_file)},
    )


# Wall-clock budget for the whole LLM planning stage, after which no *further* round
# is started. Sized so one full call (timeout × attempts) can finish and a second is
# only begun when the first was quick — which is exactly when a repair round is cheap.
DEFAULT_PLANNING_BUDGET_SECONDS = 45.0


def _planning_budget_seconds() -> float:
    raw = os.environ.get("DATA_AGENT_PLANNING_BUDGET_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PLANNING_BUDGET_SECONDS
    return value if value > 0 else DEFAULT_PLANNING_BUDGET_SECONDS


def _apply_llm_planning(
    deterministic_config: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    prompt_messages: list[dict[str, str]],
    llm_candidate: dict[str, Any] | str | None,
    llm_planner: LLMPlannerCallable | None,
    max_llm_rounds: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    attempts: list[dict[str, Any]] = []
    if llm_candidate is not None:
        return _evaluate_llm_payload(
            payload=llm_candidate,
            deterministic_config=deterministic_config,
            tables=tables,
            attempts=attempts,
            round_no=1,
            source="provided_candidate",
        )

    if llm_planner is None:
        return deterministic_config, pd.DataFrame(attempts)

    messages = list(prompt_messages)
    rounds = max(1, int(max_llm_rounds))
    started = time.monotonic()
    for round_no in range(1, rounds + 1):
        # A repair round is a bonus: the deterministic config is already a valid plan,
        # and round one only earned a retry by producing a draft that failed
        # validation. Each call can burn timeout × (retries + 1) — 96s at the shipped
        # settings — so one flaky network moment turned a 15-second plan into a
        # 90-second wait for an improvement that then fell back anyway. Past the
        # budget, stop starting rounds; never abandon one already in flight.
        if round_no > 1 and time.monotonic() - started > _planning_budget_seconds():
            attempts.append(
                {
                    "round": round_no,
                    "source": "llm",
                    "status": "skipped",
                    "message": "规划耗时已超出预算，改用确定性方案",
                }
            )
            break
        try:
            raw_response = llm_planner(messages)
        except Exception as exc:  # noqa: BLE001 - any LLM/transport failure falls back
            attempts.append(
                {
                    "round": round_no,
                    "source": "llm",
                    "status": "unavailable",
                    "message": f"LLM 调用失败，回退确定性方案: {exc}",
                }
            )
            return deterministic_config, pd.DataFrame(attempts)
        candidate_config, error = _candidate_config_from_payload(raw_response)
        if error:
            attempts.append(
                {
                    "round": round_no,
                    "source": "llm",
                    "status": "rejected",
                    "message": error,
                }
            )
            messages.extend(_repair_messages(raw_response, error))
            continue

        merged, validation_error = _merge_and_validate_llm_config(
            deterministic_config,
            candidate_config,
            tables,
        )
        if validation_error:
            attempts.append(
                {
                    "round": round_no,
                    "source": "llm",
                    "status": "rejected",
                    "message": validation_error,
                }
            )
            messages.extend(_repair_messages(raw_response, validation_error))
            continue

        attempts.append(
            {
                "round": round_no,
                "source": "llm",
                "status": "accepted",
                "message": "LLM candidate accepted after JobConfig and profile validation",
            }
        )
        return merged, pd.DataFrame(attempts)

    attempts.append(
        {
            "round": rounds + 1,
            "source": "deterministic_fallback",
            "status": "accepted",
            "message": "LLM planning failed validation; fell back to deterministic plan",
        }
    )
    return deterministic_config, pd.DataFrame(attempts)


def _evaluate_llm_payload(
    payload: dict[str, Any] | str,
    deterministic_config: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    attempts: list[dict[str, Any]],
    round_no: int,
    source: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    candidate_config, error = _candidate_config_from_payload(payload)
    if error:
        attempts.append(
            {
                "round": round_no,
                "source": source,
                "status": "rejected",
                "message": error,
            }
        )
        attempts.append(
            {
                "round": round_no + 1,
                "source": "deterministic_fallback",
                "status": "accepted",
                "message": "Candidate could not be parsed; fell back to deterministic plan",
            }
        )
        return deterministic_config, pd.DataFrame(attempts)

    merged, validation_error = _merge_and_validate_llm_config(
        deterministic_config,
        candidate_config,
        tables,
    )
    if validation_error:
        attempts.append(
            {
                "round": round_no,
                "source": source,
                "status": "rejected",
                "message": validation_error,
            }
        )
        attempts.append(
            {
                "round": round_no + 1,
                "source": "deterministic_fallback",
                "status": "accepted",
                "message": "Candidate failed validation; fell back to deterministic plan",
            }
        )
        return deterministic_config, pd.DataFrame(attempts)

    attempts.append(
        {
            "round": round_no,
            "source": source,
            "status": "accepted",
            "message": "Candidate accepted after JobConfig and profile validation",
        }
    )
    return merged, pd.DataFrame(attempts)


def _candidate_config_from_payload(payload: dict[str, Any] | str) -> tuple[dict[str, Any], str]:
    try:
        parsed = _parse_llm_payload(payload)
    except ValueError as exc:
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "LLM planner response must be a JSON object"
    candidate = parsed.get("job_config", parsed)
    if not isinstance(candidate, dict):
        return {}, "LLM planner response must contain object field: job_config"
    return candidate, ""


def _parse_llm_payload(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    text = str(payload).strip()
    if not text:
        raise ValueError("LLM planner response is empty")
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if match:
        text = match.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM planner response is not valid JSON: {exc}") from exc


def _merge_and_validate_llm_config(
    deterministic_config: dict[str, Any],
    candidate_config: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> tuple[dict[str, Any], str]:
    merged = _safe_merge_llm_config(deterministic_config, candidate_config)
    try:
        JobConfig.model_validate(merged)
        _validate_config_against_tables(merged, tables)
    except Exception as exc:
        return deterministic_config, str(exc)
    return merged, ""


def _safe_merge_llm_config(
    deterministic_config: dict[str, Any],
    candidate_config: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(deterministic_config)
    allowed_top_level = {
        "name",
        "goal",
        "output_mode",
        "business_views",
        "base_table",
        "lookup",
        "lookups",
        "field_mapping",
        "formulas",
        "anomaly_rules",
        "abnormal_field",
        "pivot",
        "dirty_data",
        "matching",
        "analysis",
        "charts",
        "quality_score",
        "exception_policy",
        "report",
        "audit",
    }
    for key, value in candidate_config.items():
        if key not in allowed_top_level:
            continue
        if key == "lookup" and isinstance(value, dict):
            merged[key] = _merge_lookup(merged.get("lookup"), value)
            continue
        if key == "lookups" and isinstance(value, list):
            merged[key] = _merge_lookups(merged.get("lookups", []), value)
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)

    # Never let the LLM change input sources or output location.
    for protected_key in ("input", "sources", "main_source", "export"):
        if protected_key in deterministic_config:
            merged[protected_key] = copy.deepcopy(deterministic_config[protected_key])
    if "export" in merged:
        merged["export"]["include_internal_sheets"] = bool(
            candidate_config.get("export", {}).get(
                "include_internal_sheets",
                merged["export"].get("include_internal_sheets", False),
            )
        )
    return merged


def _sanitize_lookup(value: dict[str, Any]) -> dict[str, Any]:
    disallowed = {"source"}
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in disallowed}


def _merge_lookups(
    deterministic_lookups: list[dict[str, Any]],
    candidate_lookups: list[Any],
) -> list[dict[str, Any]]:
    deterministic_by_name = {
        str(lookup.get("name", "")): lookup
        for lookup in deterministic_lookups
        if isinstance(lookup, dict)
    }
    merged = []
    for candidate in candidate_lookups:
        if not isinstance(candidate, dict):
            continue
        merged.append(
            _merge_lookup(
                deterministic_by_name.get(str(candidate.get("name", ""))),
                candidate,
            )
        )
    return merged


def _merge_lookup(
    deterministic_lookup: dict[str, Any] | None,
    candidate_lookup: dict[str, Any],
) -> dict[str, Any]:
    sanitized = _sanitize_lookup(candidate_lookup)
    if not deterministic_lookup:
        # With lookups now goal-gated, an LLM-proposed lookup may have no
        # deterministic counterpart to inherit the marker columns from. Fill the
        # standard `_lookup_*` field names from the lookup name so downstream
        # anomaly rules / validators can reference match state consistently.
        return _fill_lookup_marker_fields(sanitized)
    return deep_merge(deterministic_lookup, sanitized)


def _fill_lookup_marker_fields(lookup: dict[str, Any]) -> dict[str, Any]:
    name = str(lookup.get("name") or "lookup")
    lookup.setdefault("match_field", f"_lookup_matched_{name}")
    lookup.setdefault("confidence_field", f"_lookup_confidence_{name}")
    lookup.setdefault("explanation_field", f"_lookup_explanation_{name}")
    lookup.setdefault("ambiguity_field", f"_lookup_ambiguous_{name}")
    return lookup


def _validate_config_against_tables(
    config: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> None:
    base_table = str(config.get("base_table") or "")
    if base_table and base_table not in tables:
        raise ValueError(f"LLM candidate references unknown base_table: {base_table}")
    base_df = tables.get(base_table, pd.DataFrame())
    available_fields = set(str(column) for column in base_df.columns)

    lookups = list(config.get("lookups") or [])
    if config.get("lookup"):
        lookups.append(config["lookup"])
    for lookup in lookups:
        source_table = str(lookup.get("source_table") or "")
        if source_table and source_table not in tables:
            raise ValueError(
                f"LLM candidate references unknown lookup source_table: {source_table}"
            )
        right_df = tables.get(source_table, pd.DataFrame())
        left_keys = _lookup_keys(lookup, "left")
        right_keys = _lookup_keys(lookup, "right")
        fields = [str(field) for field in lookup.get("fields", [])]
        _ensure_known_fields(left_keys, available_fields, f"lookup {lookup.get('name', '')} left")
        _ensure_known_fields(
            right_keys,
            set(str(column) for column in right_df.columns),
            f"lookup {lookup.get('name', '')} right",
        )
        _ensure_known_fields(
            fields,
            set(str(column) for column in right_df.columns),
            f"lookup {lookup.get('name', '')} fields",
        )
        available_fields.update(lookup.get("field_aliases", {}).values())
        available_fields.update(fields)
        for generated_field in (
            lookup.get("match_field"),
            lookup.get("confidence_field"),
            lookup.get("explanation_field"),
            lookup.get("ambiguity_field"),
        ):
            if generated_field:
                available_fields.add(str(generated_field))

    for formula in config.get("formulas", []):
        _ensure_known_fields(_formula_referenced_fields(formula), available_fields, "formula")
        _ensure_formula_executes(formula, available_fields)
        output = formula.get("output")
        if output:
            available_fields.add(str(output))
    for rule in config.get("anomaly_rules", []):
        condition = rule.get("condition", {})
        field_name = condition.get("field")
        if field_name:
            _ensure_known_fields([str(field_name)], available_fields, "anomaly_rule")
    critical_fields = config.get("quality_score", {}).get("critical_fields", [])
    _ensure_known_fields(
        [str(field) for field in critical_fields],
        available_fields,
        "quality_score",
    )


def _ensure_formula_executes(formula: dict[str, Any], available_fields: set[str]) -> None:
    """Reject a formula the executor could not actually run.

    ``FormulaConfig`` only checks types, so an ``ifs`` with no conditions — a shape the
    model does emit — passed planning and then killed the whole job at execution with
    ``ifs formula requires conditions``. Nothing about that is recoverable at that
    point: the run is already underway and the user gets a traceback instead of a
    workbook. Caught here, the candidate is simply rejected and planning falls back to
    the deterministic plan, which is what the two-layer design promises.

    The check runs the real executor against an empty frame carrying the columns the
    formula may reference. Every arity rule in the executor is a guard clause on the
    formula's own fields, so they all fire while no data is touched. Reusing the
    executor instead of restating its rules is deliberate — a second copy of "what does
    ``ifs`` need" is precisely the kind of thing that drifts out of sync.
    """

    probe = pd.DataFrame({field: pd.Series(dtype="object") for field in sorted(available_fields)})
    try:
        apply_formulas(probe, [FormulaConfig.model_validate(formula)])
    except Exception as exc:  # noqa: BLE001 - any failure means "do not ship this plan"
        raise ValueError(
            f"LLM candidate formula {formula.get('output', '')}"
            f"（{formula.get('op', '')}）无法执行: {exc}"
        ) from exc


def _lookup_keys(lookup: dict[str, Any], side: str) -> list[str]:
    plural = lookup.get(f"{side}_keys") or []
    singular = lookup.get(f"{side}_key")
    if plural:
        return [str(item) for item in plural]
    if singular:
        return [str(singular)]
    return []


def _formula_referenced_fields(formula: dict[str, Any]) -> list[str]:
    fields = []
    if formula.get("source"):
        fields.append(str(formula["source"]))
    fields.extend(str(column) for column in formula.get("columns", []))
    condition = formula.get("condition") or {}
    if condition.get("field"):
        fields.append(str(condition["field"]))
    for item in formula.get("conditions", []):
        if isinstance(item, dict) and item.get("field"):
            fields.append(str(item["field"]))
    return fields


def _ensure_known_fields(fields: list[str], allowed_fields: set[str], label: str) -> None:
    missing = [field for field in fields if field and field not in allowed_fields]
    if missing:
        raise ValueError(f"LLM candidate references unknown {label} field(s): {missing}")


def _repair_messages(raw_response: str, error: str) -> list[dict[str, str]]:
    return [
        {"role": "assistant", "content": raw_response},
        {
            "role": "user",
            "content": (
                "The previous JSON plan failed validation. "
                f"Validation error: {error}. "
                "Return corrected strict JSON only. Do not invent tables, fields, or paths."
            ),
        },
    ]


def _apply_goal_to_job_config(
    config: dict[str, Any],
    goal: str,
    tables: dict[str, pd.DataFrame],
    input_paths: list[Union[str, Path]],
    output_file: str | Path,
    goal_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planned = copy.deepcopy(config)

    planned["name"] = _job_name(goal)
    planned["goal"] = goal
    planned["output_mode"] = "business_answer"
    suggested_base = (goal_plan or {}).get("suggested_base_table")
    if suggested_base and suggested_base in tables:
        planned["base_table"] = suggested_base
    # Shared goal-driven semantics (business_views / quality_score / matching /
    # analysis / charts) come from one helper so this planner path and the
    # deterministic apply_goal_to_job_config never drift apart.
    planned = apply_goal_semantic_defaults(planned, goal=goal, tables=tables, goal_plan=goal_plan)
    planned["input"] = {
        "path": str(input_paths[0]) if len(input_paths) == 1 else None,
        "include_unstructured_docs": True,
    }
    planned.setdefault("export", {})
    planned["export"]["output_file"] = str(output_file)
    planned["export"]["include_internal_sheets"] = False
    planned.setdefault("exception_policy", {})
    planned["exception_policy"].update(
        {
            "exclude_critical_from_final": True,
            "include_minor_in_final": True,
            "require_review_for_unmatched_lookup": True,
        }
    )
    planned.setdefault("report", {})
    planned["report"].update({"formats": ["markdown", "html"]})
    planned.setdefault("audit", {})
    planned["audit"].update({"enabled": True, "default_visible": False})
    return planned


def _job_name(goal: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", goal).strip("-").lower()
    return f"planned-{slug[:40]}" if slug else "planned-data-cleaning-job"


def _build_plan_summary(
    goal: str,
    config: dict[str, Any],
    profile_sheets: dict[str, pd.DataFrame],
    recommendation_sheets: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    table_profile = profile_sheets.get("table_profile", pd.DataFrame())
    relationship_candidates = profile_sheets.get("relationship_candidates", pd.DataFrame())
    recommended_lookups = recommendation_sheets.get("recommended_lookups", pd.DataFrame())
    recommended_formulas = recommendation_sheets.get("recommended_formulas", pd.DataFrame())
    recommended_rules = recommendation_sheets.get("recommended_anomaly_rules", pd.DataFrame())
    return pd.DataFrame(
        [
            {
                "规划项": "业务目标",
                "决策": goal,
                "原因": "由用户输入的自然语言目标确定",
            },
            {
                "规划项": "主表",
                "决策": config.get("base_table", ""),
                "原因": "基于表名优先级和行数自动推断，可在 JobConfig 中覆盖",
            },
            {
                "规划项": "输入表数量",
                "决策": len(table_profile),
                "原因": "数据包扫描得到的结构化表数量",
            },
            {
                "规划项": "候选跨表关系",
                "决策": len(relationship_candidates),
                "原因": "画像阶段识别出的可匹配字段关系",
            },
            {
                "规划项": "推荐 lookup",
                "决策": len(recommended_lookups),
                "原因": "将候选跨表关系转为可执行字段带回配置",
            },
            {
                "规划项": "推荐派生字段",
                "决策": len(recommended_formulas),
                "原因": "生成 record_key、标准化字段和通用业务标记",
            },
            {
                "规划项": "推荐异常规则",
                "决策": len(recommended_rules),
                "原因": "对空值、未匹配、重复和规则命中进行异常打标",
            },
            {
                "规划项": "业务视图",
                "决策": ", ".join(config.get("business_views", [])),
                "原因": "由目标中的导入、复核、分析等关键词和默认交付视图确定",
            },
            {
                "规划项": "关键字段",
                "决策": ", ".join(config.get("quality_score", {}).get("critical_fields", [])),
                "原因": "用于质量评分和业务风险判断",
            },
        ]
    )


def _build_clarification_questions(
    goal: str,
    config: dict[str, Any],
    profile_sheets: dict[str, pd.DataFrame],
    recommendation_sheets: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    rows = []
    recommended_lookups = recommendation_sheets.get("recommended_lookups", pd.DataFrame())
    relationship_candidates = profile_sheets.get("relationship_candidates", pd.DataFrame())
    if recommended_lookups.empty and not relationship_candidates.empty:
        rows.append(
            {
                "优先级": "高",
                "问题": "已发现跨表关系候选，但没有生成可直接执行的 lookup，是否需要人工指定 key？",
                "影响": "会影响维表字段带回和未匹配异常识别",
            }
        )
    if "system_import_view" in config.get("business_views", []):
        rows.append(
            {
                "优先级": "中",
                "问题": "目标包含系统导入，请确认是否已提供 import_template 或目标字段说明。",
                "影响": "会影响 system_import_view 的字段顺序和缺失字段判断",
            }
        )
    if any(token in goal for token in ("复核", "人工", "确认")):
        rows.append(
            {
                "优先级": "中",
                "问题": "目标强调复核，请确认异常清单中的处理状态和备注字段是否符合业务流程。",
                "影响": "会影响结果文件中问题说明视图的可操作性",
            }
        )
    duplicate_review = [
        lookup
        for lookup in config.get("lookups", [])
        if lookup.get("duplicate_strategy") == "review"
    ]
    if duplicate_review:
        rows.append(
            {
                "优先级": "中",
                "问题": "部分右表 key 存在重复，将按 review 策略先取首条并标记歧义，是否接受？",
                "影响": "会影响 lookup 置信度和人工复核任务",
            }
        )
    if not rows:
        rows.append(
            {
                "优先级": "低",
                "问题": "暂无阻塞性问题，可先按推荐 JobConfig 执行，再根据结果复核。",
                "影响": "无",
            }
        )
    return pd.DataFrame(rows)


# Priority buckets used to normalise both LLM and deterministic questions to one
# shape and to decide whether a question is blocking enough to pause execution.
_HIGH_PRIORITY = "高"
_MEDIUM_PRIORITY = "中"
_LOW_PRIORITY = "低"

_PUBLIC_PRIORITY = {
    "high": _HIGH_PRIORITY,
    "medium": _MEDIUM_PRIORITY,
    "low": _LOW_PRIORITY,
}

def collect_clarification_questions(
    goal: str,
    goal_plan: dict[str, Any],
    job_config: dict[str, Any],
    profile_sheets: dict[str, pd.DataFrame],
    recommendation_sheets: dict[str, pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """Return normalised clarification questions (LLM-first, deterministic fallback).

    Each question is ``{"id", "priority", "question", "impact"}``. TaskSpec
    missing slots are the source of truth for both the public priority and blocking
    behaviour. Older plans without TaskSpec slots keep the legacy LLM/deterministic
    fallbacks for backwards compatibility.
    """

    task_spec = goal_plan.get("task_spec") if isinstance(goal_plan, dict) else None
    missing_slots = task_spec.get("missing_slots") if isinstance(task_spec, dict) else None
    llm_questions = goal_plan.get("clarification_questions") if goal_plan else None
    questions: list[dict[str, Any]] = []
    if isinstance(missing_slots, list) and missing_slots:
        for index, slot in enumerate(missing_slots):
            if not isinstance(slot, dict):
                continue
            question = str(slot.get("question") or "").strip()
            if not question:
                continue
            priority = str(slot.get("priority") or "high").strip().lower()
            questions.append(
                {
                    "id": str(slot.get("slot_id") or f"q{index + 1}"),
                    "priority": _PUBLIC_PRIORITY.get(priority, _HIGH_PRIORITY),
                    "question": question,
                    "impact": str(slot.get("impact") or "该信息会影响计划准确性。"),
                    "options": [str(item) for item in slot.get("options") or []],
                }
            )
        if questions:
            return questions

    if isinstance(llm_questions, list) and llm_questions:
        for index, text in enumerate(llm_questions):
            question = str(text).strip()
            if not question:
                continue
            questions.append(
                {
                    "id": f"q{index + 1}",
                    "priority": _HIGH_PRIORITY,
                    "question": question,
                    "impact": "影响计划的准确性，请先确认再执行。",
                }
            )
        if questions:
            return questions

    frame = _build_clarification_questions(
        goal=goal,
        config=job_config,
        profile_sheets=profile_sheets,
        recommendation_sheets=recommendation_sheets or {},
    )
    for index, row in enumerate(frame.to_dict("records")):
        questions.append(
            {
                "id": f"q{index + 1}",
                "priority": str(row.get("优先级", _LOW_PRIORITY)),
                "question": str(row.get("问题", "")),
                "impact": str(row.get("影响", "")),
            }
        )
    return questions


def has_blocking_questions(questions: list[dict[str, Any]]) -> bool:
    """True when at least one question is high-priority (blocking enough to ask)."""
    return any(item.get("priority") == _HIGH_PRIORITY for item in questions)


def collect_conditional_clarification(
    goal_plan: dict[str, Any],
    answered_slot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the highest-impact unanswered TaskSpec slot, at most one per turn.

    Goal understanding always carries ``task_spec.missing_slots``; no second
    confidence heuristic is maintained here.
    """

    answered = answered_slot_ids or set()
    task_spec = goal_plan.get("task_spec") if isinstance(goal_plan, dict) else None
    if not isinstance(task_spec, dict):
        return []
    for slot in task_spec.get("missing_slots") or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if not slot_id or slot_id in answered:
            continue
        # Only a slot the run genuinely cannot proceed without stops the job. This used
        # to force every slot to high, so MissingSlot.priority was a dead field and a
        # four-row upload was answered with two rounds of questions — both of which had
        # standard answers (括号是负数, 合计行不进汇总). A medium slot is an assumption
        # the delivery states, not a gate the user has to clear.
        if str(slot.get("priority") or "high") != "high":
            continue
        kind = str(slot.get("kind") or "business_rule")
        return [
            {
                "id": slot_id,
                "kind": "base_table_selection" if kind == "primary_table" else kind,
                "priority": _HIGH_PRIORITY,
                "question": str(slot.get("question") or "请补充任务信息。"),
                "impact": str(slot.get("impact") or "该信息会影响计划准确性。"),
                "options": [str(item) for item in slot.get("options") or []],
            }
        ]
    return []


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
