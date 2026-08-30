"""One definition of what a delivered sheet looks like.

The CLI and the API used to assemble the workbook independently: one compacted
technical columns and could swap the whole result for a system-import view, the other
shipped the raw projection. Same input, same goal, two different files. Every entry
point now projects through this module, so "处理结果" means one thing.
"""

from __future__ import annotations

import pandas as pd

# Columns that describe a record's review state rather than its business content.
REVIEW_COLUMNS: tuple[str, ...] = (
    "处理状态",
    "问题类型",
    "建议处理",
    "源行号",
    "影响字段",
    "当前值",
)
DELIVERED_STATUS = "通过"
STATUS_COLUMN = "处理状态"
REPORTED_PROBLEM_COLUMN = "_reported_problem"
IMPORT_VIEW_SHEET = "可导入数据"

# Internal bookkeeping columns that never belong in a business deliverable.
_INTERNAL_COLUMN_NAMES = {
    "record_key",
    "record_id",
    "source_table",
    "source_row_index",
}
# Review-workflow annotations that are process state, not business content.
_PROCESS_COLUMNS = {
    "严重级别",
    "确认状态",
    "备注",
    "是否可用于最终结果",
    "是否需要人工确认",
    "来源",
}


def is_technical_column(column: str) -> bool:
    """判断某列是否为内部派生/追溯技术列，不应出现在业务交付表中。"""

    name = str(column).strip()
    if not name:
        return False
    if name.startswith("_"):
        return True
    lowered = name.lower()
    if lowered in _INTERNAL_COLUMN_NAMES:
        return True
    # 标准化派生字段（如 *_std）与通用布尔标记（如 is_active_*）不属于业务交付列。
    return lowered.endswith("_std") or lowered.startswith("is_active")


def compact_delivery_columns(
    df: pd.DataFrame,
    preferred_columns: list[str] | None = None,
    source_columns: set[str] | None = None,
) -> pd.DataFrame:
    """Drop internal/process columns and put the business-relevant ones first.

    ``source_columns`` names what the user uploaded, and nothing in it is ever dropped.
    ``_PROCESS_COLUMNS`` lists names this pipeline gives its own review annotations —
    including 备注 — so an uploaded 备注 column was deleted along with them, taking real
    content ("客户要求加急", "退货") with it. A column the user provided is theirs; only
    a name this pipeline invented is safe to remove by name.
    """

    if df.empty:
        return df.copy()

    protected = source_columns or set()
    result = df.drop(
        columns=[
            column
            for column in _PROCESS_COLUMNS
            if column in df.columns and column not in protected
        ]
    ).copy()
    result = result.drop(
        columns=[column for column in result.columns if is_technical_column(str(column))],
        errors="ignore",
    )

    preferred = preferred_columns or list(REVIEW_COLUMNS[:4])
    ordered = [column for column in preferred if column in result.columns]
    ordered.extend(column for column in result.columns if column not in ordered)
    return result[ordered].reset_index(drop=True)


def delivered_rows(business_result: pd.DataFrame) -> pd.DataFrame:
    if business_result.empty or STATUS_COLUMN not in business_result.columns:
        return business_result.copy()
    return business_result[business_result[STATUS_COLUMN].eq(DELIVERED_STATUS)].copy()


def withheld_rows(business_result: pd.DataFrame) -> pd.DataFrame:
    """Rows excluded from the primary result by the authorised exception policy."""

    if business_result.empty or STATUS_COLUMN not in business_result.columns:
        return business_result.iloc[0:0].copy()
    return business_result[~business_result[STATUS_COLUMN].eq(DELIVERED_STATUS)].copy()


def review_rows(business_result: pd.DataFrame) -> pd.DataFrame:
    if business_result.empty or STATUS_COLUMN not in business_result.columns:
        return business_result.iloc[0:0].copy()
    withheld = ~business_result[STATUS_COLUMN].eq(DELIVERED_STATUS)
    reported = business_result.get(
        REPORTED_PROBLEM_COLUMN,
        pd.Series(False, index=business_result.index),
    ).fillna(False).astype(bool)
    return business_result[withheld | reported].copy()


def delivery_result_sheet(
    business_result: pd.DataFrame,
    source_columns: set[str] | None = None,
) -> pd.DataFrame:
    """The 处理结果 sheet: delivered rows, business columns only."""

    result = compact_delivery_columns(
        delivered_rows(business_result), source_columns=source_columns
    )
    protected = source_columns or set()
    return result.drop(
        columns=[
            column
            for column in REVIEW_COLUMNS
            if column in result.columns and column not in protected
        ]
    )


def delivery_problem_sheet(
    business_result: pd.DataFrame,
    source_columns: set[str] | None = None,
) -> pd.DataFrame:
    """The 问题说明 sheet: requested problem rows, review columns first.

    A row may appear here while remaining in 处理结果 when the user asked to mark or
    inspect it but did not authorise withholding. Reporting and disposition are two
    independent projections of the same execution result.
    """

    problems = review_rows(business_result)
    if problems.empty:
        return pd.DataFrame(
            [
                {
                    STATUS_COLUMN: "全部通过",
                    "问题类型": "",
                    "建议处理": "无需处理，可直接使用处理结果。",
                }
            ]
        )
    # The row's delivery disposition may still be "通过" when the user asked only to
    # mark it. Inside the review projection it is nevertheless work to review, so the
    # business-facing status must describe this sheet rather than contradict it.
    problems = problems.copy()
    problems[STATUS_COLUMN] = "需处理"
    return compact_delivery_columns(
        problems,
        preferred_columns=list(REVIEW_COLUMNS),
        source_columns=source_columns,
    )


def normalize_field_key(name: str) -> str:
    """归一化字段名，便于跨大小写/空格/下划线做映射匹配。"""

    text = str(name).strip().lower()
    for char in (" ", "_", "-", "\t"):
        text = text.replace(char, "")
    return text


def resolve_import_source_column(field: str, columns: list[str]) -> str | None:
    """把目标系统字段解析为结果表中真实存在的列名。

    先精确匹配，再做大小写/空格/下划线不敏感的归一化匹配，确保导入视图取值口径与
    字段校验口径一致，避免假“可导入”与 NaN。
    """

    if field in columns:
        return field
    target = normalize_field_key(field)
    if not target:
        return None
    for column in columns:
        if normalize_field_key(column) == target:
            return column
    return None


def system_import_views(
    delivered: pd.DataFrame,
    import_requirements: pd.DataFrame,
    template_validation: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Project the delivered rows onto the target system's declared import fields."""

    fields = ordered_import_fields(import_requirements)
    if not fields:
        return pd.DataFrame(), pd.DataFrame()

    validation_by_field = {
        str(row.get("field", "")): row
        for row in (
            template_validation.to_dict(orient="records")
            if isinstance(template_validation, pd.DataFrame) and not template_validation.empty
            else []
        )
    }
    columns = [str(column) for column in delivered.columns]
    source_by_field = {field: resolve_import_source_column(field, columns) for field in fields}
    missing_fields = [
        field
        for field in fields
        if str(validation_by_field.get(field, {}).get("status", "")).lower() == "missing"
        or source_by_field.get(field) is None
    ]
    import_data = pd.DataFrame(index=delivered.index)
    for field in fields:
        source_column = source_by_field.get(field)
        import_data[field] = delivered[source_column] if source_column else ""
    import_data["导入校验状态"] = "可导入" if not missing_fields else "字段缺失待补充"
    import_data["不可导入原因"] = (
        "" if not missing_fields else f"缺少目标字段: {', '.join(missing_fields)}"
    )

    mapping_rows = []
    for field in fields:
        validation = validation_by_field.get(field, {})
        source_column = source_by_field.get(field)
        status = validation.get("status") or ("present" if source_column else "missing")
        resolved = source_column is not None and status != "missing"
        mapping_rows.append(
            {
                "目标系统字段": field,
                "当前字段状态": "已满足" if resolved else "缺失",
                "来源字段": source_column or "",
                "要求类型": validation.get("requirement_type", ""),
                "说明": validation.get("description", ""),
            }
        )
    return import_data.reset_index(drop=True), pd.DataFrame(mapping_rows)


def ordered_import_fields(import_requirements: pd.DataFrame) -> list[str]:
    if (
        not isinstance(import_requirements, pd.DataFrame)
        or import_requirements.empty
        or "field" not in import_requirements.columns
    ):
        return []
    fields: list[str] = []
    for field in import_requirements["field"].dropna().astype(str).tolist():
        if field and field not in fields:
            fields.append(field)
    return fields


# Uploaded import templates are header-only schemas, not data to clean. Both the
# executor-facing and delivery-facing layers need to exclude them the same way, so the
# detection lives here rather than in two identical private copies.
_TEMPLATE_NAME_TOKENS = ("template", "import", "导入", "模板")


def template_tables(source_inventory: pd.DataFrame) -> set[str]:
    """Names of uploaded tables that are import templates rather than data."""

    if not isinstance(source_inventory, pd.DataFrame) or source_inventory.empty:
        return set()
    result: set[str] = set()
    for row in source_inventory.to_dict(orient="records"):
        text = f"{row.get('table', '')} {row.get('file_name', '')}".lower()
        if any(token in text for token in _TEMPLATE_NAME_TOKENS):
            result.add(str(row.get("table", "")))
    return result


def data_table_fields(
    tables: dict[str, pd.DataFrame],
    source_inventory: pd.DataFrame,
) -> set[str]:
    """Every column available across the real data tables (templates excluded)."""

    excluded = template_tables(source_inventory)
    return {
        str(column)
        for table_name, table in tables.items()
        if table_name not in excluded
        for column in table.columns
    }


def missing_template_field_count(template_validation: pd.DataFrame) -> int:
    if (
        not isinstance(template_validation, pd.DataFrame)
        or template_validation.empty
        or "status" not in template_validation.columns
    ):
        return 0
    return int(template_validation["status"].eq("missing").sum())
