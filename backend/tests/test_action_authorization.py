"""授权来自用户的原话，不来自我们对措辞的猜测。

以前判断「用户是不是要求删行」靠一张动词表。表里漏了一个说法，后果不是「我没听懂」，
而是掉进错误的分支：早期是没要求也删，后来改成漏词就整条丢弃、一声不吭。两种失误方向
都不对，而动词表永远补不完 —— 每个行业都有自己的说法。

现在的规则：模型可以自由理解（「把重复的记录清理掉」不需要命中任何关键词），但它必须
**引用用户目标里的原话**。验证只有一步、确定性、可审计：这段引用是不是真的出自目标。

- 引得出来 → stated → 直接执行，不再追问一遍
- 引不出来 → inferred → 丢弃，转成澄清问题

于是漏判的代价是多问一句，而不是删掉用户没让删的数据，也不是假装没听见。
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from data_agent.agent.goal_understanding import understand_goal
from data_agent.capabilities.planning import build_execution_plan, validate_execution_plan
from data_agent.planning.goal_interpreter import apply_goal_to_job_config
from data_agent.planning.provenance import (
    normalize_quote,
    operation_is_negated,
    verify_evidence,
)
from data_agent.schemas.task import TaskSpec


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "orders": pd.DataFrame(
            {
                "订单号": ["O1", "O2", "O2", "O3"],
                "客户编号": ["C1", "C2", "C2", "C3"],
                "金额": [100, 200, 200, -5],
            }
        )
    }


def _planner(payload: dict[str, Any]):
    def planner(_messages):
        return json.dumps({"suggested_base_table": "orders", **payload}, ensure_ascii=False)

    return planner


def _spec(goal: str, payload: dict[str, Any]) -> TaskSpec:
    result = understand_goal(goal, _tables(), llm_planner=_planner(payload))
    return TaskSpec.model_validate(result["task_spec"])


# --- 引用验证本身 ------------------------------------------------------------


def test_a_quote_from_the_goal_is_authorization() -> None:
    assert verify_evidence("把重复的记录清理掉", "帮我把重复的记录清理掉，谢谢")


def test_punctuation_and_spacing_differences_do_not_break_a_real_quote() -> None:
    """模型重敲一遍标点是常事，不该因此判成伪造。"""

    assert verify_evidence("把重复的记录，清理掉", "帮我把重复的记录清理掉")
    assert verify_evidence("把重复的记录清理掉 ", "帮我把重复的记录清理掉")


def test_an_invented_quote_is_not_authorization() -> None:
    assert not verify_evidence("把负数订单删掉", "统计每个客户的订单金额")


def test_a_field_name_is_not_an_instruction() -> None:
    """引用「订单」不能证明用户要删订单 —— 那只是表名的一部分。"""

    assert not verify_evidence("订单", "统计订单明细的金额", names=["订单明细", "金额"])


def test_a_quote_too_short_to_mean_anything_is_rejected() -> None:
    assert not verify_evidence("的", "把重复的记录清理掉")
    assert not verify_evidence("", "把重复的记录清理掉")


def test_normalize_quote_keeps_content_and_drops_separators() -> None:
    assert normalize_quote(" 把重复的，记录清理掉。") == "把重复的记录清理掉"


def test_one_negation_marker_applies_to_a_coordinated_operation_list() -> None:
    goal = "不要删除、去重、填空或改写原始数据"

    assert operation_is_negated(goal, ["去重"])
    assert operation_is_negated(goal, ["填空", "去重"])


def test_an_affirmative_operation_after_a_contrast_is_not_negated() -> None:
    goal = "不要填空，但要按订单号去重"

    assert not operation_is_negated(goal, ["填空", "去重"])


# --- 端到端：理解 → 授权 → 计划 ----------------------------------------------


def test_a_phrasing_outside_the_verb_table_still_reaches_the_plan() -> None:
    """「清理掉」不在动词表里。模型懂，并且引得出原话，就该执行。"""

    task = _spec(
        "把重复的记录清理掉",
        {"deduplication": [{"fields": ["订单号"], "strategy": "first",
                            "evidence": "把重复的记录清理掉"}]},
    )

    assert task.deduplication, "用户明确要求去重，却什么都没发生"
    assert task.deduplication[0]["authorization"] == "stated"


def test_a_rule_nobody_asked_for_is_dropped_without_a_word() -> None:
    """目标只是统计，模型顺手提议删负数行，还没给出任何引用。

    不执行，也不发问。「用户没有要求的话，也不需要自动上报问题」—— 拿一个用户根本没
    提过的删除去问他，本身就是在推销一个他没要的操作。
    """

    task = _spec(
        "统计每个客户的订单金额",
        {"filters": [{"field": "金额", "op": "lt", "value": 0, "mode": "exclude"}]},
    )

    assert task.filters == [], "用户没要求删行，却擅自过滤"
    # 关于「删除」一个字都不该问。用户提过的统计另说 —— 那是他自己要求的，
    # 问「按什么统计」是在满足他，不是在推销一个他没要的操作。
    removal_questions = [
        slot.question for slot in task.missing_slots if "删除" in slot.question
    ]
    assert removal_questions == [], f"为用户没提过的删除平白问了一句：{removal_questions}"


def test_a_paraphrased_quote_stops_the_deletion_but_raises_a_question() -> None:
    """模型认定用户要求了删除，却是转述而不是照抄 —— 这才是可能漏掉指令的那种情况。

    照样不删（授权不成立），但必须问一句，否则用户真的提了要求时得到的是沉默。
    """

    task = _spec(
        "把作废的订单处理一下",
        {
            "filters": [
                {
                    "field": "金额",
                    "op": "lt",
                    "value": 0,
                    "mode": "exclude",
                    "evidence": "删除作废的订单记录",  # 目标里没有这句话
                }
            ]
        },
    )

    assert task.filters == [], "引用对不上原文，仍然不能删"
    questions = [slot.question for slot in task.missing_slots]
    assert any("金额" in question for question in questions), "丢弃了却一声不吭"
    assert any("本次不会删除任何记录" in question for question in questions)


def test_naming_a_set_of_rows_is_not_asking_to_delete_the_rest() -> None:
    """「金额超过5000的订单」描述的是一批记录，不是一条删除指令。"""

    task = _spec(
        "看看金额超过5000的订单",
        {"filters": [{"field": "金额", "op": "gt", "value": 5000, "mode": "keep",
                      "evidence": "金额超过5000的订单"}]},
    )

    assert task.filters == [] or all(
        item.get("authorization") == "stated" for item in task.filters
    ), "过滤规则必须要么不存在，要么有授权"


@pytest.mark.parametrize(
    "goal",
    [
        "把金额低于0的删掉",
        "清洗订单，过滤 金额 < 0 的记录",
    ],
)
def test_an_explicit_low_impact_instruction_needs_no_second_question(goal: str) -> None:
    """Quoted authorization stays on the direct path when impact is low and known."""

    tables = _tables()
    result = understand_goal(goal, tables)
    job_config = apply_goal_to_job_config({"formulas": []}, goal, tables, result)
    plan = build_execution_plan(
        job_config,
        impact_preview={
            "estimate_available": True,
            "input_rows": 100,
            "estimated_removed_rows": 1,
        },
    )

    destructive = [
        step
        for step in plan.steps
        if step.capability_id in {"filter_rows", "deduplicate"}
    ]
    assert destructive, f"「{goal}」应当编译出删行步骤"
    assert all(step.authorization == "stated" for step in destructive)
    assert not plan.requires_confirmation, "低影响的明确指令不应重复确认"


def test_an_unauthorized_step_cannot_forge_its_way_past_the_gate() -> None:
    """把 requires_confirmation 改成 False 不足以绕过；授权是一起被校验的。"""

    goal = "把金额低于0的删掉"
    tables = _tables()
    result = understand_goal(goal, tables)
    plan = build_execution_plan(
        apply_goal_to_job_config({"formulas": []}, goal, tables, result),
        impact_preview={
            "estimate_available": True,
            "input_rows": 100,
            "estimated_removed_rows": 1,
        },
    )
    payload = plan.model_dump(mode="json")
    for step in payload["steps"]:
        if step["capability_id"] == "filter_rows":
            step["authorization"] = "inferred"

    with pytest.raises(ValueError, match="confirmation policy"):
        validate_execution_plan(payload)
