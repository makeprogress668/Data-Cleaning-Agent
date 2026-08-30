"""内部标签不许出现在用户眼前。

异常标签是工程内部的记号：``duplicate_订单号``、``amount_null_values``、
``dirty:客户名称:leading_trailing_spaces``、``doc_rule_金额_min_value``。业务人员打开
工作簿看到的必须是「订单号 重复」「前后空格」，而不是这些。

这类 bug 反复出现过，原因是翻译逻辑散落在构建审计表的代码里 —— 同一个问题从一个入口
出来是中文、从另一个入口出来是原始记号，改一处修不完。拆出 ``issue_wording`` 之后，
这个文件就是那条规则的验收点。
"""

from __future__ import annotations

import pytest

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
    display_dirty_issue_name,
    display_issue_name,
    display_severity,
    label_description,
    suggested_annotation_action,
)

# --- 措辞：用户读到的每一个字 --------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("duplicate_record", "重复记录"),
        ("duplicate_订单号", "订单号 重复"),
        ("amount_null_values", "金额为空"),
        ("客户名称_null_values", "客户名称 为空"),
        ("备注_empty_strings", "备注 为空字符串"),
        ("products_by_product_id_not_found", "关联数据未匹配"),
        ("dirty:客户名称:leading_trailing_spaces", "前后空格"),
        ("dirty:金额:numeric_stored_as_text", "数字存成文本"),
        ("doc_rule_金额_min_value", "金额 小于允许的最小值"),
        ("doc_rule_客户名称_required", "客户名称 必填项缺失"),
    ],
)
def test_every_label_shape_reaches_the_user_in_business_language(
    label: str, expected: str
) -> None:
    assert display_issue_name(label) == expected


def test_an_unrecognised_label_is_passed_through_not_mangled() -> None:
    """认不出来就原样返回 —— 显示原文可以查证，编一个说法不行。"""

    assert display_issue_name("某个没见过的标签") == "某个没见过的标签"


def test_severity_is_shown_in_chinese() -> None:
    assert display_severity("error") == "错误"
    assert display_severity("warn") == "提醒"
    assert display_severity("info") == "信息"


def test_an_unknown_dirty_issue_type_falls_back_to_its_raw_name() -> None:
    assert display_dirty_issue_name("all_empty_rows") == "全空行"
    assert display_dirty_issue_name("brand_new_issue") == "brand_new_issue"


def test_每个标签都有可执行的处理建议() -> None:
    assert suggested_annotation_action("orders_not_found") == "确认映射关系或补充主数据"
    assert suggested_annotation_action("金额_null_values") == "补齐字段或确认允许为空"
    assert suggested_annotation_action("duplicate_订单号") == "确认保留策略并去重"
    assert suggested_annotation_action("随便什么") == "人工确认后决定处理方式"


def test_label_description_explains_the_class_not_the_token() -> None:
    assert label_description("orders_not_found") == "跨表 lookup 未匹配到维表记录"
    assert label_description("duplicate_订单号") == "字段存在重复值"


# --- 归类：标签的结构，与措辞无关 ----------------------------------------------


def test_dirty_labels_split_into_field_and_issue_type() -> None:
    assert dirty_label_parts(f"{DIRTY_ISSUE_PREFIX}客户名称:leading_trailing_spaces") == (
        "客户名称",
        "leading_trailing_spaces",
    )


def test_a_dirty_label_without_a_field_still_yields_its_issue_type() -> None:
    assert dirty_label_parts(f"{DIRTY_ISSUE_PREFIX}empty_table") == ("", "empty_table")


@pytest.mark.parametrize(
    ("label", "source", "category", "code"),
    [
        ("orders_not_found", "lookup_rule", "lookup_mapping", "LOOKUP_NOT_FOUND"),
        ("金额_null_values", "profile_quality_rule", "data_quality", "PROFILE_NULL_VALUES"),
        (
            "备注_empty_strings",
            "profile_quality_rule",
            "data_quality",
            "PROFILE_EMPTY_STRINGS",
        ),
        ("duplicate_订单号", "duplicate_rule", "duplicate", "DUPLICATE_RECORD"),
    ],
)
def test_a_label_classifies_consistently_across_the_three_axes(
    label: str, source: str, category: str, code: str
) -> None:
    assert classify_label_source(label) == source
    assert taxonomy_category(label) == category
    assert classify_reason_code(label) == code


def test_duplicates_are_a_warning_and_missing_data_is_an_error() -> None:
    assert severity_for_label("duplicate_订单号") == "warn"
    assert severity_for_label("备注_empty_strings") == "warn"
    assert severity_for_label("orders_not_found") == "error"


def test_a_lookup_rule_name_drops_the_not_found_suffix() -> None:
    assert hit_rule_name("orders_not_found", "lookup_rule") == "orders"
    assert hit_rule_name("duplicate_订单号", "duplicate_rule") == "duplicate_订单号"


# --- 分层本身 -----------------------------------------------------------------


def test_the_wording_layer_never_touches_dataframes() -> None:
    """措辞层一旦开始读 DataFrame，就又变回原来那个什么都装的文件了。

    这两层拆开的意义就在于：给一个标签、拿回一句话。真正的价值不是行数变少，而是
    改一句话不需要看懂审计表怎么构建的。
    """

    import data_agent.tools.issue_taxonomy as taxonomy
    import data_agent.tools.issue_wording as wording

    for module in (wording, taxonomy):
        assert not hasattr(module, "pd"), f"{module.__name__} 开始依赖 pandas 了"
