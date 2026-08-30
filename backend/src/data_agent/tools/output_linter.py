"""Deterministic enforcement of the OutputSpec delivery boundary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from data_agent.schemas.output import OutputSpec


class OutputContractError(ValueError):
    """Raised before delivery when generated output exceeds its contract."""


def lint_output(
    output_spec: OutputSpec | dict,
    *,
    sheets: Mapping[str, Iterable[str]] | None = None,
    chart_ids: Iterable[str] | None = None,
    artifact_names: Iterable[str] | None = None,
    allowed_fields: Iterable[str] | None = None,
    row_counts: Mapping[str, int] | None = None,
    primary_artifact_name: str = "final_result.xlsx",
) -> None:
    """Reject undeclared, duplicate, missing, or untraceable output content."""

    spec = (
        output_spec
        if isinstance(output_spec, OutputSpec)
        else OutputSpec.model_validate(output_spec)
    )
    errors: list[str] = []
    if sheets is not None:
        sheet_names = list(sheets)
        _duplicates(sheet_names, "sheet", errors)
        undeclared = set(sheet_names) - spec.allowed_sheet_names
        missing = spec.required_sheet_names - set(sheet_names)
        if undeclared:
            errors.append(f"undeclared sheets: {sorted(undeclared)}")
        if missing:
            errors.append(f"missing declared sheets: {sorted(missing)}")
        _lint_fields(spec, sheets, allowed_fields, errors)
        _lint_rows(spec, row_counts or {}, errors)

    if chart_ids is not None:
        actual_chart_ids = list(chart_ids)
        declared_chart_ids = [chart.chart_id for chart in spec.charts]
        _duplicates(actual_chart_ids, "chart", errors)
        undeclared = set(actual_chart_ids) - set(declared_chart_ids)
        missing = set(declared_chart_ids) - set(actual_chart_ids)
        if undeclared:
            errors.append(f"undeclared charts: {sorted(undeclared)}")
        if missing:
            errors.append(f"missing declared charts: {sorted(missing)}")

    if artifact_names is not None:
        actual_artifacts = list(artifact_names)
        expected_artifacts = {primary_artifact_name}
        if spec.report.enabled:
            expected_artifacts.update(
                f"business_answer.{_report_extension(item)}"
                for item in spec.report.formats
            )
        _duplicates(actual_artifacts, "artifact", errors)
        undeclared = set(actual_artifacts) - expected_artifacts
        missing = expected_artifacts - set(actual_artifacts)
        if undeclared:
            errors.append(f"undeclared artifacts: {sorted(undeclared)}")
        if missing:
            errors.append(f"missing declared artifacts: {sorted(missing)}")

    if errors:
        raise OutputContractError("OutputSpec validation failed: " + "; ".join(errors))


def _lint_fields(
    spec: OutputSpec,
    sheets: Mapping[str, Iterable[str]],
    allowed_fields: Iterable[str] | None,
    errors: list[str],
) -> None:
    source_fields = set(allowed_fields or [])
    artifacts = {
        spec.primary_artifact.name: spec.primary_artifact,
        **{item.name: item for item in spec.secondary_artifacts},
    }
    for sheet_name, columns in sheets.items():
        artifact = artifacts.get(sheet_name)
        if artifact is None:
            continue
        actual_fields = [str(column) for column in columns]
        _duplicates(actual_fields, f"field in {sheet_name}", errors)
        declared_fields = list(artifact.fields)
        permitted = (
            set(declared_fields) | source_fields
            if artifact.field_policy == "source_dynamic"
            else set(declared_fields) or source_fields
        )
        if artifact.field_policy == "exact" and declared_fields:
            if actual_fields != declared_fields:
                errors.append(
                    f"{sheet_name} fields differ from declared order: "
                    f"expected {declared_fields}, got {actual_fields}"
                )
                continue
        elif artifact.field_policy == "prefix" and declared_fields:
            if actual_fields[: len(declared_fields)] != declared_fields:
                errors.append(
                    f"{sheet_name} fields do not match declared prefix: "
                    f"expected {declared_fields}, got {actual_fields}"
                )
                continue
        elif artifact.field_policy == "declared_subset" and declared_fields:
            actual_declared = [field for field in declared_fields if field in actual_fields]
            if actual_fields != actual_declared:
                errors.append(
                    f"{sheet_name} fields exceed the declared conditional set: "
                    f"expected subset of {declared_fields}, got {actual_fields}"
                )
                continue
        if artifact.field_policy == "prefix":
            continue
        if permitted:
            unknown = set(actual_fields) - permitted
            if unknown:
                errors.append(
                    f"{sheet_name} contains fields without a declared source: "
                    f"{sorted(unknown)}"
                )


def _lint_rows(
    spec: OutputSpec,
    row_counts: Mapping[str, int],
    errors: list[str],
) -> None:
    artifacts = {
        spec.primary_artifact.name: spec.primary_artifact,
        **{item.name: item for item in spec.secondary_artifacts},
    }
    for name, count in row_counts.items():
        artifact = artifacts.get(name)
        if artifact is None or artifact.exact_rows is None:
            continue
        if count != artifact.exact_rows:
            errors.append(
                f"{name} row count differs from contract: "
                f"expected {artifact.exact_rows}, got {count}"
            )


def _duplicates(values: list[str], label: str, errors: list[str]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        errors.append(f"duplicate {label}s: {duplicates}")


def _report_extension(report_format: str) -> str:
    return "md" if report_format == "markdown" else report_format
