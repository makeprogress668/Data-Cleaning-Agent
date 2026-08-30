"""同一份数据分成几个文件，要当成一张表读。

「把两个月的销售数据合并成一张表」交付了四行里的两行。流水线是「一张主表 + 若干关联
表」的结构，第二个月既不是主表也不是关联目标，于是它什么都没贡献 —— 不报错、不告警，
一份看起来完全正确、少了一半记录的工作簿。这一轮查出来的问题里，这个最不容易被发现。

十二个月的导出、列完全一样，那不是十二张表，是源系统只能这么导的一张表。把它们读成
一张不是替用户多做了一步，是把输入读对了。

但结构相同不等于同一份数据 —— 预算表和实际表的列也可以完全一样，合起来就是垃圾。
所以：目标里说了就合并，没说就问，绝不自己认定。
"""

from __future__ import annotations

import pandas as pd

from data_agent.agent.goal_understanding import understand_goal
from data_agent.planning.goal_interpreter import apply_goal_to_job_config
from data_agent.schemas.task import TaskSpec
from data_agent.tools.table_union import (
    SOURCE_TABLE_COLUMN,
    union_tables,
    unionable_groups,
)


def _monthly() -> dict[str, pd.DataFrame]:
    return {
        "销售_1月": pd.DataFrame({"订单号": ["1月-1", "1月-2"], "金额": [100, 200]}),
        "销售_2月": pd.DataFrame({"订单号": ["2月-1", "2月-2"], "金额": [300, 400]}),
    }


# --- 识别 ---------------------------------------------------------------------


def test_same_columns_makes_a_group() -> None:
    assert unionable_groups(_monthly()) == [["销售_1月", "销售_2月"]]


def test_a_near_match_is_a_different_table() -> None:
    """差一列就不是同一张表。硬叠起来会造出一整列几乎全空的值。"""

    tables = _monthly()
    tables["销售_3月"] = pd.DataFrame({"订单号": ["3月-1"], "金额": [500], "备注": ["加急"]})

    assert unionable_groups(tables) == [["销售_1月", "销售_2月"]]


def test_column_order_does_not_split_a_group() -> None:
    """同一个导出跑两次，列序可能不同，但那还是同一张表。"""

    tables = {
        "a": pd.DataFrame({"订单号": ["1"], "金额": [100]}),
        "b": pd.DataFrame({"金额": [200], "订单号": ["2"]}),
    }
    assert unionable_groups(tables) == [["a", "b"]]


def test_a_lone_table_is_not_a_group() -> None:
    assert unionable_groups({"只有一张": pd.DataFrame({"a": [1]})}) == []


# --- 合并 ---------------------------------------------------------------------


def test_stacking_keeps_every_row_and_records_where_it_came_from() -> None:
    combined = union_tables(_monthly(), ["销售_1月", "销售_2月"])

    assert len(combined) == 4
    assert combined["订单号"].tolist() == ["1月-1", "1月-2", "2月-1", "2月-2"]
    assert combined[SOURCE_TABLE_COLUMN].tolist() == [
        "销售_1月",
        "销售_1月",
        "销售_2月",
        "销售_2月",
    ]


# --- 授权：说了才合并 ----------------------------------------------------------


def test_a_goal_that_says_merge_gets_a_union() -> None:
    tables = _monthly()
    plan = understand_goal("把两个月的销售数据合并成一张表", tables)
    task = TaskSpec.model_validate(plan["task_spec"])

    assert task.union, "用户明确要求合并，却没有合并计划"
    assert task.union[0]["tables"] == ["销售_1月", "销售_2月"]
    assert task.union[0]["authorization"] == "stated"
    assert not any(slot.slot_id == "primary_table" for slot in task.missing_slots), (
        "已明确合并的同结构文件不需要再选择一张主表"
    )

    job = apply_goal_to_job_config({"formulas": []}, "把两个月的销售数据合并成一张表", tables, plan)
    assert job["union_tables"] == ["销售_1月", "销售_2月"]


def test_a_goal_that_does_not_ask_gets_a_question_instead_of_a_guess() -> None:
    """结构一样也可能是预算 vs 实际。不合并可以，但不能一声不吭只用其中一张。"""

    tables = _monthly()
    task = TaskSpec.model_validate(
        understand_goal("统计金额合计", tables)["task_spec"]
    )

    assert task.union == [], "目标没说要合并，却自己合了"
    questions = [slot.question for slot in task.missing_slots]
    assert any("结构完全相同" in question for question in questions), (
        "只用了其中一张表，却没有告诉用户"
    )


def test_a_single_table_upload_raises_no_union_question() -> None:
    """只有一张表时不该平白多问一句。"""

    tables = {"订单明细": pd.DataFrame({"订单号": ["O1"], "金额": [100]})}
    task = TaskSpec.model_validate(
        understand_goal("统计金额合计", tables)["task_spec"]
    )

    assert not any("结构完全相同" in slot.question for slot in task.missing_slots)
