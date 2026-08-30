"""Phase 3C DuckDB pushdown equivalence and fallback contracts."""

from __future__ import annotations

import pandas as pd
import pytest

from data_agent.pipelines import run_job
from data_agent.schemas.job import (
    ConditionConfig,
    JobConfig,
    LookupConfig,
    PivotMetric,
    SourceConfig,
)
from data_agent.schemas.output import (
    AggregateArtifactSpec,
    OutputSpec,
    TableArtifactSpec,
)
from data_agent.tools.conditions import evaluate_condition
from data_agent.tools.pivot import build_pivot


def test_large_numeric_aggregate_uses_duckdb_without_changing_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("duckdb")
    frame = pd.DataFrame(
        {
            "城市": ["上海", "北京", "上海", "北京", None, None],
            "金额": [10.0, 20.0, None, 5.0, None, None],
            "客户": ["A", "B", "A", "C", "D", "D"],
        }
    )
    metrics = [
        PivotMetric(agg="count", output_name="记录数"),
        PivotMetric(column="金额", agg="sum", output_name="金额合计"),
        PivotMetric(column="金额", agg="mean", output_name="金额平均"),
        PivotMetric(column="客户", agg="nunique", output_name="客户数"),
    ]
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    accelerated = build_pivot(frame, ["城市"], metrics)
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "999999")
    pandas_result = build_pivot(frame, ["城市"], metrics)

    assert accelerated.attrs["data_agent_engine"] == "duckdb"
    pd.testing.assert_frame_equal(
        accelerated,
        pandas_result,
        check_dtype=False,
    )


def test_business_number_notation_stays_on_shared_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    frame = pd.DataFrame({"城市": ["上海", "上海"], "金额": ["1,234", "(567)"]})

    result = build_pivot(
        frame,
        ["城市"],
        [PivotMetric(column="金额", agg="sum", output_name="合计")],
    )

    assert result.loc[0, "合计"] == 667
    assert "data_agent_engine" not in result.attrs


@pytest.mark.parametrize(
    "condition",
    [
        ConditionConfig(field="状态", op="equals", value=" paid "),
        ConditionConfig(field="状态", op="not_equals", value="paid"),
        ConditionConfig(field="状态", op="contains", value="AI"),
        ConditionConfig(field="状态", op="in", values=["paid", "pending"]),
        ConditionConfig(field="金额", op="gte", value=10),
        ConditionConfig(field="金额", op="lt", value=10, negate=True),
        ConditionConfig(field="状态", op="is_blank"),
        ConditionConfig(field="状态", op="not_blank"),
    ],
)
def test_duckdb_filters_match_pandas_semantics(
    condition: ConditionConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("duckdb")
    frame = pd.DataFrame(
        {
            "状态": ["Paid", " pending ", None, "FAIL", "paid"],
            "金额": [20, "5", None, "bad", 10],
        },
        index=[9, 2, 8, 1, 7],
    )
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    accelerated = evaluate_condition(frame, condition)
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "999999")
    pandas_mask = evaluate_condition(frame, condition)

    assert accelerated.attrs["data_agent_engine"] == "duckdb"
    pd.testing.assert_series_equal(accelerated, pandas_mask)


def test_default_threshold_selects_duckdb_for_a_large_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("duckdb")
    monkeypatch.delenv("DATA_AGENT_DUCKDB_MIN_ROWS", raising=False)
    rows = 100_000
    frame = pd.DataFrame(
        {
            "区域": [f"R{index % 20}" for index in range(rows)],
            "金额": [index % 100 for index in range(rows)],
        }
    )

    result = build_pivot(
        frame,
        ["区域"],
        [PivotMetric(column="金额", agg="sum", output_name="合计")],
    )

    assert result.attrs["data_agent_engine"] == "duckdb"
    assert result["合计"].sum() == frame["金额"].sum()


def test_execution_event_records_the_selected_engine(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("duckdb")
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    source = tmp_path / "orders.csv"
    pd.DataFrame({"城市": ["上海", "北京"], "金额": [10, 20]}).to_csv(
        source,
        index=False,
    )
    output_spec = OutputSpec(
        secondary_artifacts=[TableArtifactSpec(name="汇总结果")],
        aggregate=AggregateArtifactSpec(
            name="汇总结果",
            group_by=["城市"],
            metrics=[{"column": "金额", "agg": "sum", "output_name": "合计"}],
        ),
    )

    result = run_job(
        JobConfig(
            main_source=SourceConfig(path=source),
            export={"output_file": tmp_path / "result.xlsx"},
        ),
        output_spec,
    )
    aggregate_stage = next(
        stage for stage in result.stages if stage.capability_id == "aggregate"
    )

    assert aggregate_stage.metrics["engine"] == "duckdb"


def test_lookup_event_records_duckdb_engine(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("duckdb")
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    pd.DataFrame({"客户号": ["A", "B"]}).to_csv(orders, index=False)
    pd.DataFrame({"客户号": ["A", "B"], "区域": ["北", "南"]}).to_csv(
        customers,
        index=False,
    )

    result = run_job(
        JobConfig(
            main_source=SourceConfig(path=orders),
            lookup=LookupConfig(
                source=SourceConfig(path=customers),
                left_key="客户号",
                right_key="客户号",
                fields=["区域"],
            ),
            export={"output_file": tmp_path / "lookup.xlsx"},
        )
    )
    lookup_stage = next(
        stage for stage in result.stages if stage.capability_id == "lookup_fields"
    )

    assert lookup_stage.metrics["engine"] == "duckdb"
