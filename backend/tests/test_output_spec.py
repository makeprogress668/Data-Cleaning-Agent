import pytest
from pydantic import ValidationError

from data_agent.schemas import OutputSpec, TaskSpec, build_output_spec


def test_plain_cleaning_defaults_to_one_table_only() -> None:
    spec = build_output_spec(
        TaskSpec(
            objective="清洗订单数据并输出规范化结果",
            actions=["clean", "export"],
        )
    )

    # 清洗会改动原数据，但用户没要求看改动清单，就不多交一张表。
    # 每多一张 sheet，都在结果和读它的人之间多放一层东西。
    assert spec.required_sheet_names == {"处理结果"}
    assert spec.secondary_artifacts == []
    assert spec.include_change_manifest is False
    assert spec.charts == []


def test_asking_what_changed_declares_the_manifest() -> None:
    """要求了就出，而且是可选产物：真的改了才写出来。"""

    spec = build_output_spec(
        TaskSpec(
            objective="清洗订单数据，并列出本次改了什么",
            actions=["clean", "export"],
            output_intent={"needs_change_manifest": True},
        )
    )

    assert [artifact.name for artifact in spec.secondary_artifacts] == ["本次改动"]
    assert spec.secondary_artifacts[0].optional is True
    assert spec.report.enabled is False
    assert spec.include_audit is False


def test_goal_declares_only_requested_review_chart_report_and_audit() -> None:
    spec = build_output_spec(
        TaskSpec(
            objective="清洗订单，列出异常，并生成一张趋势图和简要报告",
            actions=["clean", "review", "chart", "export"],
            output_intent={"needs_review": True, "needs_charts": True},
        ),
        include_audit=True,
    )

    assert spec.required_sheet_names == {"处理结果", "问题说明"}
    assert [chart.chart_id for chart in spec.charts] == ["time_trend"]
    assert spec.charts[0].business_question.startswith("清洗订单")
    assert spec.charts[0].dimension == "周期"
    assert spec.charts[0].metric == "记录数"
    assert spec.report.enabled is True
    assert spec.report.formats == ["markdown", "html"]
    assert spec.include_audit is True


def test_semantic_output_intent_selects_report_and_chart_type() -> None:
    """Validated model intent covers phrasings outside the fallback word list."""

    spec = build_output_spec(
        TaskSpec(
            objective="说明 orders 的 month 变化并配一张线形图",
            actions=["analyze", "chart", "export"],
            output_intent={
                "needs_charts": True,
                "chart_type": "line",
                "needs_report": True,
            },
        )
    )

    assert spec.report.enabled is True
    assert [chart.chart_type for chart in spec.charts] == ["line"]
    assert [chart.chart_id for chart in spec.charts] == ["time_trend"]


def test_output_spec_rejects_duplicate_artifacts_and_chart_metrics() -> None:
    with pytest.raises(ValidationError, match="artifact names must be unique"):
        OutputSpec.model_validate(
            {
                "primary_artifact": {"name": "处理结果"},
                "secondary_artifacts": [{"name": "处理结果"}],
            }
        )

    chart = {
        "chart_id": "trend_a",
        "business_question": "销售额趋势如何？",
        "dimension": "月份",
        "metric": "销售额",
        "chart_type": "line",
    }
    with pytest.raises(ValidationError, match="duplicate chart metrics"):
        OutputSpec.model_validate(
            {
                "charts": [
                    chart,
                    {**chart, "chart_id": "trend_b"},
                ]
            }
        )
