"""Compile validated JobConfig objects into registry-backed execution plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from data_agent.capabilities.builtins import get_capability_registry
from data_agent.capabilities.confirmation import (
    HIGH_REMOVAL_RATIO_THRESHOLD,
    build_step_impact_estimate,
    confirmation_reasons,
)
from data_agent.capabilities.registry import CapabilityRegistry
from data_agent.schemas.job import JobConfig
from data_agent.schemas.output import OutputSpec
from data_agent.schemas.plan import ExecutionPlan, ExecutionStep
from data_agent.schemas.task import ActionAuthorization, TaskSpec

PLAN_COMPILER_VERSION = "1.0.0"

_ACTION_CAPABILITIES = {
    "clean": {
        "map_fields",
        "map_values",
        "trim",
        "normalize_case",
        "normalize_width",
        "fill_missing",
        "detect_anomalies",
        "validate_rules",
    },
    "filter": {"filter_rows"},
    "deduplicate": {"deduplicate"},
    "lookup": {"lookup_fields", "join_tables", "rollup", "write_formulas"},
    "derive": {"derive_column"},
    "validate": {"validate_rules", "detect_anomalies"},
    "analyze": {"aggregate", "pivot", "top_n", "trend_analysis"},
    "chart": {"generate_chart"},
    "review": {"validate_rules", "detect_anomalies"},
    "annotate": {"derive_column", "validate_rules", "detect_anomalies"},
    "export": {"project_schema", "export_table", "export_report"},
}
_INFRASTRUCTURE_CAPABILITIES = {
    "read_tables",
    # Reshaping is how the input is *read*, not something done to the user's data:
    # a wide report and its long form hold the same values. Both only ever appear
    # because the goal asked for them, so neither needs an action of its own.
    "reshape",
    "union_tables",
    "extract_document_rules",
    "profile_dataset",
    "detect_relationships",
    "score_quality",
    "build_audit",
    "summarize",
}
# Slot that must be answered before the action can be compiled at all. While the slot
# is open the plan is legitimately incomplete, not wrong. Mirrors the slot ids
# agent.task_spec emits.
_ACTION_PENDING_SLOTS = {
    "filter": "filter_rule",
    "deduplicate": "deduplication_rule",
    # 「统计一下」 asks for a number without saying what to group by. The plan cannot
    # compile an aggregate yet, but the request is not wrong — it is waiting on an
    # answer. Silently handing back the detail table is the one response no mature
    # product gives: the question simply goes unanswered with nothing saying so.
    "analyze": "analysis_target",
}
_REQUIRED_ACTION_CAPABILITIES = {
    "filter": {"filter_rows"},
    "deduplicate": {"deduplicate"},
    "lookup": {"lookup_fields", "join_tables", "rollup"},
    "derive": {"derive_column"},
    "validate": {"validate_rules", "detect_anomalies"},
    "analyze": {"aggregate", "pivot", "top_n", "trend_analysis"},
    "chart": {"generate_chart"},
    "export": {"export_table"},
}


def build_execution_plan(
    job_config: JobConfig | dict[str, Any],
    *,
    task_spec: TaskSpec | dict[str, Any] | None = None,
    output_spec: OutputSpec | dict[str, Any] | None = None,
    impact_preview: dict[str, Any] | None = None,
    include_result_frames: bool = False,
    registry: CapabilityRegistry | None = None,
) -> ExecutionPlan:
    """Compile the current deterministic pipeline into an explicit ordered DAG."""

    job = (
        job_config
        if isinstance(job_config, JobConfig)
        else JobConfig.model_validate(job_config)
    )
    outputs = (
        output_spec
        if isinstance(output_spec, OutputSpec)
        else OutputSpec.model_validate(output_spec)
        if output_spec is not None
        else None
    )
    active_registry = registry or get_capability_registry()
    steps: list[ExecutionStep] = []

    def add(
        capability_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        required_fields: list[str] | None = None,
        output_fields: list[str] | None = None,
        authorization: ActionAuthorization = "implied",
    ) -> None:
        capability = active_registry.resolve(capability_id)
        step_id = f"s{len(steps) + 1:02d}_{capability_id}"
        resolved_parameters = parameters or {}
        impact_estimate = (
            build_step_impact_estimate(impact_preview)
            if capability.requires_confirmation
            else None
        )
        reasons = confirmation_reasons(
            capability_id=capability.capability_id,
            capability_requires_confirmation=capability.requires_confirmation,
            authorization=authorization,
            parameters=resolved_parameters,
            impact_estimate=impact_estimate,
        )
        steps.append(
            ExecutionStep(
                step_id=step_id,
                capability_id=capability.capability_id,
                capability_version=capability.version,
                depends_on=[steps[-1].step_id] if steps else [],
                parameters=resolved_parameters,
                required_fields=required_fields or [],
                output_fields=output_fields or [],
                risk_level=capability.risk_level,
                requires_confirmation=bool(reasons),
                confirmation_reasons=reasons,
                impact_estimate=impact_estimate,
                authorization=authorization,
                acceptance_rules=list(capability.acceptance_rules),
                failure_policy=capability.failure_policy,
            )
        )

    input_paths = [str(source.path) for source in job.sources.values()]
    if job.main_source:
        input_paths.append(str(job.main_source.path))
    if job.input.include_unstructured_docs:
        add(
            "extract_document_rules",
            {"input_paths": input_paths, "recursive": True},
        )
    add("read_tables", {"input_paths": input_paths, "recursive": True})
    if job.union_tables:
        add("union_tables", {"tables": list(job.union_tables)})
    if job.melt is not None:
        add(
            "reshape",
            {
                "value_columns": list(job.melt.value_columns),
                "variable_name": job.melt.variable_name,
                "value_name": job.melt.value_name,
            },
            output_fields=[job.melt.variable_name, job.melt.value_name],
        )
    if job.dirty_data.enabled:
        add(
            "detect_anomalies",
            {"auto_fix_safe_issues": job.dirty_data.auto_fix_safe_issues},
        )
    add("profile_dataset")
    if len(job.sources) + int(job.main_source is not None) > 1:
        add("detect_relationships")
    if job.field_mapping.rename:
        add(
            "map_fields",
            {"rename": job.field_mapping.rename},
            required_fields=list(job.field_mapping.rename),
            output_fields=list(job.field_mapping.rename.values()),
        )
    if job.field_mapping.value_maps:
        add(
            "map_values",
            {"value_maps": job.field_mapping.value_maps},
            required_fields=list(job.field_mapping.value_maps),
            output_fields=list(job.field_mapping.value_maps),
        )

    lookups = [*([job.lookup] if job.lookup else []), *job.lookups]
    for lookup in lookups:
        left_keys = lookup.left_keys or ([lookup.left_key] if lookup.left_key else [])
        right_keys = lookup.right_keys or ([lookup.right_key] if lookup.right_key else [])
        parameters = {
            "left_keys": left_keys,
            "right_keys": right_keys,
            "fields": lookup.fields,
            "match_mode": lookup.match_mode,
            "duplicate_strategy": lookup.duplicate_strategy,
            "source_table": lookup.source_table,
        }
        add(
            "lookup_fields",
            parameters,
            required_fields=[*left_keys, *right_keys],
            output_fields=[
                lookup.field_aliases.get(field, field) for field in lookup.fields
            ],
        )

    for rollup in job.rollups:
        add(
            "rollup",
            {
                "source_table": rollup.source_table,
                "left_key": rollup.left_key,
                "right_key": rollup.right_key,
                "measure": rollup.measure,
                "agg": rollup.agg,
                "output": rollup.output,
            },
            required_fields=[rollup.left_key],
            output_fields=[rollup.output],
        )

    for formula in job.formulas:
        capability_id = active_registry.resolve_formula_operator(
            formula.op
        ).capability_id
        parameters = formula.model_dump(mode="json")
        required_fields = [
            *([formula.source] if formula.source else []),
            *formula.columns,
        ]
        add(
            capability_id,
            parameters,
            required_fields=required_fields,
            output_fields=(
                []
                if formula.op in {"filter_rows", "dedupe"}
                else [formula.output] if formula.output else []
            ),
            authorization=formula.authorization,
        )

    if job.anomaly_rules:
        add(
            "validate_rules",
            {"rules": [rule.model_dump(mode="json") for rule in job.anomaly_rules]},
            required_fields=[rule.condition.field for rule in job.anomaly_rules],
            output_fields=[job.abnormal_field],
        )
    # From here on the order mirrors the delivery executor, including stages that used
    # to exist only in telemetry. Keeping one ordered contract is what makes an
    # undeclared runtime action detectable instead of silently dropping its metrics.
    wants_internal_sheets = job.export.include_internal_sheets and (
        outputs is None or outputs.include_audit
    )
    if job.analysis.enabled and wants_internal_sheets:
        add(
            "pivot",
            {
                "group_by": job.pivot.group_by,
                "metrics": [metric.model_dump(mode="json") for metric in job.pivot.metrics],
            },
            required_fields=job.pivot.group_by,
        )
    if job.target_schema:
        add(
            "project_schema",
            {"schema": list(job.target_schema)},
            output_fields=list(job.target_schema),
        )
    formula_fills = [
        {
            "source_table": lookup.source_table or lookup.name or "",
            "left_keys": lookup.left_keys
            or ([lookup.left_key] if lookup.left_key else []),
            "right_keys": lookup.right_keys
            or ([lookup.right_key] if lookup.right_key else []),
            "fields": list(lookup.fields),
            "aliases": dict(lookup.field_aliases),
        }
        for lookup in lookups
        if lookup.fields
    ]
    if job.formula_output and formula_fills:
        add("write_formulas", {"fills": formula_fills})
    if outputs is not None and outputs.aggregate is not None:
        # The 汇总结果 sheet is a distinct step from the exception pivot above. Without
        # it declared, the executor's aggregate stage matched no plan step: its metrics
        # — including how many amounts could not be read as numbers — were discarded,
        # and two steps that had run were reported as skipped.
        add(
            "aggregate",
            {
                "group_by": list(outputs.aggregate.group_by),
                "metrics": list(outputs.aggregate.metrics),
            },
            required_fields=list(outputs.aggregate.group_by),
        )
    add(
        "export_table",
        {
            "output_file": str(job.export.output_file),
            "sheets": (
                sorted(outputs.allowed_sheet_names) if outputs else ["处理结果"]
            ),
        },
    )
    if outputs is not None and wants_internal_sheets:
        add("build_audit", {"include_audit": True})
    # Quality scoring is part of every API execution. The JobConfig flag controls
    # legacy presentation settings, not whether the delivery contract is checked.
    add(
        "score_quality",
        {"critical_fields": job.quality_score.critical_fields},
        required_fields=job.quality_score.critical_fields,
    )
    needs_summary = bool(
        include_result_frames
        or (outputs is not None and (outputs.charts or outputs.report.enabled))
    )
    if needs_summary:
        add("summarize", {"goal": job.goal or ""})
    if outputs is not None and outputs.charts:
        add(
            "generate_chart",
            {"charts": [chart.model_dump(mode="json") for chart in outputs.charts]},
        )
    report_formats = outputs.report.formats if outputs else job.report.formats
    if report_formats:
        add(
            "export_report",
            {
                "output_dir": str(job.export.output_file.parent),
                "formats": report_formats,
            },
        )

    plan = ExecutionPlan(
        version=3,
        config_hash=job_config_fingerprint(job),
        steps=steps,
    )
    validate_execution_plan(plan, registry=active_registry)
    if task_spec is not None:
        validate_task_action_coverage(task_spec, plan)
    return plan


def job_config_fingerprint(job_config: JobConfig | dict[str, Any]) -> str:
    """Seal all business-affecting compatibility config into the execution plan."""

    job = (
        job_config
        if isinstance(job_config, JobConfig)
        else JobConfig.model_validate(job_config)
    )
    payload = job.model_dump(mode="json")
    for source in payload.get("sources", {}).values():
        source["path"] = Path(str(source.get("path") or "")).name
    main_source = payload.get("main_source")
    if isinstance(main_source, dict):
        main_source["path"] = Path(str(main_source.get("path") or "")).name
    export = payload.get("export")
    if isinstance(export, dict):
        export["output_file"] = Path(str(export.get("output_file") or "")).name
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_job_config_binding(
    plan: ExecutionPlan | dict[str, Any],
    job_config: JobConfig | dict[str, Any],
) -> None:
    """Reject runtime config drift from the authoritative execution plan."""

    execution_plan = (
        plan if isinstance(plan, ExecutionPlan) else ExecutionPlan.model_validate(plan)
    )
    if execution_plan.version < 3:
        return
    if execution_plan.config_hash != job_config_fingerprint(job_config):
        raise ValueError("JobConfig 与已确认 ExecutionPlan 不一致。")


def unmet_task_actions(
    task_spec: TaskSpec | dict[str, Any],
    plan: ExecutionPlan | dict[str, Any],
) -> list[str]:
    """Requested actions the plan does not carry out, without raising.

    :func:`validate_task_action_coverage` rejects a plan that *silently* omits work,
    but some gaps are legitimate — the data may not support a join, or the user has
    not specified the condition yet. Those runs still execute, so the quality score
    and the business conclusion need to know what was left undone.
    """

    task = (
        task_spec
        if isinstance(task_spec, TaskSpec)
        else TaskSpec.model_validate(task_spec)
    )
    execution_plan = (
        plan if isinstance(plan, ExecutionPlan) else ExecutionPlan.model_validate(plan)
    )
    capabilities = {step.capability_id for step in execution_plan.steps}
    uncovered = [
        action
        for action in task.actions
        if action in _REQUIRED_ACTION_CAPABILITIES
        and capabilities.isdisjoint(_REQUIRED_ACTION_CAPABILITIES[action])
    ]
    # Actions already known to be unsupported by this input are recorded on the spec
    # rather than left in `actions`, so union both sources.
    return list(dict.fromkeys([*task.unmet_actions, *uncovered]))


def validate_task_action_coverage(
    task_spec: TaskSpec | dict[str, Any],
    plan: ExecutionPlan | dict[str, Any],
) -> None:
    """Reject plans that silently omit an executable action requested by the user."""

    task = (
        task_spec
        if isinstance(task_spec, TaskSpec)
        else TaskSpec.model_validate(task_spec)
    )
    execution_plan = (
        plan if isinstance(plan, ExecutionPlan) else ExecutionPlan.model_validate(plan)
    )
    capabilities = {step.capability_id for step in execution_plan.steps}
    # An action the user asked for but has not yet specified ("过滤掉金额小于 0 的记录"
    # with no parseable condition) is not an omission — the task already carries a
    # missing_slot for it and the clarification gate is about to ask. Enforcing
    # coverage here aborted the job before the question could ever be asked.
    pending_slots = {slot.slot_id for slot in task.missing_slots}
    missing = [
        action
        for action in task.actions
        if action in _REQUIRED_ACTION_CAPABILITIES
        and capabilities.isdisjoint(_REQUIRED_ACTION_CAPABILITIES[action])
        and _ACTION_PENDING_SLOTS.get(action) not in pending_slots
    ]
    if missing:
        raise ValueError(
            "处理计划未覆盖用户要求的动作：" + "、".join(missing)
        )

    lookup_steps = (
        [
            step for step in execution_plan.steps
            if step.capability_id in {"lookup_fields", "join_tables", "rollup"}
        ]
        if "lookup" in task.actions
        else []
    )
    join_keys = {
        str(field)
        for step in lookup_steps
        for key in ("left_keys", "right_keys")
        for field in step.parameters.get(key, [])
    }
    missing_lookup_fields = []
    for target in task.target_fields:
        if (
            "lookup" not in task.actions
            or not target.table
            or target.table == task.primary_entity
            or target.field in join_keys
            or target.field.lower() not in task.objective.lower()
        ):
            continue
        # A rollup covers the field too, by summarising it rather than copying it:
        # 「把订单明细的金额汇总到客户档案」 delivers 金额合计, and demanding a raw 金额
        # column rejected the very step that answered the request.
        covered = any(
            str(step.parameters.get("source_table") or "") == target.table
            and (
                target.field
                in {str(field) for field in step.parameters.get("fields", [])}
                or str(step.parameters.get("measure") or "") == target.field
            )
            for step in lookup_steps
        )
        if not covered:
            missing_lookup_fields.append(f"{target.table}.{target.field}")
    if missing_lookup_fields:
        raise ValueError(
            "处理计划未覆盖用户要求的关联字段："
            + "、".join(sorted(missing_lookup_fields))
        )

    allowed = set(_INFRASTRUCTURE_CAPABILITIES)
    for action in task.actions:
        allowed.update(_ACTION_CAPABILITIES.get(action, set()))
    unexpected = sorted(capabilities - allowed)
    if unexpected:
        raise ValueError(
            "处理计划包含用户未要求的动作，与 TaskSpec 不一致："
            + "、".join(unexpected)
        )


def validate_task_rule_preservation(
    task_spec: TaskSpec | dict[str, Any],
    baseline_config: JobConfig | dict[str, Any],
    candidate_config: JobConfig | dict[str, Any],
) -> None:
    """Prevent planning/reflection from weakening existing validation rules."""

    task = (
        task_spec
        if isinstance(task_spec, TaskSpec)
        else TaskSpec.model_validate(task_spec)
    )
    if not set(task.actions) & {"validate", "review"}:
        return
    baseline = (
        baseline_config
        if isinstance(baseline_config, JobConfig)
        else JobConfig.model_validate(baseline_config)
    )
    candidate = (
        candidate_config
        if isinstance(candidate_config, JobConfig)
        else JobConfig.model_validate(candidate_config)
    )
    required_rules = {_rule_signature(rule) for rule in baseline.anomaly_rules}
    candidate_rules = {_rule_signature(rule) for rule in candidate.anomaly_rules}
    if not required_rules <= candidate_rules:
        raise ValueError("候选计划删除或修改了任务所需的校验规则。")


def _rule_signature(rule) -> str:
    return json.dumps(
        rule.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def validate_execution_plan(
    plan: ExecutionPlan | dict[str, Any],
    *,
    registry: CapabilityRegistry | None = None,
    allow_legacy_confirmation_policy: bool = False,
) -> ExecutionPlan:
    """Reject unknown capabilities and any metadata downgraded from the registry."""

    validated = (
        plan if isinstance(plan, ExecutionPlan) else ExecutionPlan.model_validate(plan)
    )
    active_registry = registry or get_capability_registry()
    for step in validated.steps:
        capability = active_registry.resolve(step.capability_id)
        if step.capability_version != capability.version:
            raise ValueError(
                f"{step.capability_id} version mismatch: "
                f"{step.capability_version} != {capability.version}"
            )
        capability.validate_parameters(step.parameters)
        if step.risk_level != capability.risk_level:
            raise ValueError(f"{step.capability_id} risk metadata cannot be changed")
        is_legacy_step = (
            allow_legacy_confirmation_policy
            and step.impact_estimate is None
            and not step.confirmation_reasons
        )
        if is_legacy_step:
            legacy_confirmation = (
                capability.requires_confirmation and step.authorization != "stated"
            )
            if step.requires_confirmation != legacy_confirmation:
                raise ValueError(
                    f"{step.capability_id} confirmation policy cannot be changed"
                )
        elif capability.requires_confirmation and step.impact_estimate is None:
            raise ValueError(
                f"{step.capability_id} confirmation policy requires impact evidence"
            )
        elif (
            capability.requires_confirmation
            and step.impact_estimate.high_removal_ratio_threshold
            != HIGH_REMOVAL_RATIO_THRESHOLD
        ):
            raise ValueError(
                f"{step.capability_id} confirmation threshold cannot be changed"
            )
        elif not capability.requires_confirmation and step.impact_estimate is not None:
            raise ValueError(
                f"{step.capability_id} confirmation policy has unexpected impact evidence"
            )
        if not is_legacy_step:
            expected_reasons = confirmation_reasons(
                capability_id=capability.capability_id,
                capability_requires_confirmation=capability.requires_confirmation,
                authorization=step.authorization,
                parameters=step.parameters,
                impact_estimate=step.impact_estimate,
            )
            if (
                step.confirmation_reasons != expected_reasons
                or step.requires_confirmation != bool(expected_reasons)
            ):
                raise ValueError(
                    f"{step.capability_id} confirmation policy cannot be changed"
                )
        if step.failure_policy != capability.failure_policy:
            raise ValueError(f"{step.capability_id} failure policy cannot be changed")
        if step.acceptance_rules != list(capability.acceptance_rules):
            raise ValueError(
                f"{step.capability_id} acceptance rules cannot be changed"
            )
        if step.output_fields and capability.field_effects == ("none",):
            raise ValueError(
                f"{step.capability_id} declares output fields without a field effect"
            )
    return validated
