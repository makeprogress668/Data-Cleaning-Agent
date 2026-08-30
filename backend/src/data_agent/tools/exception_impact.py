from __future__ import annotations

import pandas as pd


def analyze_exception_impact(
    exception_summary: pd.DataFrame,
    dirty_issue_summary: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for row in exception_summary.to_dict(orient="records"):
        severity = str(row.get("严重级别", ""))
        rows.append(
            {
                "异常类型": row.get("异常类型", ""),
                "影响字段": "",
                "影响行数": int(row.get("影响行数", 0)),
                "严重级别": severity,
                "是否影响最终结果": _affects_final_by_severity(severity),
                "是否已自动修复": "否",
                "是否需要人工确认": "是",
                "建议处理": row.get("建议处理", ""),
                "来源": "cleaning_rule",
            }
        )

    for row in dirty_issue_summary.to_dict(orient="records"):
        rows.append(
            {
                "异常类型": _dirty_issue_name(str(row.get("issue_type", ""))),
                "影响字段": row.get("field", ""),
                "影响行数": int(row.get("affected_rows", 0)),
                "严重级别": _display_severity(row.get("severity", "")),
                "是否影响最终结果": "是" if row.get("affects_final_result") else "否",
                "是否已自动修复": "是" if row.get("auto_fixed") else "否",
                "是否需要人工确认": "是" if row.get("requires_review") else "否",
                "建议处理": row.get("recommended_action", ""),
                "来源": "dirty_data_engine",
            }
        )

    columns = [
        "异常类型",
        "影响字段",
        "影响行数",
        "严重级别",
        "是否影响最终结果",
        "是否已自动修复",
        "是否需要人工确认",
        "建议处理",
        "来源",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["是否影响最终结果", "影响行数"],
        ascending=[False, False],
        ignore_index=True,
    )


def _affects_final_by_severity(severity: str) -> str:
    if severity == "错误":
        return "是"
    if severity == "提醒":
        return "部分影响"
    return "否"


def _display_severity(severity: object) -> str:
    mapping = {"error": "错误", "warn": "提醒", "info": "信息"}
    return mapping.get(str(severity), str(severity))


def _dirty_issue_name(issue_type: str) -> str:
    names = {
        "all_empty_rows": "全空行",
        "duplicate_rows": "重复行",
        "duplicate_columns": "重复列名",
        "null_values": "空值",
        "empty_strings": "空字符串",
        "leading_trailing_spaces": "前后空格",
        "invisible_characters": "不可见字符",
        "fullwidth_characters": "全角字符",
        "case_mixed_duplicates": "大小写混乱",
        "numeric_stored_as_text": "数字存成文本",
        "negative_amount": "金额为负",
        "negative_numeric_value": "数值为负",
        "numeric_outlier": "数值极端值",
        "date_parse_failed": "日期解析失败",
        "mixed_date_formats": "日期格式混乱",
        "duplicate_key": "重复 key",
    }
    return names.get(issue_type, issue_type)
