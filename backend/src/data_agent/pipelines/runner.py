from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

import pandas as pd

from data_agent.capabilities.builtins import get_capability_registry
from data_agent.capabilities.planning import (
    build_execution_plan,
    validate_job_config_binding,
)
from data_agent.observability.events import ExecutionRecorder, StageRecord
from data_agent.planning.issue_scope import reason_in_scope, requested_issue_kinds
from data_agent.schemas.job import JobConfig, PivotMetric
from data_agent.schemas.output import (
    AGGREGATE_ARTIFACT,
    CHANGE_MANIFEST_ARTIFACT,
    OutputSpec,
)
from data_agent.tools import (
    add_source_trace,
    analyze_dirty_data,
    apply_anomaly_rules,
    apply_field_mapping,
    apply_formulas,
    build_annotation_summary,
    build_annotation_tasks,
    build_annotation_taxonomy,
    build_business_result,
    build_business_summary,
    build_crosstab,
    build_field_change_audit,
    build_issue_summary,
    build_label_summary,
    build_pivot,
    build_record_audit,
    build_review_tasks,
    export_workbook,
    lint_output,
    profile_dataset,
    profile_tables,
    read_table,
    simplify_result_data,
    vlookup,
)
from data_agent.tools.change_manifest import MANIFEST_COLUMNS, build_change_manifest
from data_agent.tools.delivery_view import (
    IMPORT_VIEW_SHEET,
    REVIEW_COLUMNS,
    delivery_problem_sheet,
    delivery_result_sheet,
    system_import_views,
)
from data_agent.tools.dirty_data import DirtyDataResult, non_data_row_mask
from data_agent.tools.excel_formula import formula_column
from data_agent.tools.issue_taxonomy import DIRTY_ISSUE_PREFIX
from data_agent.tools.rollup import rollup_to_master
from data_agent.tools.table_union import union_tables
from data_agent.utils.numeric import unparseable_count
from data_agent.utils.reshape import melt_wide_to_long
from data_agent.utils.target_schema import is_blank_column, project_onto_schema


@dataclass(frozen=True)
class RunResult:
    output_file: Path
    cleaned_count: int
    abnormal_count: int
    summary: pd.DataFrame
    abnormal_pivot: pd.DataFrame
    table_profile: pd.DataFrame
    business_summary: pd.DataFrame
    business_result: pd.DataFrame
    # Dirty-data evidence from the single in-executor run. Surfaced here so every
    # entry point scores quality on the same axes instead of the API path silently
    # treating this axis as empty.
    dirty_issue_summary: pd.DataFrame = dataclass_field(default_factory=pd.DataFrame)
    dirty_record_issues: pd.DataFrame = dataclass_field(default_factory=pd.DataFrame)
    dirty_fix_audit: pd.DataFrame = dataclass_field(default_factory=pd.DataFrame)
    # Engineering sheets, written beside the deliverable rather than inside it.
    internal_workbook: Path | None = None
    # Observed per-stage telemetry, not a post-hoc projection of the plan.
    stages: list[StageRecord] = dataclass_field(default_factory=list)


def load_job_config(config_path: str | Path) -> JobConfig:
    path = Path(config_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return JobConfig.model_validate(payload)


def run_job(
    job: JobConfig,
    output_spec: OutputSpec | None = None,
    *,
    task_spec: dict | None = None,
    import_requirements: pd.DataFrame | None = None,
    template_validation: pd.DataFrame | None = None,
    profile_sheets: dict[str, pd.DataFrame] | None = None,
    recorder: ExecutionRecorder | None = None,
) -> RunResult:
    """Execute one validated JobConfig and write exactly the declared artifacts.

    ``import_requirements`` / ``template_validation`` are only consulted when the
    OutputSpec declares the 可导入数据 sheet; they come from the document-rule layer,
    which the executor itself does not read.

    ``recorder`` collects real per-stage telemetry (duration, rows in/out) as each
    stage runs, so observability reflects what happened instead of being reconstructed
    from the plan afterwards.
    """

    if recorder is None:
        runtime_plan = build_execution_plan(job, output_spec=output_spec)
        validate_job_config_binding(runtime_plan, job)
        telemetry = ExecutionRecorder(runtime_plan)
    else:
        telemetry = recorder
    profile_sheets_input = profile_sheets

    with telemetry.stage("read_tables") as stage:
        source_tables = load_tables(job)
        stage.output_rows = sum(len(frame) for frame in source_tables.values())
        stage.metrics["table_count"] = len(source_tables)
    if job.union_tables:
        # Before anything else: profiling, anomaly detection and base-table selection
        # should all see one dataset, not the fragment that happened to win.
        with telemetry.stage("union_tables") as stage:
            source_tables = _apply_union(
                source_tables, job.union_tables, prefer=job.base_table or ""
            )
            stage.output_rows = sum(len(frame) for frame in source_tables.values())
            stage.metrics["unioned_tables"] = len(job.union_tables)
    if job.melt is not None:
        # Before profiling and anomaly detection, like a union: every later step should
        # see the shape the user asked to analyse, not the one the report was laid out in.
        with telemetry.stage("reshape", input_rows=0) as stage:
            target = job.melt.table
            if target in source_tables:
                source_tables = {
                    **source_tables,
                    target: melt_wide_to_long(
                        source_tables[target],
                        value_columns=list(job.melt.value_columns),
                        variable_name=job.melt.variable_name,
                        value_name=job.melt.value_name,
                    ),
                }
            stage.output_rows = sum(len(frame) for frame in source_tables.values())
            stage.metrics["value_columns"] = len(job.melt.value_columns)

    tables = source_tables
    dirty_result = None
    # The dirty-data engine runs here and only here. It is gated by the goal-derived
    # dirty_data config, so an annotation-only goal ("标记异常，不要修改数据") never
    # rewrites cell values.
    if job.dirty_data.enabled:
        scanned_rows = sum(len(frame) for frame in source_tables.values())
        with telemetry.stage("detect_anomalies", input_rows=scanned_rows) as stage:
            dirty_result = analyze_dirty_data(
                source_tables,
                auto_fix_safe_issues=job.dirty_data.auto_fix_safe_issues,
            )
            tables = dirty_result.tables
            stage.metrics["issue_count"] = int(len(dirty_result.issue_summary))
            stage.metrics["auto_fixed"] = int(len(dirty_result.fix_audit))
    base_table = resolve_base_table(job, tables)
    main_df = tables[base_table]
    source_main_df = source_tables[base_table]
    input_rows = len(main_df)
    needs_internal_profile = job.export.include_internal_sheets and (
        output_spec is None or output_spec.include_audit
    )
    if profile_sheets_input is None and needs_internal_profile:
        profile_sheets_input = _resolve_profile_sheets(
            None,
            source_tables,
            telemetry,
        )
        if len(job.sources) + int(job.main_source is not None) > 1:
            telemetry.record(
                StageRecord(
                    capability_id="detect_relationships",
                    input_rows=input_rows,
                    output_rows=int(
                        len(profile_sheets_input.get("relationship_candidates", []))
                    ),
                )
            )
    elif profile_sheets_input is not None:
        # The caller already profiled these tables during planning; profiling again
        # here re-ran the O(tables² × columns²) relationship scan for nothing.
        telemetry.record(
            StageRecord(
                capability_id="profile_dataset",
                input_rows=input_rows,
                output_rows=input_rows,
                metrics={"reused_from_planning": True},
            )
        )
        if len(job.sources) + int(job.main_source is not None) > 1:
            telemetry.record(
                StageRecord(
                    capability_id="detect_relationships",
                    input_rows=input_rows,
                    output_rows=int(
                        len(profile_sheets_input.get("relationship_candidates", []))
                    ),
                    metrics={"reused_from_planning": True},
                )
            )

    traced_source_df = add_source_trace(source_main_df, source_table=base_table)
    traced_main_df = add_source_trace(main_df, source_table=base_table)
    if job.field_mapping.rename:
        with telemetry.stage("map_fields", input_rows=input_rows):
            working_df = apply_field_mapping(traced_main_df, rename=job.field_mapping.rename)
    else:
        working_df = apply_field_mapping(traced_main_df, rename=job.field_mapping.rename)

    lookups = resolve_lookups(job)
    lookup_match_fields = {}
    lookup_field_sources = {}
    formula_fills: list[dict[str, str]] = []
    for index, lookup in enumerate(lookups):
        lookup_df = resolve_lookup_table(lookup.source_table, lookup.source, tables)
        match_field = lookup.match_field or default_match_field(job, index, lookup.name)
        lookup_name = lookup.name or f"lookup_{index + 1}"
        lookup_match_fields[match_field] = lookup_name
        existing_fields = {str(column) for column in working_df.columns}
        for field in lookup.fields:
            output_field = _lookup_output_field(
                field,
                aliases=lookup.field_aliases,
                suffix=lookup.suffix,
                existing_fields=existing_fields,
            )
            lookup_field_sources[output_field] = f"lookup:{lookup_name}"
            existing_fields.add(output_field)
            if job.formula_output:
                # Recorded now because only this loop knows which source column fed
                # which delivered column; the workbook is assembled much later.
                formula_fills.append(
                    {
                        "output_field": output_field,
                        "left_key": str(
                            (lookup.left_keys or [lookup.left_key])[0]
                        ),
                        "right_key": str(
                            (lookup.right_keys or [lookup.right_key])[0]
                        ),
                        "source_field": str(field),
                        "source_table": str(lookup.source_table or lookup_name),
                    }
                )
        # Honor the global matching.duplicate_key_strategy as the default when a
        # lookup does not set its own strategy (previously this config was ignored).
        duplicate_strategy = lookup.duplicate_strategy or job.matching.duplicate_key_strategy
        with telemetry.stage(
            "lookup_fields",
            input_rows=len(working_df),
            metrics={"source_table": lookup.source_table or "", "match_mode": lookup.match_mode},
        ) as stage:
            working_df = vlookup(
                working_df,
                lookup_df,
                left_key=lookup.left_keys or lookup.left_key,
                right_key=lookup.right_keys or lookup.right_key,
                fields=lookup.fields,
                field_aliases=lookup.field_aliases,
                suffix=lookup.suffix,
                match_field=match_field,
                confidence_field=lookup.confidence_field,
                explanation_field=lookup.explanation_field,
                ambiguity_field=lookup.ambiguity_field,
                match_mode=lookup.match_mode,
                duplicate_strategy=duplicate_strategy,
                aggregate_sep=lookup.aggregate_sep,
                fuzzy_threshold=lookup.fuzzy_threshold,
            )
            stage.output_rows = len(working_df)
            stage.metrics["engine"] = working_df.attrs.get(
                "data_agent_engine",
                "pandas",
            )
            if match_field in working_df.columns:
                stage.metrics["matched_rows"] = int(
                    working_df[match_field].astype(str).ne("").sum()
                )

    if job.field_mapping.value_maps:
        with telemetry.stage("map_values", input_rows=len(working_df)):
            working_df = apply_field_mapping(
                working_df, value_maps=job.field_mapping.value_maps
            )
    else:
        working_df = apply_field_mapping(working_df, value_maps=job.field_mapping.value_maps)
    for rollup in job.rollups:
        # After the lookups, so a rollup can key off a column another lookup brought
        # across. Row count is untouched by construction: the aggregate has one row per
        # key, unlike joining the detail table itself, which multiplies the master.
        with telemetry.stage(
            "rollup",
            input_rows=len(working_df),
            metrics={"source_table": rollup.source_table, "agg": rollup.agg},
        ) as stage:
            detail = resolve_lookup_table(rollup.source_table, None, tables)
            working_df = rollup_to_master(
                working_df,
                detail,
                left_key=rollup.left_key,
                right_key=rollup.right_key,
                measure=rollup.measure,
                agg=rollup.agg,
                output=rollup.output,
            )
            stage.output_rows = len(working_df)
            stage.metrics["engine"] = working_df.attrs.get(
                "data_agent_engine",
                "pandas",
            )
        lookup_field_sources[rollup.output] = f"rollup:{rollup.source_table}"

    pre_formula_df = working_df.copy()
    working_df = _apply_formulas_recorded(working_df, job.formulas, telemetry)
    formula_outputs = {
        formula.output: f"formula:{formula.op}"
        for formula in job.formulas
        if formula.op not in {"filter_rows", "dedupe"}
    }
    if job.anomaly_rules:
        with telemetry.stage("validate_rules", input_rows=len(working_df)) as stage:
            working_df = apply_anomaly_rules(
                working_df,
                job.anomaly_rules,
                abnormal_field=job.abnormal_field,
            )
            stage.metrics["flagged_rows"] = int(
                working_df.get(job.abnormal_field, pd.Series(dtype=str))
                .fillna("")
                .astype(str)
                .ne("")
                .sum()
            )
    else:
        working_df = apply_anomaly_rules(
            working_df,
            job.anomaly_rules,
            abnormal_field=job.abnormal_field,
        )
    working_df, critical_dirty_reasons = _flag_dirty_review_rows(
        working_df,
        dirty_result,
        base_table=base_table,
        abnormal_field=job.abnormal_field,
        enabled=job.dirty_data.enabled and job.dirty_data.mark_uncertain_for_review,
    )

    abnormal_field = job.abnormal_field
    issue_scope = _issue_scope(job)
    explicit_issue_reasons = _task_authorized_rule_reasons(job, task_spec)
    # exception_policy drives which flagged rows may still enter the final result.
    cleaned_df, abnormal_df = _split_by_exception_policy(
        working_df,
        job,
        abnormal_field=abnormal_field,
        extra_critical_reasons=critical_dirty_reasons,
        issue_scope=issue_scope,
        explicit_issue_reasons=explicit_issue_reasons,
    )
    summary_df = build_summary(main_df, working_df, cleaned_df, abnormal_df)

    # Only these three feed the deliverable, so only these are unconditional. The rest
    # of the audit stack used to be computed on every run and thrown away unless
    # internal sheets were requested — including build_field_change_audit, which walks
    # every row × every column in Python.
    wants_internal = job.export.include_internal_sheets and (
        output_spec is None or output_spec.include_audit
    )
    # Every flagged row is worth reporting, not only the ones held back. Auditing
    # `abnormal_df` alone tied "what went wrong" to "what was withheld", so once the
    # exception policy stopped withholding by default, a lookup that matched nothing
    # became invisible: the user got a blank column and no line anywhere saying why.
    # Where a row is delivered and whether its problem is reported are two decisions.
    flagged_df = _flagged_rows(
        working_df,
        abnormal_field,
        issue_scope,
        explicit_issue_reasons,
    )
    record_audit = build_record_audit(
        flagged_df,
        abnormal_field=job.abnormal_field,
        lookup_match_fields=lookup_match_fields,
        rule_fields={
            (rule.reason or rule.name): rule.condition.field
            for rule in job.anomaly_rules
        },
    )
    annotation_tasks = build_annotation_tasks(record_audit)
    issue_summary = build_issue_summary(annotation_tasks)
    review_tasks = build_review_tasks(annotation_tasks)
    business_summary = build_business_summary(summary_df, issue_summary)
    # Names the user uploaded. They are never dropped by the delivery view, whatever
    # this pipeline happens to call its own annotation columns.
    source_columns = {
        str(column) for frame in source_tables.values() for column in frame.columns
    }
    business_result = build_business_result(
        cleaned_df,
        abnormal_df,
        review_tasks,
        abnormal_field=job.abnormal_field,
    )

    empty = pd.DataFrame()
    abnormal_pivot = empty
    label_summary = empty
    annotation_summary = empty
    annotation_taxonomy = empty
    field_change_audit = empty
    row_action_audit = empty
    profile_sheets: dict[str, pd.DataFrame] = {}
    if wants_internal:
        if job.analysis.enabled:
            with telemetry.stage("pivot", input_rows=len(abnormal_df)) as stage:
                abnormal_pivot = build_pivot(
                    abnormal_df, job.pivot.group_by, job.pivot.metrics
                )
                stage.output_rows = len(abnormal_pivot)
                stage.metrics["engine"] = abnormal_pivot.attrs.get(
                    "data_agent_engine",
                    "pandas",
                )
        else:
            abnormal_pivot = build_pivot(abnormal_df, job.pivot.group_by, job.pivot.metrics)
        label_summary = build_label_summary(record_audit)
        annotation_summary = build_annotation_summary(annotation_tasks)
        annotation_taxonomy = build_annotation_taxonomy(record_audit)
        field_change_audit = build_field_change_audit(
            source_df=traced_source_df,
            result_df=working_df,
            lookup_field_sources=lookup_field_sources,
            formula_outputs=formula_outputs,
        )
        row_action_audit = _build_row_action_audit(
            pre_formula_df,
            working_df,
            job.formulas,
        )
        profile_sheets = dict(profile_sheets_input or {})
    # P2 minimal output: the deliverable is the cleaned result table only. The
    # 问题汇总 metrics sheet is process information (its counts/score reach the user
    # via the report/API), so it is opt-in via include_summary_sheet, and always
    # present under the full internal-sheet audit export.
    #
    # Every declared sheet is built here, through the shared delivery projections, so
    # the workbook is identical whichever entry point asked for it.
    primary_result = (
        delivery_result_sheet(business_result, source_columns)
        if output_spec
        else business_result
    )
    output_sheets = {
        (
            output_spec.primary_artifact.name if output_spec else "处理结果"
        ): primary_result,
    }
    trusted_formula_fields: set[str] = set()
    if job.target_schema:
        # The user declared the shape — an uploaded form's headers, or a column list
        # they wrote out. Project onto it exactly: same names, same order, unfilled
        # columns left blank rather than dropped. That file usually goes somewhere
        # afterwards, and a renamed or missing column breaks whatever waits there.
        with telemetry.stage("project_schema", input_rows=len(primary_result)) as stage:
            primary_result = project_onto_schema(primary_result, job.target_schema)
            stage.output_rows = len(primary_result)
            stage.metrics["schema_columns"] = len(job.target_schema)
            stage.metrics["unfilled_columns"] = sum(
                1
                for column in job.target_schema
                if column in primary_result.columns
                and is_blank_column(primary_result[column])
            )
        output_sheets[
            output_spec.primary_artifact.name if output_spec else "处理结果"
        ] = primary_result

    if job.formula_output and formula_fills:
        # After the delivery projection, because the formula addresses the *delivered*
        # sheet's column positions — not the working frame's.
        with telemetry.stage("write_formulas", input_rows=len(primary_result)) as stage:
            primary_result, formula_sheets, trusted_formula_fields = _apply_formula_fills(
                primary_result, formula_fills, tables
            )
            output_sheets.update(formula_sheets)
            stage.output_rows = len(primary_result)
            stage.metrics["formula_columns"] = len(formula_fills)
        output_sheets[
            output_spec.primary_artifact.name if output_spec else "处理结果"
        ] = primary_result

    import_view = pd.DataFrame()
    if output_spec and output_spec.include_review_view:
        output_sheets["问题说明"] = delivery_problem_sheet(business_result, source_columns)
    if output_spec and output_spec.aggregate is not None:
        with telemetry.stage("aggregate", input_rows=len(primary_result)) as stage:
            output_sheets[AGGREGATE_ARTIFACT] = _aggregate_sheet(
                primary_result, output_spec.aggregate
            )
            stage.output_rows = len(output_sheets[AGGREGATE_ARTIFACT])
            stage.metrics["engine"] = output_sheets[AGGREGATE_ARTIFACT].attrs.get(
                "data_agent_engine",
                "pandas",
            )
            # The detail sheet keeps these rows; the totals do not count them. Recorded
            # so the difference between the two row counts is explainable.
            stage.metrics["excluded_non_data_rows"] = int(
                non_data_row_mask(primary_result).sum()
            )
            # Cells the measure column could not be read as numbers at all — "待确认"
            # in an amount column and the like. Excel's SUM skips them too, but a total
            # quietly built from fewer rows than the user thinks is the same class of
            # problem as the summary that read "1,234" as zero, so it is recorded next
            # to the other reason the two sheets can disagree.
            stage.metrics["unreadable_measure_values"] = _unreadable_measure_values(
                primary_result, output_spec.aggregate
            )
    if output_spec and output_spec.include_change_manifest:
        manifest = build_change_manifest(
            _dirty_frame(dirty_result, "fix_audit"),
            removed_row_count=max(len(pre_formula_df) - len(working_df), 0),
            removal_reason=_removal_reason(job.formulas, _business_column_count(pre_formula_df)),
        )
        # Nothing changed means nothing to account for; an empty sheet would be noise.
        if not manifest.empty:
            output_sheets[CHANGE_MANIFEST_ARTIFACT] = manifest
    if output_spec and output_spec.include_import_view:
        import_view, _mapping = system_import_views(
            primary_result,
            import_requirements if import_requirements is not None else pd.DataFrame(),
            template_validation if template_validation is not None else pd.DataFrame(),
        )
        output_sheets[IMPORT_VIEW_SHEET] = import_view
    if output_spec:
        lint_output(
            output_spec,
            sheets={
                sheet_name: [str(column) for column in frame.columns]
                for sheet_name, frame in output_sheets.items()
            },
            row_counts={
                sheet_name: len(frame)
                for sheet_name, frame in output_sheets.items()
            },
            allowed_fields=(
                _declared_output_fields(job, tables)
                | _import_view_fields(import_view)
                | _aggregate_fields(
                    output_spec, output_sheets.get(AGGREGATE_ARTIFACT)
                )
                | (set(MANIFEST_COLUMNS) if output_spec.include_change_manifest else set())
            ),
        )
    if output_spec is None and (
        job.export.include_summary_sheet or job.export.include_internal_sheets
    ):
        output_sheets = {"问题汇总": business_summary, **output_sheets}

    internal_workbook: Path | None = None
    internal_sheets: dict[str, pd.DataFrame] = {}
    if job.export.include_internal_sheets and (
        output_spec is None or output_spec.include_audit
    ):
        internal_sheets = {
            "cleaned_data": simplify_result_data(cleaned_df),
            "abnormal_data": simplify_result_data(abnormal_df),
            "summary": summary_df,
            "abnormal_pivot": abnormal_pivot,
            "issue_summary": issue_summary,
            "review_tasks": review_tasks,
            **dict(profile_sheets),
            "record_audit": record_audit,
            "label_summary": label_summary,
            "annotation_tasks": annotation_tasks,
            "annotation_summary": annotation_summary,
            "annotation_taxonomy": annotation_taxonomy,
            "field_change_audit": field_change_audit,
            "row_action_audit": row_action_audit,
        }
        if job.export.include_source_tables:
            internal_sheets.update({f"source_{name}": table for name, table in tables.items()})

    if internal_sheets and output_spec is None:
        # Raw `data-agent run <config>` has no delivery contract to protect, so it
        # keeps the historical single-workbook layout.
        output_sheets.update(internal_sheets)

    with telemetry.stage("export_table", input_rows=len(business_result)) as stage:
        primary_sheet_name = (
            output_spec.primary_artifact.name if output_spec else "处理结果"
        )
        output_file = export_workbook(
            job.export.output_file,
            output_sheets,
            trusted_formula_columns=(
                {primary_sheet_name: trusted_formula_fields}
                if trusted_formula_fields
                else None
            ),
        )
        stage.output_rows = len(primary_result)
        stage.metrics["sheets"] = sorted(output_sheets)

    if internal_sheets and output_spec is not None:
        # Under an OutputSpec the engineering sheets get their own workbook. They used
        # to be appended to the deliverable *after* lint_output ran, which both
        # bypassed the output contract and shipped 20 internal sheets inside the file
        # a business user downloads.
        with telemetry.stage("build_audit") as stage:
            internal_workbook = export_workbook(
                output_file.with_name(f"{output_file.stem}_internal{output_file.suffix}"),
                internal_sheets,
            )
            stage.metrics["sheet_count"] = len(internal_sheets)

    return RunResult(
        output_file=output_file,
        cleaned_count=len(cleaned_df),
        abnormal_count=len(abnormal_df),
        summary=summary_df,
        abnormal_pivot=abnormal_pivot,
        table_profile=(
            profile_sheets.get("table_profile")
            if "table_profile" in profile_sheets
            # Cheap per-table stats only. The expensive part of profile_dataset is the
            # cross-table relationship scan, which the deliverable never needs.
            else profile_tables(source_tables)["table_profile"]
        ),
        business_summary=business_summary,
        business_result=business_result,
        dirty_issue_summary=_dirty_frame(dirty_result, "issue_summary"),
        dirty_record_issues=_dirty_frame(dirty_result, "record_issues"),
        dirty_fix_audit=_dirty_frame(dirty_result, "fix_audit"),
        internal_workbook=internal_workbook,
        stages=telemetry.stages,
    )


def _resolve_profile_sheets(
    provided: dict[str, pd.DataFrame] | None,
    source_tables: dict[str, pd.DataFrame],
    telemetry: ExecutionRecorder,
) -> dict[str, pd.DataFrame]:
    """Reuse the caller's profile when it has one; otherwise compute it here.

    Only the internal-sheet export needs the full profile, so a normal run never pays
    for a second profiling pass over the same tables.
    """

    if provided is not None:
        return dict(provided)
    with telemetry.stage(
        "profile_dataset",
        input_rows=sum(len(frame) for frame in source_tables.values()),
    ):
        return profile_dataset(source_tables)


def _apply_formulas_recorded(
    working_df: pd.DataFrame,
    formulas,
    telemetry: ExecutionRecorder,
) -> pd.DataFrame:
    """Apply formulas one stage at a time so each records its own real row delta.

    Row-deleting operations are the ones users most need honest numbers for, so each
    filter/dedupe reports exactly how many rows it removed.
    """

    if not formulas:
        return apply_formulas(working_df, formulas)
    result = working_df
    for formula in formulas:
        capability_id = get_capability_registry().resolve_formula_operator(
            formula.op
        ).capability_id
        with telemetry.stage(
            capability_id,
            input_rows=len(result),
            metrics={"op": formula.op, "output": formula.output},
        ) as stage:
            result = apply_formulas(result, [formula])
            stage.output_rows = len(result)
            stage.metrics["engine"] = result.attrs.get(
                "data_agent_last_engine",
                "pandas",
            )
            if capability_id in {"filter_rows", "deduplicate"}:
                stage.metrics["removed_rows"] = int(stage.input_rows - stage.output_rows)
    return result


def _dirty_frame(dirty_result: DirtyDataResult | None, name: str) -> pd.DataFrame:
    if dirty_result is None:
        return pd.DataFrame()
    value = getattr(dirty_result, name)
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _flag_dirty_review_rows(
    working_df: pd.DataFrame,
    dirty_result: DirtyDataResult | None,
    *,
    base_table: str,
    abnormal_field: str,
    enabled: bool,
) -> tuple[pd.DataFrame, set[str]]:
    """Attach dirty-data findings to the rows they affect.

    This used to live only in the CLI delivery path, so the same data delivered
    through the API silently kept rows the CLI would have held back. Doing it inside
    the single deterministic executor makes every entry point deliver the same rows.

    Findings are tagged, not judged, here: the returned set names the reasons that
    count as *critical*, so ``exception_policy`` stays the single arbiter of what may
    still be delivered. A finding that only ``requires_review`` remains minor and is
    delivered with an advisory tag when ``include_minor_in_final`` is set.

    Each reason carries the field it is about, so the audit layer can drop it when
    an explicit rule already describes the same cell — a cell is never labelled
    twice ("amount 小于允许的最小值；金额为负") — while its severity still counts.
    """

    if not enabled or dirty_result is None or working_df.empty:
        return working_df, set()
    issues = dirty_result.record_issues
    if issues.empty or "_source_row_index" not in working_df.columns:
        return working_df, set()
    if "table" in issues.columns and base_table:
        issues = issues[issues["table"].eq(base_table)]
    issues = issues[
        issues["requires_review"].astype(bool)
        | issues["affects_final_result"].astype(bool)
    ]
    if issues.empty:
        return working_df, set()

    # Reasons carry their field (``dirty:<field>:<issue>``) so the audit layer can
    # tell which cell a finding is about — and can hide it when an explicit rule
    # already describes that same cell.
    issues = issues.assign(
        _reason=DIRTY_ISSUE_PREFIX
        + issues["field"].astype(str)
        + ":"
        + issues["issue_type"].astype(str)
    )
    critical = set(
        issues.loc[
            issues["affects_final_result"].astype(bool)
            | issues["severity"].astype(str).eq("error"),
            "_reason",
        ].unique()
    )
    reason_by_row = issues.groupby("row_index")["_reason"].apply(
        lambda values: ";".join(dict.fromkeys(str(item) for item in values))
    )
    extra = working_df["_source_row_index"].map(reason_by_row).fillna("")
    if not extra.any():
        return working_df, set()

    result = working_df.copy()
    existing = (
        result[abnormal_field].fillna("").astype(str)
        if abnormal_field in result.columns
        else pd.Series("", index=result.index)
    )
    result[abnormal_field] = [
        ";".join(part for part in (current, added) if part)
        for current, added in zip(existing, extra, strict=True)
    ]
    return result, critical


def _build_row_action_audit(
    before: pd.DataFrame,
    after: pd.DataFrame,
    formulas,
) -> pd.DataFrame:
    columns = [
        "source_table",
        "source_row_index",
        "action",
        "rule_config",
        "reason_code",
    ]
    destructive = [
        formula
        for formula in formulas
        if formula.op in {"filter_rows", "dedupe"}
    ]
    if not destructive or before.empty or "_source_row_index" not in before.columns:
        return pd.DataFrame(columns=columns)

    remaining = {
        (str(row.get("_source_table", "")), row.get("_source_row_index"))
        for _, row in after.iterrows()
    }
    action = "+".join(sorted({formula.op for formula in destructive}))
    rule_config = json.dumps(
        [formula.model_dump(mode="json") for formula in destructive],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    rows = []
    for _, row in before.iterrows():
        identity = (
            str(row.get("_source_table", "")),
            row.get("_source_row_index"),
        )
        if identity in remaining:
            continue
        rows.append(
            {
                "source_table": identity[0],
                "source_row_index": identity[1],
                "action": action,
                "rule_config": rule_config,
                "reason_code": "ROW_REMOVED_BY_TASK_RULE",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def run_job_from_file(config_path: str | Path) -> RunResult:
    return run_job(load_job_config(config_path))


def profile_job(job: JobConfig) -> dict[str, pd.DataFrame]:
    return profile_dataset(load_tables(job))


def profile_job_from_file(config_path: str | Path) -> dict[str, pd.DataFrame]:
    return profile_job(load_job_config(config_path))


def load_tables(job: JobConfig) -> dict[str, pd.DataFrame]:
    tables = {name: read_table(source.path, source.sheet) for name, source in job.sources.items()}

    if job.main_source and "main" not in tables:
        tables["main"] = read_table(job.main_source.path, job.main_source.sheet)

    if not tables:
        raise ValueError("Job requires sources or main_source")

    return tables


def resolve_base_table(job: JobConfig, tables: dict[str, pd.DataFrame]) -> str:
    if job.base_table:
        if job.base_table not in tables:
            raise KeyError(f"base_table not found in sources: {job.base_table}")
        return job.base_table
    if job.main_source:
        return "main"
    return next(iter(tables))


def resolve_lookups(job: JobConfig):
    lookups = list(job.lookups)
    if job.lookup:
        lookups.insert(0, job.lookup)
    return lookups


def resolve_lookup_table(source_table, source, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if source_table:
        if source_table not in tables:
            raise KeyError(f"lookup source_table not found: {source_table}")
        return tables[source_table]
    if source:
        return read_table(source.path, source.sheet)
    raise ValueError("Lookup requires source_table or source")


def default_match_field(job: JobConfig, lookup_index: int, lookup_name: str) -> str:
    if job.lookup and lookup_index == 0 and not job.lookups:
        return "_lookup_matched"
    return f"_lookup_matched_{lookup_name}"


def _declared_output_fields(
    job: JobConfig,
    tables: dict[str, pd.DataFrame],
) -> set[str]:
    fields = {
        str(column)
        for table in tables.values()
        for column in table.columns
    }
    fields.update(job.field_mapping.rename.values())
    fields.update(job.field_mapping.value_maps)
    # A rollup's output is as declared as a lookup's: the plan named it, so the
    # delivery contract has to know it is coming.
    fields.update(rollup.output for rollup in job.rollups)
    for lookup in resolve_lookups(job):
        for field in lookup.fields:
            output_field = lookup.field_aliases.get(field, field)
            fields.add(output_field)
            fields.add(f"{output_field}{lookup.suffix}")
    fields.update(
        formula.output
        for formula in job.formulas
        if formula.op not in {"filter_rows", "dedupe"}
    )
    fields.update({job.abnormal_field, "source_row_index", *REVIEW_COLUMNS})
    return fields


def _aggregate_sheet(
    delivered: pd.DataFrame,
    spec,
) -> pd.DataFrame:
    """Group the delivered rows into the summary shape the goal declared.

    Built from the same rows the user receives, so the totals always reconcile with
    the detail sheet — minus the rows that are not records at all. A 合计 row in the
    upload was being added into the very total it summarises, and a repeated header row
    became a category called "城市"; both stay in the detail sheet, neither belongs in
    the arithmetic.
    """

    metrics = [PivotMetric.model_validate(metric) for metric in spec.metrics] or [
        PivotMetric(agg="count", output_name="记录数")
    ]
    # Drop the non-records first. Adding the derived dimension beforehand filled in the
    # blank row's last cell, so it stopped looking blank and came back as its own
    # "日期缺失或无法识别" bucket.
    countable = delivered[~non_data_row_mask(delivered)] if not delivered.empty else delivered
    if countable.empty:
        countable = delivered
    working = _with_time_bucket(countable, spec.time_bucket)
    group_by = [field for field in spec.group_by if field in working.columns]
    if working.empty or (not group_by and spec.group_by):
        return pd.DataFrame(columns=[*spec.group_by, *(m.output_name for m in metrics)])
    if not group_by:
        return _grand_total(working, metrics)
    column_field = str(getattr(spec, "column_field", "") or "")
    if column_field and column_field in working.columns:
        return build_crosstab(working, group_by, column_field, metrics[0])
    summary = build_pivot(working, group_by, metrics)
    return _apply_top_n(summary, spec, metrics)


# Placeholder dimension for a total over everything. Grouping on a constant reuses the
# pivot path — including its business-number parsing — so a grand total and a breakdown
# of the same column can never disagree.
_TOTAL_DIMENSION = "_total"


def _grand_total(working: pd.DataFrame, metrics: list[PivotMetric]) -> pd.DataFrame:
    """One row: each measure computed over every delivered record."""

    framed = working.assign(**{_TOTAL_DIMENSION: "全部"})
    return build_pivot(framed, [_TOTAL_DIMENSION], metrics).drop(columns=[_TOTAL_DIMENSION])


def _apply_formula_fills(
    delivered: pd.DataFrame,
    fills: list[dict[str, str]],
    tables: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], set[str]]:
    """Replace filled values with live VLOOKUP formulas and ship what they point at.

    「用 VLOOKUP 匹配」 asks for a workbook that shows its work: the user clicks the
    cell, reads the formula, and the fill is verified the way they verify their own
    spreadsheet — no 核对 sheet, nothing extra to read. A formula whose source table is
    missing from the workbook is broken, so the source ships with it; that sheet is
    part of what was requested, not an extra nobody asked for.
    """

    if delivered.empty:
        return delivered, {}, set()

    result = delivered.copy()
    columns = [str(column) for column in result.columns]
    sheets: dict[str, pd.DataFrame] = {}
    trusted_formula_fields: set[str] = set()
    for fill in fills:
        output_field = fill["output_field"]
        source_table = fill["source_table"]
        source_df = tables.get(source_table)
        if output_field not in columns or source_df is None or source_df.empty:
            continue
        source_columns = [str(column) for column in source_df.columns]
        if fill["left_key"] not in columns:
            continue
        if fill["right_key"] not in source_columns or fill["source_field"] not in source_columns:
            continue
        result[output_field] = formula_column(
            row_count=len(result),
            result_columns=columns,
            left_key=fill["left_key"],
            source_sheet=source_table,
            source_columns=source_columns,
            right_key=fill["right_key"],
            value_field=fill["source_field"],
        )
        trusted_formula_fields.add(output_field)
        sheets[source_table] = source_df.reset_index(drop=True)
    return result, sheets, trusted_formula_fields

def _apply_union(
    tables: dict[str, pd.DataFrame],
    names: list[str],
    prefer: str = "",
) -> dict[str, pd.DataFrame]:
    """Replace the named tables with a single stacked one.

    The combined table keeps the name the plan already points at — base_table was
    chosen before the union and naming the result anything else makes the plan
    reference a table that no longer exists. Row origin survives in ``_source_table``,
    so a total can still be traced back to the file it came from.
    """

    present = [name for name in names if name in tables]
    if len(present) < 2:
        return tables
    combined = union_tables(tables, present)
    kept = prefer if prefer in present else present[0]
    merged = {name: frame for name, frame in tables.items() if name not in set(present)}
    return {kept: combined, **merged}


def _unreadable_measure_values(delivered: pd.DataFrame, spec) -> int:
    """Non-blank cells in the aggregated columns that are not numbers in any notation."""

    if delivered.empty:
        return 0
    countable = delivered[~non_data_row_mask(delivered)]
    columns = {
        str(metric.get("column") or "")
        for metric in spec.metrics
        if str(metric.get("agg") or "") != "count"
    }
    return sum(
        unparseable_count(countable[column])
        for column in columns
        if column and column in countable.columns
    )


def _apply_top_n(summary: pd.DataFrame, spec, metrics) -> pd.DataFrame:
    """Keep the highest-ranking rows the goal asked for, per group or overall."""

    if not spec.top_n or summary.empty or not metrics:
        return summary
    measure = next(
        (metric.output_name for metric in metrics if metric.output_name in summary.columns),
        "",
    )
    if not measure:
        return summary
    partition = [field for field in spec.rank_within if field in summary.columns]
    ranked = summary.sort_values(by=measure, ascending=False, kind="stable")
    if partition:
        ranked = ranked.groupby(partition, dropna=False, sort=False).head(spec.top_n)
        return ranked.sort_values(
            by=[*partition, measure], ascending=[True] * len(partition) + [False],
            kind="stable", ignore_index=True,
        )
    return ranked.head(spec.top_n).reset_index(drop=True)


# How each granularity reads back to a business user: 2024-03, 2024-Q1, 2024, 2024-W09.
_BUCKET_FORMATS = {
    "month": lambda values: values.dt.strftime("%Y-%m"),
    "year": lambda values: values.dt.strftime("%Y"),
    "quarter": lambda values: (
        values.dt.year.astype(str) + "-Q" + values.dt.quarter.astype(str)
    ),
    "week": lambda values: (
        values.dt.isocalendar().year.astype(str)
        + "-W"
        + values.dt.isocalendar().week.astype(str).str.zfill(2)
    ),
}
UNPARSEABLE_DATE_LABEL = "日期缺失或无法识别"


def _with_time_bucket(delivered: pd.DataFrame, time_bucket: dict[str, str]) -> pd.DataFrame:
    """Add the derived 月份/季度/年份 dimension, for this summary only."""

    field = time_bucket.get("field")
    output_name = time_bucket.get("output_name")
    formatter = _BUCKET_FORMATS.get(time_bucket.get("granularity", ""))
    if not field or not output_name or formatter is None or field not in delivered.columns:
        return delivered

    parsed = pd.to_datetime(delivered[field], errors="coerce", format="mixed")
    working = delivered.copy()
    # Rows whose date cannot be read are neither dropped nor filed under a wrong period;
    # they group under a label that says exactly what happened. Formatting runs only over
    # the rows that parsed — NaT leaks through .year as a float and prints "2024.0-Q1.0".
    labels = pd.Series(UNPARSEABLE_DATE_LABEL, index=delivered.index, dtype=object)
    valid = parsed.dropna()
    if not valid.empty:
        labels.loc[valid.index] = formatter(valid).astype(str)
    working[output_name] = labels
    return working


def _aggregate_fields(
    output_spec: OutputSpec | None,
    aggregate_sheet: pd.DataFrame | None = None,
) -> set[str]:
    """Columns the summary sheet is allowed to carry.

    A crosstab's column headers are *values*, not field names — 「行是城市，列是月份」
    produces one column per month present in the data, which no plan can enumerate in
    advance. The contract still holds: rows are the declared group_by, columns are the
    distinct values of the declared column_field. So the produced headers are accepted
    here rather than the whole sheet being exempted from field checking.
    """

    if output_spec is None or output_spec.aggregate is None:
        return set()
    fields = {
        *output_spec.aggregate.group_by,
        *(
            str(metric.get("output_name"))
            for metric in output_spec.aggregate.metrics
            if metric.get("output_name")
        ),
        "记录数",
    }
    if output_spec.aggregate.column_field and aggregate_sheet is not None:
        fields.update(str(column) for column in aggregate_sheet.columns)
    return fields


def _import_view_fields(import_view: pd.DataFrame) -> set[str]:
    """Columns the import projection introduces, so the linter can account for them."""

    if import_view.empty:
        return set()
    return {str(column) for column in import_view.columns}


def _lookup_output_field(
    field: str,
    *,
    aliases: dict[str, str],
    suffix: str,
    existing_fields: set[str],
) -> str:
    output_field = str(aliases.get(field, field))
    return f"{output_field}{suffix}" if output_field in existing_fields else output_field


def _issue_scope(job: JobConfig) -> frozenset[str] | None:
    """What this job is allowed to report problems about.

    A JobConfig with no goal text was written by hand or by an API caller, so its
    anomaly_rules and exception_policy *are* the explicit request — reading a scope
    out of an empty string would silently switch them off.
    """

    if not job.goal:
        return frozenset()
    return requested_issue_kinds(job.goal)


def _task_authorized_rule_reasons(
    job: JobConfig,
    task_spec: dict | None,
) -> frozenset[str]:
    """Trust rule scope already admitted by the typed intent contract.

    Re-parsing the sentence in the runner made semantic plans lose phrases such as
    “缺少” and “没有值” merely because a detector vocabulary omitted those exact
    words. A hand-authored JobConfig has no TaskSpec and is explicitly trusted.
    """

    if task_spec is None:
        return frozenset(
            str(rule.reason or rule.name)
            for rule in job.anomaly_rules
            if str(rule.reason or rule.name)
        )
    actions = {str(action) for action in task_spec.get("actions") or []}
    if not actions & {"validate", "review", "annotate"}:
        return frozenset()
    target_fields = {
        str(item.get("field") or "")
        for item in task_spec.get("target_fields") or []
        if isinstance(item, dict) and str(item.get("field") or "")
    }
    # A typed validation action authorises rules only for the fields the TaskSpec
    # actually names. Authorising every recommender rule made a request about one
    # unmatched lookup also report duplicate keys and negative amounts merely because
    # the same plan happened to contain those diagnostics.
    return frozenset(
        str(rule.reason or rule.name)
        for rule in job.anomaly_rules
        if str(rule.reason or rule.name) and rule.condition.field in target_fields
    )


def _reason_is_authorized(
    reason: str,
    scope: frozenset[str] | None,
    explicit_reasons: frozenset[str],
) -> bool:
    # A concrete issue kind in the user's words is the narrowest authority and must
    # win over broad target-field validation. TaskSpec field evidence is the fallback
    # only when the deterministic issue vocabulary could not see any problem request.
    if scope is not None:
        return reason_in_scope(reason, scope)
    return reason in explicit_reasons


def _business_column_count(df: pd.DataFrame) -> int:
    """Columns the user would recognise, excluding the pipeline's own trace fields."""

    return sum(1 for column in df.columns if not str(column).startswith("_"))


# Comparison operators as a business user would read them back.
_OPERATOR_WORDS = {
    "gt": "大于",
    "gte": "不小于",
    "lt": "小于",
    "lte": "不大于",
    "equals": "等于",
    "not_equals": "不等于",
    "in": "属于",
    "not_in": "不属于",
    "contains": "包含",
    "not_contains": "不包含",
    "is_blank": "为空",
    "not_blank": "不为空",
}


def _removal_reason(formulas, column_count: int = 0) -> str:
    """Say why rows went, in the words the goal used rather than the plan's.

    The first version read "不满足 金额 lt 0 的行未保留" — a double negative wrapped
    around a raw operator id, describing the compiled condition instead of the request.
    """

    reasons = []
    for formula in formulas:
        if formula.op == "dedupe":
            keys = [str(column) for column in formula.columns]
            whole_row = bool(column_count) and len(keys) >= column_count
            named = "、".join(keys)
            reasons.append("整行完全重复" if whole_row or not keys else f"按 {named} 重复")
        elif formula.op == "filter_rows" and formula.condition is not None:
            condition = formula.condition
            operator = _OPERATOR_WORDS.get(condition.op, condition.op)
            phrase = f"{condition.field}{operator}{condition.value}"
            reasons.append(phrase if condition.negate else f"不满足「{phrase}」")
    return "；".join(reasons)


def _flagged_rows(
    working_df: pd.DataFrame,
    abnormal_field: str,
    scope: frozenset[str] | None,
    explicit_reasons: frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """Rows carrying a reason the user asked about, withheld or not.

    Reasons outside the scope are stripped from the row rather than the row being
    dropped, so a record that is genuinely unmatched still reports *that* and not the
    four unrelated findings that happened to land on it too.
    """

    if abnormal_field not in working_df.columns:
        return working_df.iloc[0:0].copy()
    reasons = working_df[abnormal_field].fillna("").astype(str)
    in_scope = reasons.map(
        lambda text: ";".join(
            part
            for part in text.split(";")
            if part and _reason_is_authorized(part, scope, explicit_reasons)
        )
    )
    flagged = working_df[in_scope.ne("")].copy()
    flagged[abnormal_field] = in_scope[in_scope.ne("")]
    return flagged


def _split_by_exception_policy(
    working_df: pd.DataFrame,
    job: JobConfig,
    abnormal_field: str,
    extra_critical_reasons: set[str] | None = None,
    issue_scope: frozenset[str] | None = None,
    explicit_issue_reasons: frozenset[str] = frozenset(),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split records into final vs needs-review according to exception_policy.

    Rows with no flag are always final. Flagged rows are routed by the policy:
    - exclude_critical_from_final: rows carrying any error-severity reason go to review.
    - include_minor_in_final: warn-only rows may stay in the final result.
    - require_review_for_unmatched_lookup: any unmatched lookup forces review.

    ``extra_critical_reasons`` lets the dirty-data engine contribute error-severity
    findings without bypassing the policy: they are judged by the same rules as
    rule violations, so one policy governs every source of exceptions.
    """

    policy = job.exception_policy
    reason_series = working_df.get(abnormal_field, pd.Series("", index=working_df.index))
    reason_series = reason_series.fillna("").astype(str)
    flagged = reason_series.ne("")

    error_reasons = {
        (rule.reason or rule.name) for rule in job.anomaly_rules if rule.severity == "error"
    }
    error_reasons |= extra_critical_reasons or set()

    # Only the problems the goal asked about can hold a row back. "列出关联不上的记录"
    # used to withhold all 15 rows, because every one of them carried some unrelated
    # finding (备注 为空, 数字存成文本, a repeated foreign key…). The delivery came back
    # empty and the one line the user wanted was buried among the rest.
    scope = issue_scope if issue_scope is not None else _issue_scope(job)

    def _row_needs_review(reason_text: str) -> bool:
        if not reason_text:
            return False
        reasons = [
            part
            for part in reason_text.split(";")
            if part and _reason_is_authorized(part, scope, explicit_issue_reasons)
        ]
        if not reasons:
            return False
        has_error = any(reason in error_reasons for reason in reasons)
        # Lookup-not-found reasons carry a "_not_found" suffix by convention.
        has_unmatched = any(reason.endswith("_not_found") for reason in reasons)
        if policy.require_review_for_unmatched_lookup and has_unmatched:
            return True
        if policy.exclude_critical_from_final and has_error:
            return True
        if not policy.include_minor_in_final:
            return True
        # Only minor (warn) issues remain and the policy keeps them in the final set.
        return False

    review_mask = pd.Series(
        [
            _row_needs_review(reason_series.loc[idx]) if flag else False
            for idx, flag in flagged.items()
        ],
        index=working_df.index,
    )
    cleaned_df = working_df[~review_mask].copy()
    abnormal_df = working_df[review_mask].copy()
    return cleaned_df, abnormal_df


def build_summary(
    source_df: pd.DataFrame,
    working_df: pd.DataFrame,
    cleaned_df: pd.DataFrame,
    abnormal_df: pd.DataFrame,
) -> pd.DataFrame:
    filtered_out = max(0, len(source_df) - len(working_df))
    return pd.DataFrame(
        [
            {"metric": "total", "value": int(len(source_df))},
            {"metric": "output", "value": int(len(working_df))},
            {"metric": "filtered_out", "value": int(filtered_out)},
            {"metric": "valid", "value": int(len(cleaned_df))},
            {"metric": "abnormal", "value": int(len(abnormal_df))},
        ]
    )
