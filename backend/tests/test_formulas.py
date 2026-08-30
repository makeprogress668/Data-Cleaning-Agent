import pandas as pd
import pytest
from pydantic import ValidationError

from data_agent.schemas.job import (
    ConditionConfig,
    FormulaConfig,
    LookupConfig,
    PivotMetric,
)
from data_agent.tools.formulas import apply_formulas


def test_apply_formulas_supports_common_excel_equivalents() -> None:
    df = pd.DataFrame(
        {
            "sitecode": ["S001", ""],
            "owner_code": ["O01", "O02"],
            "building_status": ["活跃", "inactive"],
        }
    )

    result = apply_formulas(
        df,
        [
            FormulaConfig(
                output="record_key",
                op="concat",
                columns=["sitecode", "owner_code"],
                sep="-",
            ),
            FormulaConfig(
                output="status_std",
                op="map",
                source="building_status",
                mapping={"活跃": "active", "inactive": "inactive"},
                default="unknown",
            ),
            FormulaConfig(
                output="is_active",
                op="if_else",
                condition=ConditionConfig(field="status_std", op="equals", value="active"),
                true_value="Y",
                false_value="N",
            ),
        ],
    )

    assert result["record_key"].tolist() == ["S001-O01", "-O02"]
    assert result["status_std"].tolist() == ["active", "inactive"]
    assert result["is_active"].tolist() == ["Y", "N"]


def test_apply_formulas_supports_advanced_excel_equivalents() -> None:
    df = pd.DataFrame(
        {
            "code": ["A", "B", "C"],
            "lookup_code": ["A", "B", "D"],
            "lookup_value": [10, 20, 40],
            "amount": [100.5, 200.2, 50.0],
            "status": ["ok", "bad", "ok"],
            "text": ["ABCDE", "XYZ12", "S-003"],
            "compound": ["north-001", "south-002", "west-003"],
            "raw_date": ["2026-06-30", "2026-07-01", "invalid"],
            "year": [2026, 2026, 2026],
            "month": [6, 7, 8],
            "day": [30, 1, 15],
            "ratio": [1.234, None, "#N/A"],
        }
    )

    result = apply_formulas(
        df,
        [
            FormulaConfig(output="ratio_safe", op="iferror", source="ratio", default=0),
            FormulaConfig(output="ok_count", op="countif", source="status", value="ok"),
            FormulaConfig(
                output="ok_amount",
                op="sumif",
                source="status",
                value="ok",
                columns=["amount"],
            ),
            FormulaConfig(
                output="xlookup_value",
                op="xlookup",
                source="code",
                columns=["lookup_code", "lookup_value"],
                default=-1,
            ),
            FormulaConfig(
                output="index_match_value",
                op="index_match",
                source="code",
                columns=["lookup_code", "lookup_value"],
                default=-1,
            ),
            FormulaConfig(output="left_text", op="left", source="text", value=2),
            FormulaConfig(output="right_text", op="right", source="text", value=2),
            FormulaConfig(output="mid_text", op="mid", source="text", values=[2, 3]),
            FormulaConfig(output="text_date", op="text", source="raw_date", value="yyyy/mm/dd"),
            FormulaConfig(output="date_value", op="date", columns=["year", "month", "day"]),
            FormulaConfig(output="rounded", op="round", source="amount", value=0),
            FormulaConfig(
                output="amount_bucket",
                op="bucket",
                source="amount",
                values=[100, 200],
                mapping={"100": "low", "200": "medium"},
                default="high",
            ),
            FormulaConfig(output="digits", op="regex_extract", source="text", value=r"(\d+)"),
            FormulaConfig(output="split_region", op="split", source="compound", sep="-", value=0),
            FormulaConfig(output="joined", op="join", columns=["code", "status"], sep=":"),
        ],
    )

    assert result["ratio_safe"].tolist() == [1.234, 0, 0]
    assert result["ok_count"].tolist() == [2, 2, 2]
    assert result["ok_amount"].tolist() == [150.5, 150.5, 150.5]
    assert result["xlookup_value"].tolist() == [10, 20, -1]
    assert result["index_match_value"].tolist() == [10, 20, -1]
    assert result["left_text"].tolist() == ["AB", "XY", "S-"]
    assert result["right_text"].tolist() == ["DE", "12", "03"]
    assert result["mid_text"].tolist() == ["BCD", "YZ1", "-00"]
    assert result["text_date"].tolist() == ["2026/06/30", "2026/07/01", ""]
    assert result["date_value"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-06-30",
        "2026-07-01",
        "2026-08-15",
    ]
    assert result["rounded"].tolist() == [100.0, 200.0, 50.0]
    assert result["amount_bucket"].tolist() == ["medium", "high", "low"]
    assert result["digits"].tolist() == ["", "12", "003"]
    assert result["split_region"].tolist() == ["north", "south", "west"]
    assert result["joined"].tolist() == ["A:ok", "B:bad", "C:ok"]


def test_apply_formulas_supports_business_productivity_ops() -> None:
    df = pd.DataFrame(
        {
            "dept": ["A", "A", "B", "A"],
            "status": ["ok", "bad", "ok", "ok"],
            "amount": [100, 200, 300, 400],
            "sitecode": ["S001", "S001", "S002", "S001"],
            "updated_at": ["2026-06-01", "2026-06-03", "2026-06-02", "2026-06-05"],
            "start_date": ["2026-06-01", "2026-06-01", "2026-06-05", "2026-06-10"],
            "end_date": ["2026-06-03", "2026-06-05", "2026-06-08", "2026-06-20"],
            "category": [None, "x", None, "y"],
        }
    )

    result = apply_formulas(
        df,
        [
            FormulaConfig(
                output="a_ok_count",
                op="countifs",
                conditions=[
                    ConditionConfig(field="dept", op="equals", value="A"),
                    ConditionConfig(field="status", op="equals", value="ok"),
                ],
            ),
            FormulaConfig(
                output="a_ok_amount",
                op="sumifs",
                columns=["amount"],
                conditions=[
                    ConditionConfig(field="dept", op="equals", value="A"),
                    ConditionConfig(field="status", op="equals", value="ok"),
                ],
            ),
            FormulaConfig(
                output="filter_ok_large",
                op="filter_rows",
                conditions=[
                    ConditionConfig(field="status", op="equals", value="ok"),
                    ConditionConfig(field="amount", op="gt", value=150),
                ],
            ),
            FormulaConfig(
                output="keep_latest_sitecode",
                op="dedupe",
                source="updated_at",
                columns=["sitecode"],
                value="latest",
            ),
            FormulaConfig(output="amount_rank", op="sort_rank", source="amount", value="desc"),
            FormulaConfig(output="amount_top2", op="top_n", source="amount", value=2),
            FormulaConfig(
                output="duration_days",
                op="date_diff",
                source="start_date",
                columns=["end_date"],
                value="days",
            ),
            FormulaConfig(output="category_filled", op="fill_down", source="category"),
        ],
    )

    assert result.index.tolist() == [2, 3]
    assert result["amount"].tolist() == [300, 400]
    assert result["a_ok_count"].tolist() == [2, 2]
    assert result["a_ok_amount"].tolist() == [500, 500]
    assert "filter_ok_large" not in result.columns
    assert "keep_latest_sitecode" not in result.columns
    assert result["amount_rank"].tolist() == [2.0, 1.0]
    assert result["amount_top2"].tolist() == [True, True]
    assert result["duration_days"].tolist() == [3, 10]
    assert result["category_filled"].tolist() == [None, "y"]


def test_apply_formulas_supports_extended_excel_text_date_and_logic_ops() -> None:
    df = pd.DataFrame(
        {
            "status": ["paid", "pending", "failed"],
            "amount": [12.341, -8.765, None],
            "text": [" A-001 ", "Ｂeta#02", None],
            "raw_date": ["2026-07-01", "2026-08-15", "bad"],
            "flag_a": [True, True, False],
            "flag_b": [True, False, False],
            "maybe_error": ["#N/A", "#VALUE!", "ok"],
        }
    )

    result = apply_formulas(
        df,
        [
            FormulaConfig(
                output="status_label",
                op="ifs",
                conditions=[
                    ConditionConfig(field="status", op="equals", value="paid"),
                    ConditionConfig(field="status", op="equals", value="pending"),
                ],
                values=["已支付", "待处理"],
                default="其他",
            ),
            FormulaConfig(
                output="both_flags",
                op="and",
                conditions=[
                    ConditionConfig(field="flag_a", op="equals", value=True),
                    ConditionConfig(field="flag_b", op="equals", value=True),
                ],
            ),
            FormulaConfig(
                output="any_flag",
                op="or",
                conditions=[
                    ConditionConfig(field="flag_a", op="equals", value=True),
                    ConditionConfig(field="flag_b", op="equals", value=True),
                ],
            ),
            FormulaConfig(output="not_a", op="not", source="flag_a"),
            FormulaConfig(output="ifna_value", op="ifna", source="maybe_error", default="NA"),
            FormulaConfig(output="length", op="len", source="text"),
            FormulaConfig(output="subbed", op="substitute", source="text", values=["-", "_"]),
            FormulaConfig(
                output="replaced",
                op="replace",
                source="status",
                values=[1, 4],
                value="done",
            ),
            FormulaConfig(
                output="regex_clean",
                op="regex_replace",
                source="text",
                value=r"[^A-Za-z0-9]+",
                default="",
            ),
            FormulaConfig(output="joined_text", op="textjoin", columns=["status", "text"], sep="|"),
            FormulaConfig(output="normalized", op="normalize_width", source="text"),
            FormulaConfig(output="special_removed", op="remove_special_chars", source="text"),
            FormulaConfig(output="year_value", op="year", source="raw_date"),
            FormulaConfig(output="month_value", op="month", source="raw_date"),
            FormulaConfig(output="day_value", op="day", source="raw_date"),
            FormulaConfig(output="today_value", op="today", value="2026-07-01"),
            FormulaConfig(output="abs_amount", op="abs", source="amount"),
            FormulaConfig(output="roundup_amount", op="roundup", source="amount", value=1),
            FormulaConfig(output="rounddown_amount", op="rounddown", source="amount", value=1),
        ],
    )

    assert result["status_label"].tolist() == ["已支付", "待处理", "其他"]
    assert result["both_flags"].tolist() == [True, False, False]
    assert result["any_flag"].tolist() == [True, True, False]
    assert result["not_a"].tolist() == [False, False, True]
    assert result["ifna_value"].tolist() == ["NA", "#VALUE!", "ok"]
    assert result["length"].tolist() == [7, 7, 0]
    assert result["subbed"].tolist() == [" A_001 ", "Ｂeta#02", ""]
    assert result["replaced"].tolist() == ["done", "doneing", "doneed"]
    assert result["regex_clean"].tolist() == ["A001", "eta02", ""]
    assert result["joined_text"].tolist() == ["paid| A-001 ", "pending|Ｂeta#02", "failed"]
    assert result["normalized"].tolist() == ["A-001", "Beta#02", ""]
    assert result["special_removed"].tolist() == ["A001", "eta02", ""]
    assert result["year_value"].dropna().astype(int).tolist() == [2026, 2026]
    assert result["month_value"].dropna().astype(int).tolist() == [7, 8]
    assert result["day_value"].dropna().astype(int).tolist() == [1, 15]
    assert result["today_value"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-07-01",
        "2026-07-01",
        "2026-07-01",
    ]
    assert result["abs_amount"].dropna().round(3).tolist() == [12.341, 8.765]
    assert result["roundup_amount"].dropna().round(1).tolist() == [12.4, -8.8]
    assert result["rounddown_amount"].dropna().round(1).tolist() == [12.3, -8.7]


def test_apply_formulas_only_skips_explicitly_optional_formula(caplog) -> None:
    df = pd.DataFrame({"amount": [10, 20], "name": ["a", "b"]})

    with caplog.at_level("WARNING"):
        result = apply_formulas(
            df,
            [
                FormulaConfig(output="amount_copy", op="copy", source="amount"),
                # References a column that does not exist -> raises inside evaluate.
                FormulaConfig(
                    output="broken",
                    op="copy",
                    source="does_not_exist",
                    required=False,
                ),
                FormulaConfig(output="name_upper", op="upper", source="name"),
            ],
        )

    # Good formulas still ran.
    assert result["amount_copy"].tolist() == [10, 20]
    assert result["name_upper"].tolist() == ["A", "B"]
    # The bad formula produced no column instead of aborting the job.
    assert "broken" not in result.columns
    # The skip is observable in logs.
    assert any("跳过可选公式" in record.message for record in caplog.records)


def test_required_formula_failure_aborts_execution() -> None:
    df = pd.DataFrame({"value": [1, 2, 3]})

    with pytest.raises(ValueError, match="必需公式"):
        apply_formulas(
            df,
            [
                FormulaConfig(output="ok", op="copy", source="value"),
                FormulaConfig(output="nope", op="copy", source="missing"),
            ],
        )


def test_unsupported_formula_op_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError, match="not_a_real_op"):
        FormulaConfig(output="nope", op="not_a_real_op", source="value")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ConditionConfig, {"field": "status", "op": "unknown_condition"}),
        (
            LookupConfig,
            {
                "left_key": "id",
                "right_key": "id",
                "match_mode": "semantic_magic",
            },
        ),
        (PivotMetric, {"agg": "arbitrary_method"}),
    ],
)
def test_unknown_execution_operators_are_rejected_by_schema(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
