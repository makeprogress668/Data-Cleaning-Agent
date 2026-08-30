import json

import pandas as pd
import pytest
from pydantic import ValidationError

from data_agent.agent.goal_understanding import understand_goal
from data_agent.planning.goal_interpreter import apply_task_spec_operations
from data_agent.schemas.task import TaskSpec


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "orders": pd.DataFrame(
            {
                "order_id": ["O1", "O2"],
                "customer_id": ["C1", "C2"],
                "amount": [100, 200],
            }
        ),
        "customers": pd.DataFrame(
            {
                "customer_id": ["C1", "C2"],
                "customer_name": ["Acme", "Globex"],
            }
        ),
    }


def test_clear_goal_produces_valid_task_spec() -> None:
    result = understand_goal("清洗订单并按 customer_id 合并客户名称", _tables())
    task = TaskSpec.model_validate(result["task_spec"])

    assert task.objective == "清洗订单并按 customer_id 合并客户名称"
    assert task.primary_entity == "orders"
    assert {"clean", "lookup", "export"}.issubset(task.actions)
    assert "orders" in task.target_tables
    assert task.confidence in {"high", "medium"}
    assert not any(slot.kind == "primary_table" for slot in task.missing_slots)


def test_ambiguous_goal_creates_primary_table_missing_slot() -> None:
    result = understand_goal("把两个表关联起来处理一下", _tables())
    task = TaskSpec.model_validate(result["task_spec"])

    slot = next(item for item in task.missing_slots if item.kind == "primary_table")
    assert slot.slot_id == "primary_table"
    assert set(slot.options) == {"orders", "customers"}
    assert task.confidence == "low"


def test_llm_questions_become_typed_missing_slots() -> None:
    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [{"table": "orders", "field": "amount"}],
                "clarification_questions": ["“能用的记录”具体按什么标准保留？"],
            },
            ensure_ascii=False,
        )

    result = understand_goal("整理订单，能用的留下", _tables(), llm_planner=planner)
    task = TaskSpec.model_validate(result["task_spec"])

    assert task.primary_entity == "orders"
    assert task.target_fields[0].field == "amount"
    assert len(task.missing_slots) == 1
    assert task.missing_slots[0].kind == "retention_rule"
    assert task.missing_slots[0].slot_id.startswith("slot_")


def test_task_spec_output_intent_reuses_validated_capabilities() -> None:
    result = understand_goal("统计订单金额并生成趋势图", _tables())
    task = TaskSpec.model_validate(result["task_spec"])

    assert "analyze" in task.actions
    assert "chart" in task.actions
    assert task.output_intent["needs_charts"] is True


def test_task_spec_reasserts_execution_gates_after_llm_job_override() -> None:
    goal = "请清洗 orders，只保留 amount 非空记录，把缺失记录单独输出"

    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [{"table": "orders", "field": "amount"}],
                "capabilities": {
                    "needs_exception_review": True,
                    "wants_inplace": True,
                    "wants_row_review": True,
                    "needs_report": True,
                },
                "capability_evidence": {
                    "needs_exception_review": "把缺失记录单独输出",
                    "wants_inplace": "清洗 orders",
                    "wants_row_review": "只保留 amount 非空记录",
                },
                "clarification_questions": [
                    {"question": "还需要检查其他字段吗？", "blocking": True}
                ],
            },
            ensure_ascii=False,
        )

    result = understand_goal(goal, _tables(), llm_planner=planner)
    task = TaskSpec.model_validate(result["task_spec"])
    compiled = apply_task_spec_operations(
        {
            "dirty_data": {
                "enabled": False,
                "auto_fix_safe_issues": False,
                "mark_uncertain_for_review": False,
            },
            "exception_policy": {
                "exclude_critical_from_final": False,
                "require_review_for_unmatched_lookup": False,
            },
            "analysis": {"enabled": True},
            "charts": {"enabled": True},
        },
        result,
    )

    assert task.output_intent["withhold_flagged_rows"] is True
    assert task.output_intent["needs_report"] is False
    assert task.missing_slots[0].priority == "medium"
    assert compiled["dirty_data"] == {
        "enabled": True,
        "auto_fix_safe_issues": True,
        "mark_uncertain_for_review": True,
    }
    assert compiled["exception_policy"]["exclude_critical_from_final"] is True
    assert compiled["exception_policy"]["require_review_for_unmatched_lookup"] is True
    assert compiled["analysis"]["enabled"] is False
    assert compiled["charts"]["enabled"] is False


def test_review_list_alone_does_not_withhold_rows() -> None:
    result = understand_goal(
        "检查 orders，把 amount 为空的记录列入复核清单",
        _tables(),
    )
    task = TaskSpec.model_validate(result["task_spec"])

    assert "review" in task.actions
    assert task.output_intent["needs_review"] is True
    assert task.output_intent["withhold_flagged_rows"] is False


def test_calculation_for_an_aggregate_does_not_claim_a_derived_column() -> None:
    result = understand_goal(
        "按 customer_id 统计 amount 合计，amount 只用于计算，明细保持原样",
        _tables(),
    )
    task = TaskSpec.model_validate(result["task_spec"])

    assert "analyze" in task.actions
    assert "derive" not in task.actions
    assert task.derivations == []


def test_coordinated_negation_blocks_mutation_and_lookup_key_is_not_a_dimension() -> None:
    tables = {
        "orders": pd.DataFrame(
            {
                "订单号": ["O1", "O2", "O3"],
                "客户编号": ["C1", "C2", "C1"],
                "金额": ["1,234", "(567)", "2,000"],
            }
        ),
        "customers": pd.DataFrame(
            {
                "客户编号": ["C1", "C2"],
                "客户名称": ["甲公司", "乙公司"],
                "区域": ["华北", "华东"],
            }
        ),
    }
    goal = (
        "以 orders 为主表，按客户编号将 customers 的区域关联到订单；"
        "按区域统计金额合计和订单数量。不要删除、去重、填空或改写原始数据。"
    )

    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "capabilities": {
                    "needs_lookup": True,
                    "needs_analysis": True,
                    "wants_inplace": True,
                },
                "capability_evidence": {
                    "wants_inplace": "不要删除、去重、填空或改写原始数据"
                },
                "deduplication": [
                    {"fields": ["金额"], "strategy": "first", "evidence": "去重"}
                ],
            },
            ensure_ascii=False,
        )

    result = understand_goal(goal, tables, llm_planner=planner)
    task = TaskSpec.model_validate(result["task_spec"])

    assert "clean" not in task.actions
    assert "deduplicate" not in task.actions
    assert task.deduplication == []
    assert task.aggregation["group_by"] == ["区域"]


def test_explicit_filter_and_deduplication_are_structured_without_clarification() -> None:
    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [
                    {"table": "orders", "field": "order_id"},
                    {"table": "orders", "field": "amount"},
                ],
                "clarification_questions": ["保留第一条是否按原始行顺序？"],
            },
            ensure_ascii=False,
        )

    result = understand_goal(
        "按 order_id 去重，保留第一条，并过滤 amount < 0 的记录",
        _tables(),
        llm_planner=planner,
    )
    task = TaskSpec.model_validate(result["task_spec"])

    assert {"filter", "deduplicate"}.issubset(task.actions)
    # Rules the user actually wrote are tagged as coming from the goal text, so the
    # confirmation page can distinguish them from model-proposed ones.
    assert task.filters == [
        {
            "field": "amount",
            "op": "lt",
            "value": 0,
            "mode": "exclude",
            "source": "goal_text",
            "authorization": "stated",
        }
    ]
    assert task.deduplication == [
        {
            "fields": ["order_id"],
            "strategy": "first",
            "source": "goal_text",
            "authorization": "stated",
        }
    ]
    assert task.missing_slots == []


def test_negated_filter_and_deduplication_are_not_planned() -> None:
    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [{"table": "orders", "field": "order_id"}],
                "filters": [
                    {"field": "amount", "op": "gt", "value": 0, "mode": "keep"}
                ],
                "deduplication": [
                    {"fields": ["order_id"], "strategy": "first"}
                ],
            }
        )

    result = understand_goal(
        "清洗订单，保留全部记录，不去重、不筛选，字段原样输出",
        _tables(),
        llm_planner=planner,
    )
    task = TaskSpec.model_validate(result["task_spec"])

    assert "filter" not in task.actions
    assert "deduplicate" not in task.actions
    assert task.filters == []
    assert task.deduplication == []
    assert task.missing_slots == []


def test_llm_may_express_a_filter_the_regex_cannot_parse_but_it_is_tagged() -> None:
    """Semantic phrasing must still reach the plan — flagged, not silently dropped.

    "把 C2 这个客户的订单去掉" contains no `field op value` pattern, so the
    deterministic parser finds nothing. The overlay used to be discarded unless it
    restated a parsed rule, which meant the user's explicit instruction vanished
    without a warning or a clarification question. It is now kept, tagged, and marked
    ``authorization="stated"`` — the goal itself asks for the removal, so it runs
    without a second question.
    """

    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "filters": [
                    {
                        "field": "customer_id",
                        "op": "not_equals",
                        "value": "C2",
                        "mode": "keep",
                        "evidence": "把 C2 这个客户的订单去掉",
                    }
                ],
            },
            ensure_ascii=False,
        )

    result = understand_goal("把 C2 这个客户的订单去掉", _tables(), llm_planner=planner)
    task = TaskSpec.model_validate(result["task_spec"])

    assert task.filters == [
        {
            "field": "customer_id",
            "op": "not_equals",
            "value": "C2",
            "mode": "keep",
            "evidence": "把 C2 这个客户的订单去掉",
            "source": "llm",
            "authorization": "stated",
        }
    ]
    assert "filter" in task.actions


def test_llm_cannot_inject_filter_missing_from_user_goal() -> None:
    def planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "filters": [
                    {"field": "amount", "op": "in", "mode": "keep"}
                ],
            }
        )

    result = understand_goal("清洗订单并输出结果", _tables(), llm_planner=planner)
    task = TaskSpec.model_validate(result["task_spec"])

    assert task.filters == []
    assert "filter" not in task.actions


def test_task_spec_rejects_unknown_nested_rule_fields() -> None:
    with pytest.raises(ValidationError, match="unexpected_rule_key"):
        TaskSpec.model_validate(
            {
                "objective": "过滤负金额",
                "filters": [
                    {
                        "field": "amount",
                        "op": "lt",
                        "value": 0,
                        "mode": "exclude",
                        "unexpected_rule_key": True,
                    }
                ],
            }
        )


def test_task_spec_rejects_incomplete_structured_operations() -> None:
    with pytest.raises(ValidationError, match="comparison filter requires value"):
        TaskSpec.model_validate(
            {
                "objective": "过滤负金额",
                "filters": [{"field": "amount", "op": "lt", "mode": "exclude"}],
            }
        )

    with pytest.raises(ValidationError, match="split derivation is incomplete"):
        TaskSpec.model_validate(
            {
                "objective": "拆分地址",
                "derivations": [
                    {"kind": "split", "source": "地址", "outputs": ["省", "市"]}
                ],
            }
        )


def test_task_spec_serializes_typed_rules_as_plain_json_objects() -> None:
    task = TaskSpec.model_validate(
        {
            "objective": "过滤负金额",
            "actions": ["filter", "export"],
            "filters": [
                {
                    "field": "amount",
                    "op": "lt",
                    "value": 0,
                    "mode": "exclude",
                    "source": "goal_text",
                    "authorization": "stated",
                }
            ],
        }
    )

    payload = task.model_dump(mode="json")
    assert isinstance(payload["filters"][0], dict)
    assert payload["filters"][0]["authorization"] == "stated"


def test_task_spec_v2_materializes_discriminated_operations() -> None:
    task = TaskSpec(
        objective="删除退款订单并按区域统计金额",
        actions=["filter", "analyze", "export"],
        filters=[
            {
                "field": "状态",
                "op": "equals",
                "mode": "exclude",
                "value": "退款",
            }
        ],
        aggregation={
            "group_by": ["区域"],
            "metrics": [
                {"column": "金额", "agg": "sum", "output_name": "金额合计"}
            ],
        },
    )

    payload = task.model_dump(mode="json")
    assert payload["version"] == 2
    assert [item["kind"] for item in payload["operations"]] == [
        "filter",
        "analyze",
        "export",
    ]
    assert payload["operations"][0]["filters"] == payload["filters"]
    assert payload["operations"][1]["aggregation"] == payload["aggregation"]


def test_task_spec_rejects_divergent_legacy_and_typed_operations() -> None:
    with pytest.raises(ValidationError, match="diverge"):
        TaskSpec(
            objective="删除退款订单",
            actions=["filter"],
            filters=[
                {
                    "field": "状态",
                    "op": "equals",
                    "mode": "exclude",
                    "value": "退款",
                }
            ],
            operations=[
                {
                    "kind": "filter",
                    "filters": [
                        {
                            "field": "状态",
                            "op": "equals",
                            "mode": "exclude",
                            "value": "已取消",
                        }
                    ],
                }
            ],
        )
