"""把明细汇总到主表 —— Excel 的跨表 SUMIF。

VLOOKUP 带回一个匹配值，SUMIF 带回所有匹配行的**总数**。没有后者，用户能把客户名称
关联到订单上，却没法把订单总额填到客户上 —— 而后一个方向才是大多数报表要的。

两半引擎本来都在（分组聚合、按键匹配），但没有任何东西把它们组合起来，于是这个操作
根本没法被提出来。

行数永不改变：聚合结果按定义每个键一行，不像直接关联明细表那样会把主表撑开。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.agent.goal_understanding import understand_goal
from data_agent.services import answer_input_paths
from data_agent.tools.rollup import rollup_to_master


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


def _master() -> pd.DataFrame:
    return pd.DataFrame(
        {"客户编号": ["C1", "C2", "C3"], "客户名称": ["甲公司", "乙公司", "丙公司"]}
    )


def _detail() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4"],
            "客户编号": ["C1", "C1", "C2", "C2"],
            "金额": ["1,234", "(200)", 500, 300],
        }
    )


def _inputs(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    _master().to_excel(src / "客户档案.xlsx", index=False)
    _detail().to_excel(src / "订单明细.xlsx", index=False)
    return src


def _deliver(tmp_path: Path, goal: str, name: str) -> pd.DataFrame:
    answer_input_paths([_inputs(tmp_path)], goal=goal, output_dir=tmp_path / name)
    return pd.read_excel(tmp_path / name / "final_result.xlsx", sheet_name="处理结果")


# --- 执行层 -------------------------------------------------------------------


def test_totals_land_on_the_master_without_changing_its_rows() -> None:
    out = rollup_to_master(
        _master(),
        _detail(),
        left_key="客户编号",
        right_key="客户编号",
        measure="金额",
        agg="sum",
        output="订单总额",
    )

    assert len(out) == 3, "主表行数被改变了 —— 直接关联明细才会这样"
    totals = dict(zip(out["客户编号"], out["订单总额"], strict=True))
    assert totals["C1"] == 1034, "会计负数和千分位没算对：1,234 + (200)"
    assert totals["C2"] == 800


def test_a_key_with_no_detail_rows_totals_zero() -> None:
    """「这个客户没下过单」是零，不是未知。Excel 的 SUMIF 也答 0。"""

    out = rollup_to_master(
        _master(), _detail(), left_key="客户编号", right_key="客户编号",
        measure="金额", agg="sum", output="订单总额",
    )
    assert dict(zip(out["客户编号"], out["订单总额"], strict=True))["C3"] == 0


def test_a_missing_column_is_refused_not_guessed() -> None:
    with pytest.raises(KeyError):
        rollup_to_master(
            _master(), _detail(), left_key="客户编号", right_key="客户编号",
            measure="不存在的列", agg="sum", output="x",
        )


# --- 端到端 -------------------------------------------------------------------


def test_a_summing_goal_with_a_destination_fills_the_master(tmp_path: Path) -> None:
    delivered = _deliver(tmp_path, "把订单明细的金额按客户汇总到客户档案", "sum")

    assert list(delivered.columns) == ["客户编号", "客户名称", "金额合计"]
    assert len(delivered) == 3
    assert dict(zip(delivered["客户编号"], delivered["金额合计"], strict=True))["C1"] == 1034


def test_a_complete_rollup_goal_does_not_ask_for_another_analysis_dimension() -> None:
    task = understand_goal(
        "把订单明细的金额按客户汇总到客户档案",
        {"客户档案": _master(), "订单明细": _detail()},
    )["task_spec"]

    assert not any(
        slot["slot_id"] == "analysis_target" for slot in task["missing_slots"]
    )


def test_counting_needs_no_measure_column(tmp_path: Path) -> None:
    """「统计每个客户的订单笔数」没点名任何数值列 —— 最基本的汇总不该因此做不了。"""

    delivered = _deliver(tmp_path, "统计每个客户的订单笔数，填到客户档案", "count")

    assert "笔数" in delivered.columns
    assert dict(zip(delivered["客户编号"], delivered["笔数"], strict=True)) == {
        "C1": 2, "C2": 2, "C3": 0,
    }


def test_a_summary_without_a_destination_stays_a_summary(tmp_path: Path) -> None:
    """「按客户统计金额」要的是一张汇总表，不是往主表上加一列。
    目的地短语才是两者的分界。"""

    delivered = _deliver(tmp_path, "按客户统计金额合计", "plain")

    assert "金额合计" not in delivered.columns
