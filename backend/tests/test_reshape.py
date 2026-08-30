"""宽转长和交叉表 —— Excel 的逆透视与透视表。

报表按人读的方式排版：一行一个客户、十二列十二个月。而任何分析要的都是相反的形状 ——
「按月统计」需要一个月份列，可表里没有，只有十二个**列名**是月份。

反过来，「行是城市，列是月份」要的是布局。同样的数字，一个横着读一个竖着读，排错方向
的报表没人用。

两件事都只在目标要求时才做：宽表本身就是用户报表的样子，不请自来地重排等于交回一份
他认不出来的东西。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.schemas.job import PivotMetric
from data_agent.services import answer_input_paths
from data_agent.tools.pivot import build_crosstab
from data_agent.utils.reshape import melt_wide_to_long, name_shape, wide_value_columns


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


def _wide() -> pd.DataFrame:
    return pd.DataFrame(
        {"客户": ["甲公司", "乙公司"], "1月": [100, 200], "2月": [110, 210], "3月": [120, 220]}
    )


def _long_source(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    _wide().to_excel(src / "月度销售.xlsx", index=False)
    return src


def _crosstab_source(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4", "O5"],
            "城市": ["北京", "北京", "上海", "上海", "广州"],
            "月份": ["1月", "2月", "1月", "2月", "1月"],
            "金额": ["1,000", 2000, 3000, "(500)", 700],
        }
    ).to_excel(src / "订单.xlsx", index=False)
    return src


def _sheet(tmp_path: Path, src: Path, goal: str, name: str, sheet: str) -> pd.DataFrame:
    answer_input_paths([src], goal=goal, output_dir=tmp_path / name)
    return pd.read_excel(tmp_path / name / "final_result.xlsx", sheet_name=sheet)


# --- 宽列识别 -----------------------------------------------------------------


def test_digits_fold_so_siblings_group() -> None:
    """1月/2月/12月 折叠成同一个形状 —— 不需要月份词表，季度年份周次一样管用。"""

    assert name_shape("1月") == name_shape("12月") == "#月"
    assert name_shape("2024-01") == "#-#"


def test_a_wide_block_is_found() -> None:
    assert wide_value_columns(_wide()) == ["1月", "2月", "3月"]


def test_two_similar_columns_are_not_a_wide_block() -> None:
    """金额1/金额2 更可能是两个真字段，不是一个维度的两个取值。"""

    frame = pd.DataFrame({"客户": ["甲"], "金额1": [1], "金额2": [2]})
    assert wide_value_columns(frame) == []


# --- 宽转长 -------------------------------------------------------------------


def test_melting_keeps_every_value() -> None:
    long = melt_wide_to_long(
        _wide(), value_columns=["1月", "2月", "3月"], variable_name="月份", value_name="金额"
    )

    assert len(long) == 6, "2 个客户 × 3 个月"
    assert set(long["月份"]) == {"1月", "2月", "3月"}
    assert long["金额"].sum() == _wide()[["1月", "2月", "3月"]].to_numpy().sum()


def test_a_goal_that_asks_for_long_form_gets_it(tmp_path: Path) -> None:
    delivered = _sheet(
        tmp_path, _long_source(tmp_path), "把月份列转成行，做成长表", "melt", "处理结果"
    )

    assert list(delivered.columns) == ["客户", "月份", "数值"]
    assert len(delivered) == 6


def test_a_goal_that_does_not_ask_keeps_the_wide_shape(tmp_path: Path) -> None:
    """宽表本身就是用户报表的样子。不请自来地重排等于交回他认不出来的东西。"""

    delivered = _sheet(tmp_path, _long_source(tmp_path), "清洗一下数据", "no_melt", "处理结果")

    assert list(delivered.columns) == ["客户", "1月", "2月", "3月"]


def test_reshaping_then_aggregating_uses_the_new_columns(tmp_path: Path) -> None:
    """「宽转长后按月份统计金额」—— 汇总要认识转换之后才存在的那两列。"""

    summary = _sheet(
        tmp_path, _long_source(tmp_path), "宽转长后按月份统计金额合计", "both", "汇总结果"
    )

    totals = dict(zip(summary["月份"], summary["金额合计"], strict=True))
    assert totals == {"1月": 300, "2月": 320, "3月": 340}


# --- 交叉表 -------------------------------------------------------------------


def test_crosstab_reads_across_instead_of_down() -> None:
    df = pd.DataFrame(
        {
            "城市": ["北京", "北京", "上海"],
            "月份": ["1月", "2月", "1月"],
            "金额": ["1,000", 2000, 3000],
        }
    )
    table = build_crosstab(
        df, ["城市"], "月份", PivotMetric(column="金额", agg="sum", output_name="金额合计")
    )

    assert list(table.columns) == ["城市", "1月", "2月"]
    row = table.set_index("城市").loc["北京"]
    assert row["1月"] == 1000 and row["2月"] == 2000


def test_a_missing_combination_is_zero_not_blank() -> None:
    """「这个城市三月没销售」是一个数。Excel 的透视表也答 0。"""

    table = build_crosstab(
        pd.DataFrame({"城市": ["北京", "上海"], "月份": ["1月", "2月"], "金额": [100, 200]}),
        ["城市"],
        "月份",
        PivotMetric(column="金额", agg="sum", output_name="金额合计"),
    )
    assert table.set_index("城市").loc["北京", "2月"] == 0


def test_asking_for_a_crosstab_lays_it_out_across(tmp_path: Path) -> None:
    summary = _sheet(
        tmp_path,
        _crosstab_source(tmp_path),
        "按城市和月份做交叉表，统计金额合计",
        "cross",
        "汇总结果",
    )

    assert list(summary.columns) == ["城市", "1月", "2月"]
    assert summary.set_index("城市").loc["上海", "2月"] == -500


def test_two_dimensions_without_a_crosstab_word_stay_rows(tmp_path: Path) -> None:
    """「按城市和月份统计」要的是分组列表，不是布局。"""

    summary = _sheet(
        tmp_path, _crosstab_source(tmp_path), "按城市和月份统计金额合计", "rows", "汇总结果"
    )

    assert {"城市", "月份", "金额合计"} <= set(summary.columns)
    assert len(summary) == 5
