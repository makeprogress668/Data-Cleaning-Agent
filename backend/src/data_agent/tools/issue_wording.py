"""Every string a business user reads about a data issue, in one place.

The rule this module exists to enforce: an internal token must never reach the user.
``duplicate_订单号`` is engineering shorthand; the user is owed 「订单号 重复」. When
that translation lived inside the audit-frame builders, the same issue could come out
as Chinese through one entry point and as a raw token through another, and a wording
fix had to be made in as many places as it had leaked into.

Nothing here reads a DataFrame. Give it a label, get back the words — which is also
why it is the module to change when a phrase reads wrong to a business user.
"""

from __future__ import annotations

from data_agent.tools.issue_taxonomy import DIRTY_ISSUE_PREFIX, dirty_label_parts

# Single source of truth for dirty-data issue labels shown to business users. Both the
# CLI delivery path and the deterministic runner render through it, so the same issue
# never appears as Chinese in one entry point and as a raw token in another.
DIRTY_ISSUE_DISPLAY_NAMES = {
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
    "duplicate_key": "关键字段重复",
    "blank_or_unnamed_columns": "空表头或未命名列",
    "repeated_header_rows": "重复表头行",
    "summary_rows_mixed_in_data": "合计汇总行混入明细",
    "comment_rows_mixed_in_data": "备注说明行混入明细",
    "sparse_possible_note_rows": "疑似说明或分隔行",
    "empty_table": "空表",
    "safe_text_normalization": "规范文本（去空格/不可见字符/全角）",
}

# 文档规则类型 -> 面向业务用户的中文说明模板（{field} 为字段名）。
DOC_RULE_TYPE_TEMPLATES = {
    "required": "{field} 必填项缺失",
    "min_value": "{field} 小于允许的最小值",
    "max_value": "{field} 超过允许的最大值",
    "enum": "{field} 取值不在允许范围",
    "format": "{field} 格式不正确",
    "type_date": "{field} 不是有效日期",
    "type_numeric": "{field} 不是有效数值",
    "type_integer": "{field} 不是有效整数",
    "cleaning_requirement": "{field} 不符合清洗要求",
}


def display_severity(severity: object) -> str:
    mapping = {"error": "错误", "warn": "提醒", "info": "信息"}
    return mapping.get(str(severity), str(severity))


def display_dirty_issue_name(issue_type: object) -> str:
    """Translate a raw dirty-data issue type into business language."""

    text = str(issue_type)
    return DIRTY_ISSUE_DISPLAY_NAMES.get(text, text)


def display_issue_name(label: object) -> str:
    label_text = str(label)
    names = {
        "duplicate_record": "重复记录",
        "amount_null_values": "金额为空",
    }
    if label_text in names:
        return names[label_text]
    if label_text.startswith(DIRTY_ISSUE_PREFIX):
        _field, issue_type = dirty_label_parts(label_text)
        return DIRTY_ISSUE_DISPLAY_NAMES.get(issue_type, issue_type)
    doc_rule_name = _display_doc_rule_name(label_text)
    if doc_rule_name:
        return doc_rule_name
    if label_text.endswith("_not_found"):
        return "关联数据未匹配"
    if label_text.startswith("duplicate_"):
        # The matcher names these after the key it checked ("duplicate_订单号"), which
        # is engineering shorthand leaking straight onto the user's findings list.
        return f"{label_text.removeprefix('duplicate_')} 重复"
    if label_text.endswith("_null_values"):
        return f"{label_text.removesuffix('_null_values')} 为空"
    if label_text.endswith("_empty_strings"):
        return f"{label_text.removesuffix('_empty_strings')} 为空字符串"
    return label_text


def _display_doc_rule_name(label: str) -> str | None:
    """把 ``doc_rule_<field>_<type>`` 内部标签翻译成中文业务说明。"""

    if not label.startswith("doc_rule_"):
        return None
    body = label[len("doc_rule_") :]
    for rule_type, template in DOC_RULE_TYPE_TEMPLATES.items():
        suffix = f"_{rule_type}"
        if body.endswith(suffix):
            field = body[: -len(suffix)] or "字段"
            return template.format(field=field)
    return f"{body} 校验未通过" if body else "文档规则校验未通过"


def suggested_annotation_action(label: str) -> str:
    if label.endswith("_not_found"):
        return "确认映射关系或补充主数据"
    if label.endswith("_null_values") or label.endswith("_empty_strings"):
        return "补齐字段或确认允许为空"
    if label == "duplicate_record" or label.startswith("duplicate_"):
        return "确认保留策略并去重"
    return "人工确认后决定处理方式"


def label_description(label: str) -> str:
    descriptions = {
        "duplicate_record": "主表记录存在重复 key",
    }
    if label in descriptions:
        return descriptions[label]
    if label.endswith("_not_found"):
        return "跨表 lookup 未匹配到维表记录"
    if label.endswith("_null_values"):
        return "字段存在空值"
    if label.endswith("_empty_strings"):
        return "字段存在空字符串"
    if label.startswith("duplicate_"):
        return "字段存在重复值"
    return "自定义异常标签"
