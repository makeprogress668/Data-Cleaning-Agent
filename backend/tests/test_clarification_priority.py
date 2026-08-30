"""真不确定才拦，有标准答案就照做。

用户要的是「说一句话，拿到能直接用的成品」。但四行数据的一次上传被问了两轮，两个问题
都有标准答案：括号里的金额是会计写法的负数、合计行不进按维度的汇总。这不是 human-in-
the-loop，是把表单塞给了一个只想要结果的人。

反过来，「整理订单，能用的留下」里的「能用」没有标准答案。不问就交付全部行 —— 用户
要求筛选，拿到的却是原样，这是更严重的答非所问。

区分依据不是「谁提的问题」，而是「没有这个答案还能不能交付用户要的东西」。这是语义
判断，由理解层给出 blocking 标记；确定性层只负责尊重它。
"""

from __future__ import annotations

import json

import pandas as pd

from data_agent.agent.goal_understanding import understand_goal
from data_agent.agent.planner import (
    collect_clarification_questions,
    collect_conditional_clarification,
    has_blocking_questions,
)
from data_agent.schemas.task import TaskSpec


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "订单明细": pd.DataFrame(
            {"订单号": ["O1", "O2"], "状态": ["已付款", "已取消"], "金额": [100, 200]}
        )
    }


def _planner(questions):
    def planner(_messages):
        return json.dumps(
            {"suggested_base_table": "订单明细", "clarification_questions": questions},
            ensure_ascii=False,
        )

    return planner


def _plan(goal: str, questions):
    return understand_goal(goal, _tables(), llm_planner=_planner(questions))


def test_a_question_with_a_standard_answer_does_not_stop_the_run() -> None:
    plan = _plan(
        "按状态统计金额合计",
        [{"question": "括号中的金额将按负数计算。", "blocking": False}],
    )

    assert not has_blocking_questions(collect_conditional_clarification(plan)), (
        "有标准答案的问题拦住了任务"
    )


def test_a_question_without_which_nothing_can_run_does_stop_it() -> None:
    plan = _plan(
        "整理订单，能用的留下",
        [{"question": "「能用的」按什么状态判断？", "blocking": True}],
    )

    questions = collect_conditional_clarification(plan)
    assert has_blocking_questions(questions), "缺少筛选条件却直接跑完，交付了全部行"
    assert "能用" in questions[0]["question"]


def test_an_unmarked_question_stays_blocking() -> None:
    """模型没表态时，问比不问安全。"""

    plan = _plan("整理订单，能用的留下", ["「能用的」按什么状态判断？"])

    assert has_blocking_questions(collect_conditional_clarification(plan))


def test_a_non_blocking_question_still_reaches_the_user() -> None:
    """不拦不等于不说 —— 它作为本次采用的假设留在结果里。"""

    plan = _plan(
        "按状态统计金额合计",
        [{"question": "括号中的金额将按负数计算。", "blocking": False}],
    )
    task = TaskSpec.model_validate(plan["task_spec"])

    assert any("括号" in slot.question for slot in task.missing_slots), "假设被丢掉了"


def test_public_question_priority_matches_the_task_spec() -> None:
    """A non-blocking assumption must not be presented as a high-priority question."""

    plan = _plan(
        "按状态统计金额合计",
        [{"question": "括号中的金额将按负数计算。", "blocking": False}],
    )

    questions = collect_clarification_questions(
        goal="按状态统计金额合计",
        goal_plan=plan,
        job_config={},
        profile_sheets={},
    )

    assert len(questions) == 1
    assert questions[0]["priority"] == "中"
    assert not has_blocking_questions(questions)


def test_missing_value_handling_choice_uses_the_safe_non_blocking_default() -> None:
    goal = "订单明细中金额缺失的算异常，请清洗并列出原因"

    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "订单明细",
                "target_fields": [{"table": "订单明细", "field": "金额"}],
                "capabilities": {
                    "needs_exception_review": True,
                    "wants_inplace": True,
                },
                "capability_evidence": {
                    "needs_exception_review": "列出原因",
                    "wants_inplace": "清洗",
                },
                "clarification_questions": [
                    {
                        "question": (
                            "金额缺失记录是删除、保留并标记，还是尝试补全？"
                        ),
                        "blocking": True,
                    }
                ],
            },
            ensure_ascii=False,
        )

    plan = understand_goal(goal, _tables(), llm_planner=planner)
    slot = TaskSpec.model_validate(plan["task_spec"]).missing_slots[0]

    assert slot.priority == "medium"
    assert not has_blocking_questions(collect_conditional_clarification(plan))


def test_a_structural_gap_blocks_regardless_of_the_model() -> None:
    """同结构的两张表要不要合并，是确定性层的判断，不依赖模型表态。

    不合并就只用其中一张 —— 交付的数字会只覆盖一部分记录，而用户看不出来。
    """

    tables = {
        "销售_1月": pd.DataFrame({"订单号": ["1月-1"], "金额": [100]}),
        "销售_2月": pd.DataFrame({"订单号": ["2月-1"], "金额": [200]}),
    }
    plan = understand_goal("统计金额合计", tables)
    task = TaskSpec.model_validate(plan["task_spec"])

    union_slots = [slot for slot in task.missing_slots if "结构完全相同" in slot.question]
    assert union_slots, "只用了其中一张表，却没有拦下来问"
    assert union_slots[0].priority == "high"
    assert has_blocking_questions(collect_conditional_clarification(plan))
