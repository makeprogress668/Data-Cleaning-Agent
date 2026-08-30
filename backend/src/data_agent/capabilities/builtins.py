"""Capability metadata mapped to the existing deterministic implementations."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from data_agent.business_report import write_business_report
from data_agent.capabilities.base import (
    CapabilitySpec,
    FailurePolicy,
    FieldEffect,
    RiskLevel,
)
from data_agent.capabilities.registry import CapabilityRegistry
from data_agent.tools import (
    analyze_business_data,
    analyze_dirty_data,
    apply_anomaly_rules,
    apply_field_mapping,
    apply_formulas,
    build_field_change_audit,
    build_pivot,
    compute_quality_score,
    export_workbook,
    extract_document_insights,
    generate_business_charts,
    profile_dataset,
    read_tables_from_paths,
    vlookup,
)
from data_agent.tools.excel_formula import formula_column
from data_agent.tools.rollup import rollup_to_master
from data_agent.tools.table_union import union_tables
from data_agent.utils.reshape import melt_wide_to_long
from data_agent.utils.target_schema import project_onto_schema

DEFAULT_CAPABILITY_MAX_ROWS = 5_000_000


def _capability(
    capability_id: str,
    executor: Callable[..., Any],
    *,
    parameters: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    input_schema: dict[str, str] | None = None,
    output_schema: dict[str, str] | None = None,
    engines: tuple[str, ...] = ("pandas",),
    risk: RiskLevel = "low",
    confirm: bool = False,
    effects: tuple[FieldEffect, ...] = ("none",),
    formula_operators: tuple[str, ...] = (),
    formula_fallback: bool = False,
    acceptance: tuple[str, ...] = ("operation completes without an exception",),
    failure_policy: FailurePolicy = "fail",
    max_rows: int | None = DEFAULT_CAPABILITY_MAX_ROWS,
) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        version="1.0.0",
        input_schema=input_schema or {"data": "DataFrame"},
        output_schema=output_schema or {"data": "DataFrame"},
        parameter_schema=parameters or {},
        required_parameters=required,
        supported_engines=engines,
        risk_level=risk,
        requires_confirmation=confirm,
        field_effects=effects,
        executor=executor,
        formula_operators=formula_operators,
        formula_fallback=formula_fallback,
        acceptance_rules=acceptance,
        failure_policy=failure_policy,
        max_rows=max_rows,
    )


def builtin_capabilities() -> tuple[CapabilitySpec, ...]:
    """Return the complete V2 capability allowlist."""

    formula_parameters = {
        "output": "string",
        "op": "FormulaOperator",
        "required": "boolean",
        "authorization": "string",
        "source": "optional string",
        "columns": "list[string]",
        "value": "any",
        "values": "list[any]",
        "mapping": "dict[string,any]",
        "default": "any",
        "sep": "string",
        "condition": "optional ConditionConfig",
        "conditions": "list[ConditionConfig]",
        "true_value": "any",
        "false_value": "any",
    }
    return (
        _capability(
            "read_tables",
            read_tables_from_paths,
            parameters={"input_paths": "list[path]", "recursive": "boolean"},
            required=("input_paths",),
            input_schema={"input_paths": "list[path]"},
            output_schema={"tables": "dict[str,DataFrame]", "inventory": "DataFrame"},
            engines=("pandas", "duckdb"),
            acceptance=("at least one supported table is loaded",),
        ),
        _capability(
            "extract_document_rules",
            extract_document_insights,
            parameters={"input_paths": "list[path]", "recursive": "boolean"},
            required=("input_paths",),
            input_schema={"input_paths": "list[path]"},
            output_schema={"rules": "list[AnomalyRuleConfig]"},
            acceptance=("unsupported documents are reported without inventing rules",),
        ),
        _capability(
            "profile_dataset",
            profile_dataset,
            input_schema={"tables": "dict[str,DataFrame]"},
            output_schema={"profiles": "dict[str,DataFrame]"},
            engines=("pandas", "duckdb"),
            acceptance=("every input table has a profile row",),
        ),
        _capability(
            "detect_relationships",
            profile_dataset,
            input_schema={"tables": "dict[str,DataFrame]"},
            output_schema={"relationships": "DataFrame"},
            engines=("pandas", "duckdb"),
            acceptance=("relationship evidence references existing tables and fields",),
        ),
        _capability(
            "map_fields",
            apply_field_mapping,
            parameters={"rename": "dict[string,string]"},
            required=("rename",),
            effects=("overwrite",),
            acceptance=("renamed fields are unique",),
        ),
        _capability(
            "trim",
            apply_formulas,
            parameters=formula_parameters,
            required=("output", "op"),
            effects=("overwrite",),
            formula_operators=("trim",),
            acceptance=("row count is unchanged",),
        ),
        _capability(
            "normalize_case",
            apply_formulas,
            parameters=formula_parameters,
            required=("output", "op"),
            effects=("overwrite",),
            formula_operators=("upper", "lower"),
            acceptance=("row count is unchanged",),
        ),
        _capability(
            "normalize_width",
            apply_formulas,
            parameters=formula_parameters,
            required=("output", "op"),
            effects=("overwrite",),
            formula_operators=("normalize_width",),
            acceptance=("row count is unchanged",),
        ),
        _capability(
            "map_values",
            apply_field_mapping,
            parameters={"value_maps": "dict[string,dict]"},
            required=("value_maps",),
            effects=("overwrite",),
            acceptance=("unmapped values follow the declared fallback",),
        ),
        _capability(
            "filter_rows",
            apply_formulas,
            parameters=formula_parameters,
            required=("op",),
            risk="high",
            confirm=True,
            effects=("delete",),
            formula_operators=("filter_rows",),
            acceptance=("retained row count and filter condition are recorded",),
        ),
        _capability(
            "deduplicate",
            apply_formulas,
            parameters=formula_parameters,
            required=("op",),
            risk="high",
            confirm=True,
            effects=("delete",),
            formula_operators=("dedupe",),
            acceptance=("deduplication keys and removed row count are recorded",),
        ),
        _capability(
            "fill_missing",
            apply_formulas,
            parameters=formula_parameters,
            required=("output", "op"),
            risk="medium",
            effects=("overwrite",),
            formula_operators=("coalesce", "fill_down"),
            acceptance=("only missing target values are filled",),
        ),
        _capability(
            "join_tables",
            vlookup,
            parameters={
                "left_keys": "list[string]",
                "right_keys": "list[string]",
                "fields": "list[string]",
                "match_mode": "LookupMatchMode",
                "duplicate_strategy": "optional string",
                "source_table": "optional string",
            },
            required=("left_keys", "right_keys"),
            risk="medium",
            effects=("add",),
            acceptance=("join expansion and unmatched rows are measured",),
        ),
        _capability(
            "lookup_fields",
            vlookup,
            parameters={
                "left_keys": "list[string]",
                "right_keys": "list[string]",
                "fields": "list[string]",
                "match_mode": "LookupMatchMode",
                "duplicate_strategy": "optional string",
                "source_table": "optional string",
            },
            required=("left_keys", "right_keys", "fields"),
            engines=("pandas", "duckdb"),
            risk="medium",
            effects=("add",),
            acceptance=("matched fields originate from the declared lookup table",),
        ),
        _capability(
            "derive_column",
            apply_formulas,
            parameters=formula_parameters,
            required=("output", "op"),
            effects=("add",),
            formula_fallback=True,
            acceptance=("the output field has a declared formula and source fields",),
        ),
        _capability(
            "project_schema",
            project_onto_schema,
            parameters={"schema": "list[string]"},
            required=("schema",),
            effects=("add", "delete"),
            acceptance=("delivered fields and order exactly match the declared schema",),
        ),
        _capability(
            "write_formulas",
            formula_column,
            parameters={"fills": "list[FormulaFill]"},
            required=("fills",),
            effects=("overwrite",),
            acceptance=("only declared generated columns contain executable formulas",),
        ),
        _capability(
            "aggregate",
            build_pivot,
            parameters={"group_by": "list[string]", "metrics": "list[PivotMetric]"},
            required=("group_by", "metrics"),
            output_schema={"result": "DataFrame"},
            engines=("pandas", "duckdb"),
            acceptance=("all metrics use registered aggregation functions",),
        ),
        _capability(
            # Excel's unpivot: twelve month columns become a 月份 column. Row count
            # multiplies by design, which is exactly what the operation is — declaring
            # it here is what lets the row-conservation check know that is allowed.
            "reshape",
            melt_wide_to_long,
            parameters={
                "value_columns": "list[string]",
                "variable_name": "string",
                "value_name": "string",
            },
            required=("value_columns", "variable_name", "value_name"),
            effects=("add",),
            acceptance=("no value is dropped: every wide cell becomes one long row",),
        ),
        _capability(
            # Excel's cross-sheet SUMIF: the total of every matching detail row, not
            # one matching value. Medium risk like a lookup — it adds a column and can
            # be wrong about which rows matched — but it never changes the row count.
            "rollup",
            rollup_to_master,
            parameters={
                "source_table": "string",
                "left_key": "string",
                "right_key": "string",
                "measure": "string",
                "agg": "PivotAggregation",
                "output": "string",
            },
            required=("source_table", "left_key", "right_key", "measure", "output"),
            risk="medium",
            effects=("add",),
            acceptance=("master row count is unchanged and every key is accounted for",),
        ),
        _capability(
            # Stacking several same-shaped uploads into one table. It only ever adds
            # rows, so the risk is low — but it belongs in the allowlist like every
            # other thing the executor does, and without a step here the recorded
            # stage had no plan step to attach to and its metrics were dropped.
            "union_tables",
            union_tables,
            parameters={"tables": "list[string]"},
            required=("tables",),
            input_schema={"tables": "dict[str,DataFrame]"},
            output_schema={"data": "DataFrame"},
            effects=("add",),
            acceptance=("every source row appears once in the combined table",),
        ),
        _capability(
            "pivot",
            build_pivot,
            parameters={"group_by": "list[string]", "metrics": "list[PivotMetric]"},
            required=("group_by", "metrics"),
            output_schema={"result": "DataFrame"},
            engines=("pandas", "duckdb"),
            acceptance=("all group and metric fields exist",),
        ),
        _capability(
            "validate_rules",
            apply_anomaly_rules,
            parameters={"rules": "list[AnomalyRuleConfig]"},
            required=("rules",),
            output_schema={"data": "DataFrame", "violations": "DataFrame"},
            effects=("add",),
            acceptance=("every violation references a declared rule",),
        ),
        _capability(
            "detect_anomalies",
            analyze_dirty_data,
            parameters={"auto_fix_safe_issues": "boolean"},
            output_schema={"issues": "DataFrame"},
            risk="medium",
            effects=("add", "overwrite"),
            acceptance=("automatic fixes are limited to safe issue classes",),
        ),
        _capability(
            "summarize",
            analyze_business_data,
            parameters={"goal": "string"},
            output_schema={"summary": "DataFrame"},
            acceptance=("summary metrics are derived from execution results",),
        ),
        _capability(
            "top_n",
            analyze_business_data,
            parameters={"dimension": "string", "metric": "string", "limit": "integer"},
            required=("dimension", "metric"),
            output_schema={"result": "DataFrame"},
            acceptance=("ranking dimension and metric are declared",),
        ),
        _capability(
            "trend_analysis",
            analyze_business_data,
            parameters={"time_field": "string", "metric": "string"},
            required=("time_field", "metric"),
            output_schema={"result": "DataFrame"},
            acceptance=("the time field is parseable and the metric is declared",),
        ),
        _capability(
            "generate_chart",
            generate_business_charts,
            parameters={"charts": "list[ChartOutputSpec]"},
            required=("charts",),
            output_schema={"charts": "list[file]"},
            acceptance=("one output file is produced for each declared chart",),
        ),
        _capability(
            "score_quality",
            compute_quality_score,
            parameters={"critical_fields": "list[string]"},
            output_schema={"score": "integer", "deductions": "DataFrame"},
            acceptance=("the score and every deduction are deterministic",),
        ),
        _capability(
            "build_audit",
            build_field_change_audit,
            parameters={"include_audit": "boolean"},
            output_schema={"audit": "files"},
            acceptance=("audit fields trace to source or registered operations",),
        ),
        _capability(
            "export_table",
            export_workbook,
            parameters={"output_file": "path", "sheets": "list[string]"},
            required=("output_file", "sheets"),
            input_schema={"sheets": "dict[str,DataFrame]"},
            output_schema={"artifact": "xlsx"},
            acceptance=("the workbook passes OutputSpec linting before export",),
        ),
        _capability(
            "export_report",
            write_business_report,
            parameters={"output_dir": "path", "formats": "list[string]"},
            required=("output_dir", "formats"),
            input_schema={"answer": "BusinessAnswer"},
            output_schema={"artifacts": "dict[str,path]"},
            acceptance=("report formats and files are declared by OutputSpec",),
        ),
    )


@lru_cache(maxsize=1)
def get_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_many(builtin_capabilities())
    return registry
