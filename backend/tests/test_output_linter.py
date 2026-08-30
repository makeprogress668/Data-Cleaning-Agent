import pytest

from data_agent.schemas import TaskSpec, build_output_spec
from data_agent.tools import OutputContractError, lint_output


def _spec():
    return build_output_spec(
        TaskSpec(
            objective="清洗订单，列出异常并生成一张异常分布图和报告",
            output_intent={"needs_review": True, "needs_charts": True},
        )
    )


def test_linter_accepts_exact_declared_delivery() -> None:
    spec = _spec()

    lint_output(
        spec,
        sheets={
            "处理结果": ["order_id", "处理状态"],
            "问题说明": ["order_id", "问题类型"],
        },
        chart_ids=["exception_distribution"],
        artifact_names=[
            "final_result.xlsx",
            "business_answer.md",
            "business_answer.html",
        ],
        allowed_fields={"order_id", "处理状态", "问题类型"},
    )


def test_linter_rejects_undeclared_or_missing_outputs() -> None:
    spec = _spec()

    with pytest.raises(OutputContractError, match="undeclared sheets"):
        lint_output(
            spec,
            sheets={
                "处理结果": ["order_id"],
                "问题说明": ["order_id"],
                "过程分析": ["metric"],
            },
        )
    with pytest.raises(OutputContractError, match="missing declared charts"):
        lint_output(spec, chart_ids=[])
    with pytest.raises(OutputContractError, match="undeclared artifacts"):
        lint_output(
            spec,
            artifact_names=[
                "final_result.xlsx",
                "business_answer.md",
                "business_answer.html",
                "extra.csv",
            ],
        )


def test_linter_rejects_fields_without_source_or_declaration() -> None:
    spec = build_output_spec(
        TaskSpec(objective="清洗订单", output_intent={})
    )

    with pytest.raises(OutputContractError, match="without a declared source"):
        lint_output(
            spec,
            sheets={"处理结果": ["order_id", "invented_insight"]},
            allowed_fields={"order_id"},
        )


def test_linter_enforces_exact_field_order_and_row_contract() -> None:
    spec = build_output_spec(
        TaskSpec(objective="输出订单", actions=["export"]),
        primary_fields=["订单号", "金额"],
        primary_row_count=2,
    )

    lint_output(
        spec,
        sheets={"处理结果": ["订单号", "金额"]},
        row_counts={"处理结果": 2},
    )
    with pytest.raises(OutputContractError, match="declared order"):
        lint_output(
            spec,
            sheets={"处理结果": ["金额", "订单号"]},
            row_counts={"处理结果": 2},
        )
    with pytest.raises(OutputContractError, match="row count"):
        lint_output(
            spec,
            sheets={"处理结果": ["订单号", "金额"]},
            row_counts={"处理结果": 1},
        )
