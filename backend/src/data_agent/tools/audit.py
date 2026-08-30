from __future__ import annotations

import logging
from collections.abc import Iterable

import pandas as pd

from data_agent.tools.delivery_view import REPORTED_PROBLEM_COLUMN
from data_agent.tools.issue_taxonomy import (
    DIRTY_ISSUE_PREFIX,
    classify_label_source,
    classify_reason_code,
    dirty_label_parts,
    hit_rule_name,
    severity_for_label,
    taxonomy_category,
)
from data_agent.tools.issue_wording import (
    display_issue_name,
    display_severity,
    label_description,
    suggested_annotation_action,
)
from data_agent.utils.field_names import looks_like_key

logger = logging.getLogger(__name__)

DEFAULT_TRACE_FIELDS = [
    "_source_table",
    "_source_row_index",
    "record_id",
    "record_key",
]


def add_source_trace(
    df: pd.DataFrame,
    source_table: str = "main",
    trace_field: str = "_source_row_index",
) -> pd.DataFrame:
    result = df.copy()
    if "_source_table" not in result.columns:
        result.insert(0, "_source_table", source_table)
    if trace_field not in result.columns:
        result.insert(1, trace_field, result.index)
    return result


def _label_field(label: str, issue_fields: dict[str, str]) -> str:
    """Which column a finding is about, for rule labels and dirty labels alike."""

    if label.startswith(DIRTY_ISSUE_PREFIX):
        return dirty_label_parts(label)[0]
    return issue_fields.get(label, "")


def _drop_duplicate_dirty_labels(
    labels: list[str],
    issue_fields: dict[str, str],
) -> list[str]:
    """Say each problem once.

    An explicit rule and a built-in heuristic routinely describe the same cell — the
    user saw "amount 小于允许的最小值；金额为负" for a single negative amount. When a
    rule already covers the field, its wording (which came from the user's own
    requirement) is the one kept; the heuristic's duplicate label is dropped. The
    heuristic still counted toward severity before this point, so nothing becomes
    deliverable that was not.
    """

    covered = {
        issue_fields.get(label, "")
        for label in labels
        if not label.startswith(DIRTY_ISSUE_PREFIX)
    }
    covered.discard("")
    if not covered:
        return labels
    return [
        label
        for label in labels
        if not label.startswith(DIRTY_ISSUE_PREFIX)
        or dirty_label_parts(label)[0] not in covered
    ]


def build_record_audit(
    abnormal_df: pd.DataFrame,
    abnormal_field: str = "abnormal_type",
    trace_fields: Iterable[str] = DEFAULT_TRACE_FIELDS,
    lookup_match_fields: dict[str, str] | None = None,
    rule_fields: dict[str, str] | None = None,
) -> pd.DataFrame:
    columns = [
        "source_table",
        "source_row_index",
        "label",
        "label_source",
        "reason_code",
        "hit_rule",
        "hit_lookup_rules",
        "failed_lookup_rules",
        "abnormal_type",
        "affected_field",
        "current_value",
        *[
            field
            for field in trace_fields
            if field not in {"_source_row_index", "_source_table"}
        ],
    ]
    if abnormal_df.empty or abnormal_field not in abnormal_df.columns:
        return pd.DataFrame(columns=columns)

    rows = []
    available_trace_fields = [field for field in trace_fields if field in abnormal_df.columns]
    lookup_fields = lookup_match_fields or infer_lookup_match_fields(abnormal_df)
    issue_fields = rule_fields or {}
    for row_index, row in abnormal_df.iterrows():
        labels = _drop_duplicate_dirty_labels(
            _split_labels(row.get(abnormal_field, "")),
            issue_fields,
        )
        hit_lookup_rules, failed_lookup_rules = lookup_rule_status(row, lookup_fields)
        for label in labels:
            reason_code = classify_reason_code(label)
            label_source = classify_label_source(label)
            hit_rule = hit_rule_name(label, label_source)
            affected_field = _label_field(label, issue_fields)
            audit_row = {
                "source_table": row.get("_source_table", ""),
                "source_row_index": row.get("_source_row_index", row_index),
                "label": label,
                "label_source": label_source,
                "reason_code": reason_code,
                "hit_rule": hit_rule,
                "hit_lookup_rules": ";".join(hit_lookup_rules),
                "failed_lookup_rules": ";".join(failed_lookup_rules),
                "abnormal_type": row.get(abnormal_field, ""),
                "affected_field": affected_field,
                "current_value": row.get(affected_field, "") if affected_field else "",
            }
            for field in available_trace_fields:
                if field not in {"_source_row_index", "_source_table"}:
                    audit_row[field] = row.get(field)
            rows.append(audit_row)
            if label_source == "lookup_rule":
                logger.info(
                    "匹配失败审计：label=%s reason_code=%s source_table=%s "
                    "source_row=%s hit_lookup_rules=%s failed_lookup_rules=%s keys=%s",
                    label,
                    reason_code,
                    audit_row["source_table"],
                    audit_row["source_row_index"],
                    audit_row["hit_lookup_rules"],
                    audit_row["failed_lookup_rules"],
                    _row_keys(row),
                )

    return pd.DataFrame(rows, columns=columns)


def build_label_summary(record_audit: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "label",
        "label_source",
        "reason_code",
        "row_count",
        "sample_source_rows",
        "sample_keys",
    ]
    if record_audit.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for (label, label_source, reason_code), group in record_audit.groupby(
        ["label", "label_source", "reason_code"]
    ):
        rows.append(
            {
                "label": label,
                "label_source": label_source,
                "reason_code": reason_code,
                "row_count": int(len(group)),
                "sample_source_rows": ", ".join(
                    group["source_row_index"].astype(str).head(10).tolist()
                ),
                "sample_keys": _sample_keys(group),
            }
        )

    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["row_count", "label"],
        ascending=[False, True],
        ignore_index=True,
    )


def build_annotation_tasks(record_audit: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "annotation_id",
        "source_table",
        "source_row_index",
        "label",
        "taxonomy_category",
        "severity",
        "reason_code",
        "manual_status",
        "reviewer",
        "review_note",
        "suggested_action",
        "feedback_to_rule",
        "feedback_rule_target",
        "hit_rule",
        "hit_lookup_rules",
        "failed_lookup_rules",
        "record_id",
        "record_key",
        "affected_field",
        "current_value",
    ]
    if record_audit.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for index, row in record_audit.reset_index(drop=True).iterrows():
        rows.append(
            {
                "annotation_id": f"ANN-{index + 1:06d}",
                "source_table": row.get("source_table", ""),
                "source_row_index": row.get("source_row_index", ""),
                "label": row.get("label", ""),
                "taxonomy_category": taxonomy_category(row.get("label", "")),
                "severity": severity_for_label(row.get("label", "")),
                "reason_code": row.get("reason_code", ""),
                "manual_status": "pending",
                "reviewer": "",
                "review_note": "",
                "suggested_action": suggested_annotation_action(row.get("label", "")),
                "feedback_to_rule": "",
                "feedback_rule_target": row.get("hit_rule", ""),
                "hit_rule": row.get("hit_rule", ""),
                "hit_lookup_rules": row.get("hit_lookup_rules", ""),
                "failed_lookup_rules": row.get("failed_lookup_rules", ""),
                "record_id": row.get("record_id", ""),
                "record_key": row.get("record_key", ""),
                "affected_field": row.get("affected_field", ""),
                "current_value": row.get("current_value", ""),
            }
        )

    return pd.DataFrame(rows, columns=columns)


def build_annotation_summary(annotation_tasks: pd.DataFrame) -> pd.DataFrame:
    columns = ["label", "taxonomy_category", "severity", "manual_status", "task_count"]
    if annotation_tasks.empty:
        return pd.DataFrame(columns=columns)

    return (
        annotation_tasks.groupby(
            ["label", "taxonomy_category", "severity", "manual_status"],
            as_index=False,
        )
        .size()
        .rename(columns={"size": "task_count"})
        .sort_values(by=["task_count", "label"], ascending=[False, True], ignore_index=True)
    )


def build_annotation_taxonomy(record_audit: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "label",
        "taxonomy_category",
        "severity",
        "reason_code",
        "description",
        "default_manual_status",
        "allowed_manual_status",
        "feedback_rule_target",
    ]
    if record_audit.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    seen = set()
    for row in record_audit.to_dict(orient="records"):
        label = row["label"]
        if label in seen:
            continue
        seen.add(label)
        rows.append(
            {
                "label": label,
                "taxonomy_category": taxonomy_category(label),
                "severity": severity_for_label(label),
                "reason_code": row["reason_code"],
                "description": label_description(label),
                "default_manual_status": "pending",
                "allowed_manual_status": "pending/accepted/rejected",
                "feedback_rule_target": row.get("hit_rule", ""),
            }
        )

    return pd.DataFrame(rows, columns=columns)


def build_issue_summary(annotation_tasks: pd.DataFrame) -> pd.DataFrame:
    columns = ["issue_type", "severity", "affected_rows", "suggested_action"]
    if annotation_tasks.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for (label, severity, suggested_action), group in annotation_tasks.groupby(
        ["label", "severity", "suggested_action"]
    ):
        rows.append(
            {
                "issue_type": label,
                "severity": severity,
                "affected_rows": int(len(group)),
                "suggested_action": suggested_action,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["affected_rows", "issue_type"],
        ascending=[False, True],
        ignore_index=True,
    )


def build_review_tasks(annotation_tasks: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "source_row_index",
        "issue_type",
        "severity",
        "status",
        "suggested_action",
        "review_note",
        "record_id",
        "affected_field",
        "current_value",
    ]
    if annotation_tasks.empty:
        return pd.DataFrame(columns=columns)

    result = pd.DataFrame(
        {
            "source_row_index": annotation_tasks.get("source_row_index"),
            "issue_type": annotation_tasks.get("label"),
            "severity": annotation_tasks.get("severity"),
            "status": annotation_tasks.get("manual_status"),
            "suggested_action": annotation_tasks.get("suggested_action"),
            "review_note": annotation_tasks.get("review_note"),
            "record_id": annotation_tasks.get("record_id"),
            "affected_field": annotation_tasks.get("affected_field"),
            "current_value": annotation_tasks.get("current_value"),
        }
    )
    return result[columns]


def build_business_summary(
    summary: pd.DataFrame,
    issue_summary: pd.DataFrame,
) -> pd.DataFrame:
    columns = ["类型", "项目", "数量", "严重级别", "建议处理"]
    rows = []
    metric_names = {
        "total": "总行数",
        "output": "输出行数",
        "filtered_out": "按目标排除行数",
        "valid": "通过行数",
        "abnormal": "需处理行数",
    }
    for row in summary.to_dict(orient="records"):
        rows.append(
            {
                "类型": "总体",
                "项目": metric_names.get(str(row["metric"]), str(row["metric"])),
                "数量": int(row["value"]),
                "严重级别": "",
                "建议处理": "",
            }
        )

    for row in issue_summary.to_dict(orient="records"):
        rows.append(
            {
                "类型": "问题类型",
                "项目": display_issue_name(row["issue_type"]),
                "数量": int(row["affected_rows"]),
                "严重级别": display_severity(row["severity"]),
                "建议处理": row["suggested_action"],
            }
        )

    return pd.DataFrame(rows, columns=columns)


def build_business_result(
    cleaned_df: pd.DataFrame,
    abnormal_df: pd.DataFrame,
    review_tasks: pd.DataFrame,
    abnormal_field: str = "abnormal_type",
) -> pd.DataFrame:
    cleaned = simplify_result_data(cleaned_df)
    abnormal = simplify_result_data(abnormal_df)
    task_context = _review_context_by_row(review_tasks)

    rows = []
    for _, row in cleaned.iterrows():
        # A delivered row may still carry advisory (warn) reasons in abnormal_field
        # (e.g. a soft document-rule violation). Keep it delivered ("通过") but tag
        # the issue so precise positioning is preserved rather than silently dropped.
        source_row_index = row.get("source_row_index", row.get("_source_row_index", ""))
        review_context = task_context.get(source_row_index)
        context = review_context or _advisory_context(row.get(abnormal_field, ""))
        rows.append(
            _business_row(
                row,
                status="通过",
                context=context,
                abnormal_field=abnormal_field,
                reported_problem=review_context is not None,
            )
        )
    for _, row in abnormal.iterrows():
        source_row_index = row.get("source_row_index", row.get("_source_row_index", ""))
        context = task_context.get(source_row_index, {})
        rows.append(
            _business_row(
                row,
                status="需处理",
                context=context,
                abnormal_field=abnormal_field,
                reported_problem=True,
            )
        )

    if not rows:
        return pd.DataFrame(columns=_business_column_order())

    result = pd.DataFrame(rows).sort_values(by="源行号", ignore_index=True)
    ordered = [column for column in _business_column_order() if column in result.columns]
    ordered.extend(column for column in result.columns if column not in ordered)
    return result[ordered]


def simplify_result_data(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    result = df.copy()
    if "_source_row_index" in result.columns and "source_row_index" not in result.columns:
        result = result.rename(columns={"_source_row_index": "source_row_index"})
    internal_columns = [
        column
        for column in result.columns
        if column.startswith("_lookup_") or column in {"_source_table", "_lookup_matched"}
    ]
    return result.drop(columns=internal_columns, errors="ignore")


def build_field_change_audit(
    source_df: pd.DataFrame,
    result_df: pd.DataFrame,
    lookup_field_sources: dict[str, str] | None = None,
    formula_outputs: dict[str, str] | None = None,
) -> pd.DataFrame:
    columns = [
        "source_table",
        "source_row_index",
        "field",
        "before_value",
        "after_value",
        "change_type",
        "producer",
        "reason_code",
    ]
    if result_df.empty:
        return pd.DataFrame(columns=columns)

    lookup_field_sources = lookup_field_sources or {}
    formula_outputs = formula_outputs or {}

    # One column at a time instead of one cell at a time. The previous row × column
    # Python loop did a `.loc` lookup per row, so a 100k-row × 20-column result meant
    # two million interpreter iterations; this compares whole aligned Series.
    source_aligned = (
        source_df.set_index("_source_row_index", drop=False)
        .reindex(result_df["_source_row_index"])
        .reset_index(drop=True)
    )
    result_reset = result_df.reset_index(drop=True)
    source_table = (
        result_reset["_source_table"]
        if "_source_table" in result_reset.columns
        else pd.Series("", index=result_reset.index)
    )

    frames: list[pd.DataFrame] = []
    for field in result_reset.columns:
        field_name = str(field)
        if field_name.startswith("_lookup_") or field_name == "abnormal_type":
            continue
        after = result_reset[field]
        if field_name in {"_source_table", "_source_row_index"}:
            change_type = "trace_added"
            before = (
                source_aligned[field_name]
                if field_name in source_aligned.columns
                else pd.Series("", index=result_reset.index)
            )
            changed = pd.Series(True, index=result_reset.index)
        elif field_name not in source_aligned.columns:
            change_type = "field_added"
            before = pd.Series("", index=result_reset.index)
            changed = pd.Series(True, index=result_reset.index)
        else:
            change_type = "value_changed"
            before = source_aligned[field_name]
            changed = _normalized_series(before).ne(_normalized_series(after))
        if not changed.any():
            continue
        frames.append(
            pd.DataFrame(
                {
                    "source_table": source_table[changed],
                    "source_row_index": result_reset["_source_row_index"][changed],
                    "field": field_name,
                    "before_value": before[changed],
                    "after_value": after[changed],
                    "change_type": change_type,
                    "producer": field_producer(
                        field_name, lookup_field_sources, formula_outputs
                    ),
                    "reason_code": f"FIELD_{change_type.upper()}",
                }
            )
        )

    if not frames:
        return pd.DataFrame(columns=columns)
    # Preserve the original row-major ordering (row, then column).
    audit = pd.concat(frames, ignore_index=False)
    field_order = {str(field): index for index, field in enumerate(result_reset.columns)}
    audit = audit.assign(_field_order=audit["field"].map(field_order))
    audit = audit.sort_values(
        by=["source_row_index", "_field_order"], kind="stable"
    ).drop(columns="_field_order")
    return audit.reset_index(drop=True)[columns]


def _normalized_series(series: pd.Series) -> pd.Series:
    """Render a column the way :func:`_normalize_value` renders a single cell."""

    return series.where(series.notna(), "").astype(str)


def infer_lookup_match_fields(df: pd.DataFrame) -> dict[str, str]:
    result = {}
    for column in df.columns:
        if column.startswith("_lookup_matched_"):
            result[column] = column.removeprefix("_lookup_matched_")
        elif column == "_lookup_matched":
            result[column] = "lookup"
    return result


def lookup_rule_status(
    row: pd.Series,
    lookup_match_fields: dict[str, str],
) -> tuple[list[str], list[str]]:
    hit_rules = []
    failed_rules = []
    for field, rule_name in lookup_match_fields.items():
        matched = bool(row.get(field, False))
        if matched:
            hit_rules.append(rule_name)
        else:
            failed_rules.append(rule_name)
    return hit_rules, failed_rules


def field_producer(
    field: str,
    lookup_field_sources: dict[str, str],
    formula_outputs: dict[str, str],
) -> str:
    if field in lookup_field_sources:
        return lookup_field_sources[field]
    if field in formula_outputs:
        return formula_outputs[field]
    if field in {"_source_table", "_source_row_index"}:
        return "source_trace"
    return "source_or_mapping"


def _split_labels(value: object) -> list[str]:
    return [label.strip() for label in str(value).split(";") if label.strip()]


def _review_context_by_row(review_tasks: pd.DataFrame) -> dict[object, dict[str, str]]:
    if review_tasks.empty:
        return {}
    result = {}
    for source_row_index, group in review_tasks.groupby("source_row_index", dropna=False):
        result[source_row_index] = {
            "问题类型": "；".join(
                display_issue_name(label) for label in group["issue_type"].dropna().tolist()
            ),
            "建议处理": "；".join(_unique_text(group["suggested_action"].dropna().tolist())),
            "影响字段": "；".join(
                _unique_text(group["affected_field"].dropna().tolist())
            ),
            "当前值": "；".join(
                _unique_text(group["current_value"].dropna().tolist())
            ),
        }
    return result


def _advisory_context(reason_text: object) -> dict[str, str]:
    """Build a business-facing tag for a delivered row carrying advisory reasons.

    Delivered rows keep status ``通过`` but must still name any soft (warn) issue so
    the value is positioned precisely rather than shipped silently. Returns an empty
    context when the row has no reason, so genuinely clean rows stay unmarked.
    """

    labels = _split_labels(reason_text)
    if not labels:
        return {}
    return {"问题类型": "；".join(display_issue_name(label) for label in labels)}


def _business_row(
    row: pd.Series,
    status: str,
    context: dict[str, str],
    abnormal_field: str,
    reported_problem: bool,
) -> dict[str, object]:
    renamed = {}
    for field, value in row.to_dict().items():
        if field == abnormal_field:
            continue
        renamed[_business_column_name(field)] = value

    result = {
        REPORTED_PROBLEM_COLUMN: reported_problem,
        "源行号": row.get("source_row_index", row.get("_source_row_index", "")),
        "处理状态": status,
        "问题类型": context.get("问题类型", ""),
        "建议处理": context.get("建议处理", ""),
        "影响字段": context.get("影响字段", ""),
        "当前值": context.get("当前值", ""),
    }
    result.update(renamed)
    return result


def _business_column_name(field: str) -> str:
    if field == "source_row_index":
        return "源行号"
    return str(field)


def _business_column_order() -> list[str]:
    return [
        "处理状态",
        "问题类型",
        "建议处理",
        "影响字段",
        "当前值",
        "源行号",
    ]


def _unique_text(values: list[object]) -> list[str]:
    result = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


def _field_change_type(
    field: str,
    before_exists: bool,
    before_value: object,
    after_value: object,
) -> str:
    if field in {"_source_table", "_source_row_index"}:
        return "trace_added"
    if not before_exists:
        return "field_added"
    if _normalize_value(before_value) != _normalize_value(after_value):
        return "value_changed"
    return "unchanged"


def _normalize_value(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value)


def _row_keys(row: pd.Series) -> str:
    key_fields = _key_like_columns(row.index)
    return "|".join(f"{field}={row.get(field)}" for field in key_fields)


def _sample_keys(group: pd.DataFrame) -> str:
    key_fields = _key_like_columns(group.columns)
    if not key_fields:
        return ""

    values = []
    for row in group[key_fields].head(10).to_dict(orient="records"):
        values.append("|".join(f"{key}={value}" for key, value in row.items()))
    return "; ".join(values)


def _key_like_columns(columns) -> list[str]:
    result = []
    for column in columns:
        if looks_like_key(column):
            result.append(str(column))
        if len(result) >= 4:
            break
    return result
