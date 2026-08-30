import pandas as pd

from data_agent.planning.goal_interpreter import derive_capabilities


def _tables(multi: bool = True) -> dict[str, pd.DataFrame]:
    tables = {"orders": pd.DataFrame({"order_id": ["O1"], "amount": [10]})}
    if multi:
        tables["customers"] = pd.DataFrame({"customer_id": ["C1"], "name": ["A"]})
    return tables


def test_plain_clean_goal_enables_only_inplace() -> None:
    caps = derive_capabilities("清洗订单数据，规范化客户名称", _tables())
    assert caps["wants_inplace"] is True
    assert caps["wants_annotation"] is False
    # A plain cleaning goal must not turn on analysis / charts / lookup.
    assert caps["needs_analysis"] is False
    assert caps["needs_charts"] is False
    assert caps["needs_lookup"] is False


def test_annotation_goal_enables_annotation_not_inplace() -> None:
    caps = derive_capabilities("找出并标注手机号异常的记录", _tables())
    assert caps["wants_annotation"] is True
    assert caps["wants_inplace"] is False


def test_analysis_goal_enables_analysis_and_charts() -> None:
    caps = derive_capabilities("按月份统计各产品销售额并生成图表", _tables())
    assert caps["needs_analysis"] is True
    assert caps["needs_charts"] is True


def test_merge_goal_keeps_the_lookup_intent_even_when_it_cannot_be_satisfied() -> None:
    """Intent is recorded here; feasibility is decided later, against the real profile.

    Gating the intent on table count made "关联客户表" with a single upload produce a
    plan with no join, no warning and a perfect score — the user was never told the
    thing they asked for had not happened. The intent now survives, and the planner
    marks it unmet (with a stated reason) once it sees there is no usable key.
    """

    goal = "关联客户表，匹配补齐客户名称"
    assert derive_capabilities(goal, _tables(multi=True))["needs_lookup"] is True
    assert derive_capabilities(goal, _tables(multi=False))["needs_lookup"] is True
    # A goal that never mentions matching still does not turn it on.
    assert derive_capabilities("清洗订单数据", _tables(multi=True))["needs_lookup"] is False


def test_empty_goal_authorises_nothing() -> None:
    """An empty goal is not permission to clean.

    This used to fall back to in-place cleaning, so a goal that named nothing —
    or named only a lookup — silently trimmed whitespace, stripped invisible
    characters and rewrote full-width text across the whole table.
    """

    caps = derive_capabilities("", _tables())
    assert caps["wants_inplace"] is False
    assert caps["needs_analysis"] is False
    assert caps["needs_charts"] is False
    assert caps["needs_lookup"] is False


def test_exception_goal_flags_review() -> None:
    caps = derive_capabilities("清洗数据并列出不能导入的异常记录", _tables())
    assert caps["needs_exception_review"] is True
