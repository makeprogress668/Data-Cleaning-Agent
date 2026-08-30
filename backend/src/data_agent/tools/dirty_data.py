from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.utils.field_names import looks_like_key_suffix

TEXT_INVISIBLE_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
DATE_NAME_PATTERN = re.compile(r"(date|time|day|日期|时间)", re.IGNORECASE)
AMOUNT_NAME_PATTERN = re.compile(r"(amount|cost|price|fee|金额|费用|价格)", re.IGNORECASE)
NON_NEGATIVE_NAME_PATTERN = re.compile(
    r"(qty|quantity|count|stock|inventory|age|duration|days|hours|数量|库存|年龄|时长|天数|小时|人数|件数)",
    re.IGNORECASE,
)
SUMMARY_ROW_PATTERN = re.compile(
    r"^(total|subtotal|summary|sum|合计|总计|小计|汇总)$",
    re.IGNORECASE,
)
COMMENT_ROW_PATTERN = re.compile(
    r"^(note|notes|remark|remarks|备注|说明|注释)[:：]?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DirtyDataResult:
    tables: dict[str, pd.DataFrame]
    issue_summary: pd.DataFrame
    record_issues: pd.DataFrame
    fix_audit: pd.DataFrame


def analyze_dirty_data(
    tables: dict[str, pd.DataFrame],
    auto_fix_safe_issues: bool = True,
) -> DirtyDataResult:
    fixed_tables: dict[str, pd.DataFrame] = {}
    issue_rows: list[dict[str, Any]] = []
    record_rows: list[dict[str, Any]] = []
    fix_rows: list[dict[str, Any]] = []

    for table_name, table in tables.items():
        fixed_table = table.copy()
        _detect_table_issues(table_name, fixed_table, issue_rows, record_rows)
        for column in list(fixed_table.columns):
            series = fixed_table[column]
            _detect_column_quality(
                table_name,
                column,
                series,
                issue_rows,
                record_rows,
                auto_fix_safe_issues,
            )
            _detect_business_numeric_issues(table_name, column, series, issue_rows, record_rows)
            _detect_date_issues(table_name, column, series, issue_rows, record_rows)
            _detect_duplicate_key(table_name, column, series, issue_rows, record_rows)
            if auto_fix_safe_issues:
                fixed_table[column] = _auto_fix_text_column(
                    table_name,
                    column,
                    series,
                    fix_rows,
                )
        fixed_tables[table_name] = fixed_table

    return DirtyDataResult(
        tables=fixed_tables,
        issue_summary=_issue_frame(issue_rows),
        record_issues=_record_issue_frame(record_rows),
        fix_audit=_fix_frame(fix_rows),
    )


def write_fixed_tables(tables: dict[str, pd.DataFrame], output_dir: str | Path) -> list[Path]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for table_name, table in tables.items():
        path = output_path / f"{_safe_file_stem(table_name)}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            table.to_excel(writer, sheet_name="data", index=False)
        paths.append(path)
    return paths


def _detect_table_issues(
    table: str,
    df: pd.DataFrame,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
) -> None:
    if df.empty:
        _add_issue(issue_rows, table, "", "empty_table", "warn", 0, "", False, False, False, True)
        return

    empty_row_mask = df.apply(_is_empty_row, axis=1)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="all_empty_rows",
        severity="warn",
        mask=empty_row_mask,
        series=pd.Series([""] * len(df), index=df.index),
        affects_final=False,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=True,
        action="删除全空行或确认是否为备注/分隔行",
    )

    duplicate_mask = df.duplicated(keep=False)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="duplicate_rows",
        severity="warn",
        mask=duplicate_mask,
        series=pd.Series([""] * len(df), index=df.index),
        affects_final=True,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=True,
        action="确认重复记录保留策略",
    )

    duplicate_columns = pd.Series(df.columns).duplicated(keep=False)
    if duplicate_columns.any():
        names = pd.Series(df.columns)[duplicate_columns].astype(str).tolist()
        _add_issue(
            issue_rows,
            table,
            "",
            "duplicate_columns",
            "error",
            len(names),
            ", ".join(names[:5]),
            True,
            False,
            False,
            True,
            "重命名重复字段后再导入或清洗",
        )

    blank_header_columns = [
        str(column)
        for column in df.columns
        if not str(column).strip() or str(column).lower().startswith("unnamed:")
    ]
    if blank_header_columns:
        _add_issue(
            issue_rows,
            table,
            "",
            "blank_or_unnamed_columns",
            "error",
            len(blank_header_columns),
            ", ".join(blank_header_columns[:5]),
            True,
            False,
            False,
            True,
            "确认是否存在空表头、合并单元格或表头读取错误",
        )

    repeated_header_mask = df.apply(lambda row: _is_repeated_header_row(row, df.columns), axis=1)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="repeated_header_rows",
        severity="warn",
        mask=repeated_header_mask,
        series=pd.Series(["重复表头"] * len(df), index=df.index),
        affects_final=True,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=True,
        action="删除混入数据区的重复表头行后再处理",
    )

    summary_row_mask = df.apply(_is_summary_row, axis=1)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="summary_rows_mixed_in_data",
        severity="warn",
        mask=summary_row_mask,
        series=pd.Series(["合计/汇总行"] * len(df), index=df.index),
        affects_final=True,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=True,
        action="确认合计/汇总行是否应从明细数据中剔除",
    )

    comment_row_mask = df.apply(_is_comment_row, axis=1)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="comment_rows_mixed_in_data",
        severity="warn",
        mask=comment_row_mask,
        series=pd.Series(["备注/说明行"] * len(df), index=df.index),
        affects_final=False,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=True,
        action="确认备注行是否应排除或转为元数据",
    )

    sparse_row_mask = df.apply(_is_sparse_possible_note_row, axis=1)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table=table,
        field="",
        issue_type="sparse_possible_note_rows",
        severity="info",
        mask=sparse_row_mask,
        series=pd.Series(["疑似说明/分隔行"] * len(df), index=df.index),
        affects_final=False,
        auto_fixable=False,
        auto_fixed=False,
        requires_review=False,
        action="抽样确认该行是否属于业务数据",
    )


def _detect_column_quality(
    table: str,
    field: str,
    series: pd.Series,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
    auto_fix_safe_issues: bool,
) -> None:
    null_mask = series.isna()
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "null_values",
        "error",
        null_mask,
        series,
        True,
        False,
        False,
        True,
        "补齐字段或确认允许为空",
    )

    text = series.dropna().astype(str)
    full_text = series.astype("object").where(series.notna(), "").astype(str)
    empty_mask = series.notna() & full_text.str.strip().eq("")
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "empty_strings",
        "warn",
        empty_mask,
        series,
        True,
        False,
        False,
        True,
        "补齐字段或确认允许为空字符串",
    )

    space_mask = series.notna() & full_text.ne(full_text.str.strip())
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "leading_trailing_spaces",
        "warn",
        space_mask,
        series,
        False,
        True,
        auto_fix_safe_issues,
        False,
        "自动去除前后空格",
    )

    invisible_mask = series.notna() & full_text.str.contains(TEXT_INVISIBLE_PATTERN, regex=True)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "invisible_characters",
        "warn",
        invisible_mask,
        series,
        False,
        True,
        auto_fix_safe_issues,
        False,
        "自动移除不可见字符",
    )

    fullwidth_mask = series.notna() & full_text.map(_contains_fullwidth)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "fullwidth_characters",
        "warn",
        fullwidth_mask,
        series,
        False,
        True,
        auto_fix_safe_issues,
        False,
        "自动转换全角字符为半角字符",
    )

    _detect_mixed_case(table, field, text, issue_rows)
    _detect_numeric_stored_as_text(table, field, text, issue_rows, record_rows, series)


def _negatives_look_like_outliers(numeric: pd.Series) -> bool:
    """Whether negative values in this column are anomalies rather than its nature.

    "amount/金额 must not be negative" is a guess made from the column *name*. It holds
    for an order table where one row slipped through negative, and is simply wrong for
    a refund, discount or adjustment column, where it used to withhold 100% of the rows
    and fill the review list with noise.

    A column that never goes positive is negative by design, so the agent says nothing.
    As soon as positive values exist, the negatives stand out against them and are
    worth flagging.
    """

    return bool(numeric.dropna().gt(0).any())


def _detect_business_numeric_issues(
    table: str,
    field: str,
    series: pd.Series,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
) -> None:
    numeric = pd.to_numeric(series, errors="coerce")
    if not numeric.notna().any():
        return

    negative_issue = _negative_issue_type(field)
    if negative_issue and _negatives_look_like_outliers(numeric):
        negative_mask = numeric.lt(0).fillna(False)
        action = (
            "确认金额是否允许为负或修正原始记录"
            if negative_issue == "negative_amount"
            else "确认该数值字段是否允许为负或修正原始记录"
        )
        _add_issue_from_mask(
            issue_rows,
            record_rows,
            table,
            field,
            negative_issue,
            "error",
            negative_mask,
            series,
            True,
            False,
            False,
            True,
            action,
        )

    non_null = numeric.dropna()
    if len(non_null) < 4:
        return
    q1 = non_null.quantile(0.25)
    q3 = non_null.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    outlier_mask = numeric.lt(lower).fillna(False) | numeric.gt(upper).fillna(False)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "numeric_outlier",
        "warn",
        outlier_mask,
        series,
        True,
        False,
        False,
        True,
        "人工确认极端值是否合理",
    )


def _detect_date_issues(
    table: str,
    field: str,
    series: pd.Series,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
) -> None:
    text = series.dropna().astype(str).str.strip()
    if text.empty or not DATE_NAME_PATTERN.search(str(field)):
        return
    parsed = pd.to_datetime(text, errors="coerce")
    invalid_rate = float(parsed.isna().mean())
    if invalid_rate > 0:
        invalid_values = text[parsed.isna()]
        mask = series.astype(str).isin(set(invalid_values.tolist()))
        _add_issue_from_mask(
            issue_rows,
            record_rows,
            table,
            field,
            "date_parse_failed",
            "error",
            mask,
            series,
            True,
            False,
            False,
            True,
            "统一日期格式或修正非法日期",
        )

    pattern_count = text.map(_date_pattern).nunique()
    if pattern_count > 1:
        _add_issue(
            issue_rows,
            table,
            field,
            "mixed_date_formats",
            "warn",
            int(len(text)),
            ", ".join(text.head(5).tolist()),
            False,
            False,
            False,
            True,
            "统一日期格式后再导入系统",
        )


def _detect_duplicate_key(
    table: str,
    field: str,
    series: pd.Series,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
) -> None:
    if not looks_like_key_suffix(field):
        return
    normalized = series.dropna().astype(str).str.strip()
    duplicated_values = set(normalized[normalized.duplicated(keep=False)].tolist())
    if not duplicated_values:
        return
    mask = series.astype(str).str.strip().isin(duplicated_values)
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "duplicate_key",
        "warn",
        mask,
        series,
        True,
        False,
        False,
        True,
        "确认 key 是否应唯一并制定去重策略",
    )


def _detect_mixed_case(
    table: str,
    field: str,
    text: pd.Series,
    issue_rows: list[dict[str, Any]],
) -> None:
    if text.empty:
        return
    normalized = text.str.lower()
    if normalized.nunique() < text.nunique():
        _add_issue(
            issue_rows,
            table,
            field,
            "case_mixed_duplicates",
            "warn",
            int(len(text)),
            ", ".join(text.head(5).tolist()),
            False,
            True,
            False,
            False,
            "按匹配规则统一大小写或使用忽略大小写匹配",
        )


def _detect_numeric_stored_as_text(
    table: str,
    field: str,
    text: pd.Series,
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
    original: pd.Series,
) -> None:
    if text.empty or pd.api.types.is_numeric_dtype(original):
        return
    parsed = pd.to_numeric(text, errors="coerce")
    parse_rate = float(parsed.notna().mean())
    if parse_rate < 0.8:
        return
    mask = original.notna() & original.astype(str).str.strip().ne("")
    _add_issue_from_mask(
        issue_rows,
        record_rows,
        table,
        field,
        "numeric_stored_as_text",
        "warn",
        mask,
        original,
        False,
        False,
        False,
        True,
        "确认字段类型后转换为数值",
    )


def _auto_fix_text_column(
    table: str,
    field: str,
    series: pd.Series,
    fix_rows: list[dict[str, Any]],
) -> pd.Series:
    if not pd.api.types.is_object_dtype(series) and not pd.api.types.is_string_dtype(series):
        return series

    fixed = series.copy()
    for index, value in series.items():
        if pd.isna(value):
            continue
        before = str(value)
        after = _normalize_safe_text(before)
        if before == after:
            continue
        fixed.at[index] = after
        fix_rows.append(
            {
                "table": table,
                "row_index": index,
                "field": field,
                "before_value": before,
                "after_value": after,
                "fix_type": "safe_text_normalization",
                "reason_code": "DIRTY_TEXT_AUTO_FIX",
            }
        )
    return fixed


def _add_issue_from_mask(
    issue_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
    table: str,
    field: str,
    issue_type: str,
    severity: str,
    mask: pd.Series,
    series: pd.Series,
    affects_final: bool,
    auto_fixable: bool,
    auto_fixed: bool,
    requires_review: bool,
    action: str,
) -> None:
    mask = mask.fillna(False).astype(bool)
    if not bool(mask.any()):
        return
    samples = series[mask].dropna().astype(str).head(5).tolist()
    _add_issue(
        issue_rows,
        table,
        field,
        issue_type,
        severity,
        int(mask.sum()),
        ", ".join(samples),
        affects_final,
        auto_fixable,
        auto_fixed,
        requires_review,
        action,
    )
    for row_index, value in series[mask].head(100).items():
        record_rows.append(
            {
                "table": table,
                "row_index": row_index,
                "field": field,
                "issue_type": issue_type,
                "severity": severity,
                "current_value": "" if pd.isna(value) else value,
                "affects_final_result": affects_final,
                "auto_fixable": auto_fixable,
                "auto_fixed": auto_fixed,
                "requires_review": requires_review,
                "recommended_action": action,
            }
        )


def _add_issue(
    issue_rows: list[dict[str, Any]],
    table: str,
    field: str,
    issue_type: str,
    severity: str,
    affected_rows: int,
    sample_values: str,
    affects_final: bool,
    auto_fixable: bool,
    auto_fixed: bool,
    requires_review: bool,
    action: str = "人工确认后处理",
) -> None:
    issue_rows.append(
        {
            "table": table,
            "field": field,
            "issue_type": issue_type,
            "severity": severity,
            "affected_rows": int(affected_rows),
            "sample_values": sample_values,
            "affects_final_result": affects_final,
            "auto_fixable": auto_fixable,
            "auto_fixed": auto_fixed,
            "requires_review": requires_review,
            "recommended_action": action,
        }
    )


def _issue_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [
        "table",
        "field",
        "issue_type",
        "severity",
        "affected_rows",
        "sample_values",
        "affects_final_result",
        "auto_fixable",
        "auto_fixed",
        "requires_review",
        "recommended_action",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["severity", "affected_rows", "issue_type"],
        ascending=[True, False, True],
        ignore_index=True,
    )


def _record_issue_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [
        "table",
        "row_index",
        "field",
        "issue_type",
        "severity",
        "current_value",
        "affects_final_result",
        "auto_fixable",
        "auto_fixed",
        "requires_review",
        "recommended_action",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["table", "row_index", "field"],
        ignore_index=True,
    )


def _fix_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [
        "table",
        "row_index",
        "field",
        "before_value",
        "after_value",
        "fix_type",
        "reason_code",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def _negative_issue_type(field: object) -> str | None:
    field_text = str(field)
    if AMOUNT_NAME_PATTERN.search(field_text):
        return "negative_amount"
    if NON_NEGATIVE_NAME_PATTERN.search(field_text):
        return "negative_numeric_value"
    return None


def non_data_row_mask(df: pd.DataFrame) -> pd.Series:
    """Rows that are page furniture rather than records: blank, repeated header, 合计.

    These stay in the delivered detail — nobody asked for them to be removed — but a
    合计 row must not be added into a total, and a repeated header row must not become
    its own category. Excluding them from the summary is arithmetic, not a judgement
    about the user's data: adding a subtotal to the numbers it already subtotals is
    simply the wrong answer.

    Detection lives here rather than inside the dirty-data engine because that engine
    only runs when the user asked for cleaning, and a totals row skews an analysis
    whether or not cleaning was requested.
    """

    if df.empty:
        return pd.Series(dtype=bool, index=df.index)
    return df.apply(
        lambda row: _is_empty_row(row)
        or _is_summary_row(row)
        or _is_repeated_header_row(row, df.columns),
        axis=1,
    ).astype(bool)


def _is_empty_row(row: pd.Series) -> bool:
    return all(pd.isna(value) or str(value).strip() == "" for value in row.tolist())


def _is_repeated_header_row(row: pd.Series, columns: pd.Index) -> bool:
    row_values = [_normalize_cell(value) for value in row.tolist()]
    column_values = [_normalize_cell(column) for column in columns.tolist()]
    pairs = [
        (row_value, column_value)
        # Ragged sheets are the normal input here, so compare the overlap rather than
        # raising when a row is shorter or longer than the header.
        for row_value, column_value in zip(row_values, column_values, strict=False)
        if row_value or column_value
    ]
    if len(pairs) < 2:
        return False
    matched = sum(1 for row_value, column_value in pairs if row_value == column_value)
    return matched >= max(2, int(len(pairs) * 0.6))


def _is_summary_row(row: pd.Series) -> bool:
    first_text = _first_non_empty_text(row)
    return bool(first_text and SUMMARY_ROW_PATTERN.match(first_text.strip()))


def _is_comment_row(row: pd.Series) -> bool:
    first_text = _first_non_empty_text(row)
    return bool(first_text and COMMENT_ROW_PATTERN.match(first_text.strip()))


def _is_sparse_possible_note_row(row: pd.Series) -> bool:
    values = [_normalize_cell(value) for value in row.tolist()]
    non_empty = [value for value in values if value]
    if len(non_empty) != 1 or len(values) < 3:
        return False
    only_value = non_empty[0]
    if SUMMARY_ROW_PATTERN.match(only_value) or COMMENT_ROW_PATTERN.match(only_value):
        return False
    return len(only_value) >= 8


def _first_non_empty_text(row: pd.Series) -> str:
    for value in row.tolist():
        text = _normalize_cell(value)
        if text:
            return text
    return ""


def _normalize_cell(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lower()


def _contains_fullwidth(value: str) -> bool:
    return any(ord(char) == 12288 or 65281 <= ord(char) <= 65374 for char in value)


def _normalize_safe_text(value: str) -> str:
    value = TEXT_INVISIBLE_PATTERN.sub("", value).strip()
    chars = []
    for char in value:
        code = ord(char)
        if code == 12288:
            chars.append(" ")
        elif 65281 <= code <= 65374:
            chars.append(chr(code - 65248))
        else:
            chars.append(char)
    return "".join(chars).strip()


def _date_pattern(value: str) -> str:
    return re.sub(r"\d+", "0", value)


def _safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return stem or "table"
