"""Tests for the LLM-first goal understanding layer (Phase A).

Verifies: (a) with no LLM the output equals the deterministic interpret_goal plus
the fallback envelope; (b) a valid overlay reshapes focus / base table /
capabilities; (c) an overlay referencing unknown tables/fields is rejected and the
deterministic values are kept; (d) any planner exception falls back safely.
"""

import json

import pandas as pd

from data_agent.agent.goal_understanding import understand_goal
from data_agent.planning.goal_interpreter import derive_capabilities, interpret_goal


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "orders": pd.DataFrame(
            {"order_id": ["O1", "O2"], "customer_id": ["C1", "C2"], "amount": [100, 200]}
        ),
        "customers": pd.DataFrame(
            {"customer_id": ["C1", "C2"], "customer_name": ["Acme", "Globex"]}
        ),
    }


def test_no_llm_returns_deterministic_superset() -> None:
    goal = "清洗订单数据"
    tables = _tables()
    result = understand_goal(goal, tables, llm_planner=None)

    baseline = interpret_goal(goal, tables)
    # Every deterministic key is preserved unchanged.
    for key, value in baseline.items():
        assert result[key] == value
    # Plus the fallback envelope.
    assert result["understanding_source"] == "deterministic"
    assert result["capabilities"] == derive_capabilities(goal, tables)
    assert result["clarification_questions"] == []
    assert result["assumptions"] == []


def test_valid_overlay_reshapes_intent_and_capabilities() -> None:
    goal = "把客户信息合并进订单并做统计分析"
    tables = _tables()

    def _planner(_messages):
        return json.dumps(
            {
                "focus": ["多表匹配", "统计分析"],
                "business_views": ["analysis_dashboard_view"],
                "requested_outputs": ["统计报表"],
                "suggested_base_table": "orders",
                "capabilities": {
                    "needs_lookup": True,
                    "needs_analysis": True,
                    "needs_charts": False,
                    "needs_exception_review": False,
                    "wants_inplace": False,
                    "wants_annotation": True,
                },
                "target_fields": [{"table": "customers", "field": "customer_name"}],
                "clarification_questions": ["按 customer_id 关联，对吗？"],
                "assumptions": ["orders 为主表"],
            }
        )

    result = understand_goal(goal, tables, llm_planner=_planner)

    assert result["understanding_source"] == "llm"
    assert result["focus"] == ["多表匹配", "统计分析"]
    assert result["suggested_base_table"] == "orders"
    assert result["capabilities"]["needs_lookup"] is True
    assert result["capabilities"]["needs_analysis"] is True
    # Annotation was not requested. The model cannot add a user-visible field merely
    # because it seems useful.
    assert result["capabilities"]["wants_annotation"] is False
    assert {"table": "customers", "field": "customer_name"} in result["target_fields"]
    assert result["clarification_questions"] == ["按 customer_id 关联，对吗？"]
    # The deterministic evidence layer is preserved.
    assert "field_matches" in result and "table_matches" in result


def test_overlay_with_unknown_schema_and_unsupported_capability_is_rejected() -> None:
    goal = "清洗订单数据"
    tables = _tables()

    def _planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "ghost_table",  # not a real table
                "target_fields": [
                    {"table": "orders", "field": "ghost_field"},  # not a real column
                    {"table": "orders", "field": "amount"},  # real -> kept
                ],
                "capabilities": {"needs_analysis": True},
            }
        )

    result = understand_goal(goal, tables, llm_planner=_planner)

    # Unknown base table is dropped -> deterministic value kept.
    assert result["suggested_base_table"] == interpret_goal(goal, tables)["suggested_base_table"]
    # Only the real column survives.
    assert result["target_fields"] == [{"table": "orders", "field": "amount"}]
    # A schema-valid boolean is still not authority to add an unrequested output.
    assert result["capabilities"]["needs_analysis"] is False


def test_planner_exception_falls_back_and_says_so() -> None:
    """A reachable-but-broken model is not the same as no model configured.

    Both used to report "deterministic", so a 30-second timeout looked exactly like a
    deliberate rules-only deployment and nothing explained why today's result differed
    from yesterday's.
    """

    goal = "清洗订单数据"
    tables = _tables()

    def _broken_planner(_messages):
        raise RuntimeError("LLM transport blew up")

    result = understand_goal(goal, tables, llm_planner=_broken_planner)
    assert result["understanding_source"] == "deterministic_fallback"
    assert "LLM transport blew up" in result["understanding_fallback_reason"]
    assert result["capabilities"] == derive_capabilities(goal, tables)


def test_invalid_json_falls_back_and_says_so() -> None:
    goal = "清洗订单数据"
    tables = _tables()
    result = understand_goal(goal, tables, llm_planner=lambda _m: "not json at all")
    assert result["understanding_source"] == "deterministic_fallback"
    assert result["understanding_fallback_reason"]


def test_empty_goal_short_circuits_to_deterministic() -> None:
    tables = _tables()
    called = {"n": 0}

    def _planner(_messages):
        called["n"] += 1
        return "{}"

    result = understand_goal("", tables, llm_planner=_planner)
    assert result["understanding_source"] == "deterministic"
    assert called["n"] == 0  # planner never invoked for an empty goal


# --- understanding visibility signals (closing gap #1: silent mislocation) -----


def test_understand_goal_marks_high_confidence_when_fields_located() -> None:
    """A goal that grounds onto concrete fields is graded high, not a fallback."""
    goal = "把客户信息合并进订单"
    tables = _tables()
    result = understand_goal(goal, tables, llm_planner=None)

    assert result["understanding_confidence"] == "high"
    assert result["is_generic_fallback"] is False


def test_understand_goal_marks_generic_fallback_on_unfamiliar_domain() -> None:
    """An out-of-domain goal that matches no table is flagged low + generic fallback
    so the console can warn instead of silently running generic cleaning."""
    goal = "处理外星舰队补给物资"
    tables = _tables()
    result = understand_goal(goal, tables, llm_planner=None)

    assert result["understanding_confidence"] == "low"
    assert result["is_generic_fallback"] is True


def test_understand_goal_confidence_survives_llm_overlay() -> None:
    """When the LLM validates a real base table + target fields, the plan is graded
    high-confidence and not a fallback, with source marked llm."""
    goal = "把客户信息合并进订单"
    tables = _tables()

    def _planner(_messages):
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [{"table": "customers", "field": "customer_name"}],
            }
        )

    result = understand_goal(goal, tables, llm_planner=_planner)

    assert result["understanding_source"] == "llm"
    assert result["understanding_confidence"] == "high"
    assert result["is_generic_fallback"] is False


def test_string_false_does_not_enable_capabilities() -> None:
    goal = "清洗订单数据"
    baseline = derive_capabilities(goal, _tables())

    def _planner(_messages):
        return json.dumps(
            {
                "capabilities": {
                    "needs_analysis": "false",
                    "wants_annotation": "false",
                }
            }
        )

    result = understand_goal(goal, _tables(), llm_planner=_planner)

    assert result["capabilities"]["needs_analysis"] == baseline["needs_analysis"]
    assert result["capabilities"]["wants_annotation"] == baseline["wants_annotation"]


def test_semantic_aggregation_compiles_into_task_spec() -> None:
    goal = "分析 orders 的 customer_id 金额走势，生成折线图、变更清单和简短报告"

    def _planner(_messages):
        return json.dumps(
            {
                "capabilities": {
                    "needs_analysis": True,
                    "needs_charts": True,
                    "needs_change_manifest": True,
                    "needs_report": True,
                },
                "capability_evidence": {
                    "needs_analysis": "分析 orders 的 customer_id 金额走势",
                    "needs_charts": "生成折线图",
                    "needs_change_manifest": "变更清单",
                    "needs_report": "简短报告",
                },
                "target_fields": [
                    {"table": "orders", "field": "customer_id"},
                    {"table": "orders", "field": "amount"},
                ],
                "aggregation": {
                    "group_by": ["customer_id"],
                    "metrics": [
                        {
                            "column": "amount",
                            "agg": "sum",
                            "output_name": "amount_sum",
                        }
                    ],
                },
                "chart_type": "line",
            }
        )

    result = understand_goal(goal, _tables(), llm_planner=_planner)

    assert result["task_spec"]["aggregation"] == {
        "group_by": ["customer_id"],
        "column_field": "",
        "metrics": [
            {"column": "amount", "agg": "sum", "output_name": "amount_sum"}
        ],
        "source": "llm",
    }
    assert result["task_spec"]["output_intent"]["needs_change_manifest"] is True
    assert result["task_spec"]["output_intent"]["needs_report"] is True
    assert result["task_spec"]["output_intent"]["chart_type"] == "line"


def test_annotation_requires_verbatim_goal_evidence() -> None:
    goal = "把 orders 的异常记录标出来"

    def _planner(_messages):
        return json.dumps(
            {
                "capabilities": {"wants_annotation": True},
                "capability_evidence": {
                    "wants_annotation": "把 orders 的异常记录标出来"
                },
            }
        )

    result = understand_goal(goal, _tables(), llm_planner=_planner)
    assert result["capabilities"]["wants_annotation"] is True
