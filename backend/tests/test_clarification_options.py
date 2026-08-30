"""澄清要做成选择题，而且越问越准。

成熟产品没有一个用开放式提问：Cortex Analyst 给备选问题，WrenAI 给三个候选解读，
AmbiSQL 用多选题。原因都一样 —— 能写出「按 status 字段，保留 paid」的用户根本不需要
被问；给他一个空白框，他答得含糊或者干脆放弃，给他四个按钮，他答得准。

候选项必须来自这份数据本身。哪些列能筛、哪些像键、哪些适合当维度，是关于这次上传的
问题，每次都不一样。

记忆改变的是**问题**，不是**问不问**：上个月按城市统计过，这次「按什么统计」里城市
排第一。再问一遍不是失败 —— 上个月重要的维度这个月仍然需要确认。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.agent.goal_understanding import understand_goal
from data_agent.agent.memory import record_clarification_choice
from data_agent.planning.clarification_options import (
    NO_GROUPING,
    WHOLE_ROW_DUPLICATE,
    dedup_key_options,
    filter_field_options,
    group_by_options,
    lead_with_remembered,
)
from data_agent.schemas.task import TaskSpec


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "订单": pd.DataFrame(
            {
                "订单号": ["O1", "O2", "O3", "O4"],
                "客户编号": ["C1", "C2", "C1", "C3"],
                "状态": ["已付", "未付", "已付", "已取消"],
                "城市": ["北京", "上海", "北京", "广州"],
                "金额": [100, 200, 300, 400],
            }
        )
    }


# --- 候选来自数据，不是写死的 ---------------------------------------------------


def test_filter_options_are_real_conditions_from_this_upload() -> None:
    """「状态」还不是答案 —— 用户要说的是哪个状态。"""

    options = filter_field_options(_tables())

    assert any(option.startswith("状态 = ") for option in options)
    assert not any(option.startswith("订单号") for option in options), (
        "每行一个值的列不是筛选条件"
    )


def test_dedup_options_offer_the_key_and_excel_s_own_default() -> None:
    options = dedup_key_options(_tables())

    assert "订单号" in options
    assert options[-1] == WHOLE_ROW_DUPLICATE, "Excel 删除重复项的默认行为要给出来"


def test_group_options_offer_dimensions_and_the_grand_total() -> None:
    options = group_by_options(_tables())

    assert {"状态", "城市"} <= set(options)
    assert options[-1] == NO_GROUPING, "「只要总计」是真答案，不该让用户自己想到"


def test_a_measure_column_is_not_offered_as_a_dimension() -> None:
    assert "金额" not in group_by_options(_tables())


def test_a_key_like_column_can_still_be_a_dimension() -> None:
    """「按客户统计」就是按客户编号分组。名字像键不代表不能当维度，
    真正不能分组的是每行一个值的纯标识列。"""

    assert "客户编号" in group_by_options(_tables())
    assert "订单号" not in group_by_options(_tables())


# --- 记忆：改变问题，不改变问不问 -----------------------------------------------


def test_a_remembered_choice_leads_the_list() -> None:
    assert lead_with_remembered(["状态", "城市", NO_GROUPING], ["城市"]) == [
        "城市",
        "状态",
        NO_GROUPING,
    ]


def test_a_remembered_choice_absent_from_this_upload_is_dropped() -> None:
    """上个月的列这个月可能根本不在文件里。用它打头比不记得更糟。"""

    assert lead_with_remembered(["状态", "城市"], ["销售员"]) == ["状态", "城市"]


def test_memory_reorders_the_real_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_MEMORY_PATH", str(tmp_path / "memory.json"))

    before = TaskSpec.model_validate(
        understand_goal("统计一下", _tables())["task_spec"]
    ).missing_slots[0]
    assert before.options[0] != "城市"

    record_clarification_choice("analysis_target", "城市")
    record_clarification_choice("analysis_target", "城市")

    after = TaskSpec.model_validate(
        understand_goal("统计一下", _tables())["task_spec"]
    ).missing_slots[0]

    assert after.options[0] == "城市", "选过两次的维度没有排到前面"
    assert set(after.options) == set(before.options), "记忆只改顺序，不该增删候选"


def test_the_question_is_still_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记住不等于替用户决定。上个月按城市，不代表这个月也是。"""

    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_MEMORY_PATH", str(tmp_path / "memory.json"))
    record_clarification_choice("analysis_target", "城市")

    task = TaskSpec.model_validate(understand_goal("统计一下", _tables())["task_spec"])

    assert any(slot.slot_id == "analysis_target" for slot in task.missing_slots)
