import pandas as pd

from data_agent.tools.profiler import (
    detect_key_candidates,
    detect_relationship_candidates,
    profile_tables,
)


def test_profile_tables_returns_table_column_and_issue_sheets() -> None:
    tables = {
        "raw": pd.DataFrame({"sitecode": ["S001", ""], "amount": [1, None]}),
        "lookup": pd.DataFrame({"sitecode": ["S001"]}),
    }

    result = profile_tables(tables)

    assert set(result) == {"table_profile", "column_profile", "profile_issues"}
    assert result["table_profile"].set_index("table").loc["raw", "row_count"] == 2
    assert "empty_strings" in set(result["profile_issues"]["issue_type"])
    assert "null_values" in set(result["profile_issues"]["issue_type"])


def test_chinese_column_names_can_be_matched_across_tables() -> None:
    """跨表关联必须支持中文列名。

    Key normalisation used an ASCII-only character class, so every Chinese column
    collapsed to the empty string: the {normalized: column} map kept one entry per
    table and the only "shared key" found was whichever pair of last columns
    remained. Cross-table lookup — one of the product's core capabilities — could
    never work on Chinese data.
    """

    orders = pd.DataFrame(
        {"订单号": ["O1", "O2"], "客户编号": ["C1", "C2"], "金额": [100, 200]}
    )
    customers = pd.DataFrame({"客户编号": ["C1", "C2"], "客户名称": ["甲公司", "乙公司"]})

    relationships = detect_relationship_candidates({"订单明细": orders, "客户档案": customers})

    matched = relationships[
        relationships["left_table"].eq("订单明细")
        & relationships["recommendation"].eq("recommended_lookup")
    ]
    assert len(matched) == 1
    assert matched.iloc[0]["left_field"] == "客户编号"
    assert matched.iloc[0]["right_field"] == "客户编号"
    assert matched.iloc[0]["left_match_rate"] == 1.0
    # 不应再把毫不相关的列配成候选
    assert "金额" not in set(relationships["left_field"])


def test_chinese_key_columns_are_detected_as_key_candidates() -> None:
    """中文主键/业务键必须能被识别，否则中文表没有任何关键字段候选。"""

    tables = {
        "订单明细": pd.DataFrame(
            {"订单号": ["O1", "O2"], "客户编号": ["C1", "C2"], "金额": [100, 200]}
        )
    }
    profiles = profile_tables(tables)
    candidates = detect_key_candidates(tables, profiles["column_profile"])

    fields = set(candidates["field"])
    assert {"订单号", "客户编号"} <= fields
    assert "金额" not in fields
