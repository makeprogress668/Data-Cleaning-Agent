"""交付物里只有用户要的东西。

每多一张 sheet、每多一列，都在结果和读它的人之间多放一层东西。所以判断标准不是
「这个信息有没有用」，而是「用户要没要」——有用但没要，就是噪音。

三条规则，各自都曾经反着来过：
  本次改动   由动作类型自动触发 → 每个清洗任务都白送一张表
  关联字段   右表能带的全带     → 只要客户名称，却多出所属大区、联系电话
  问题说明   目标要求才出       → 这条一直是对的，钉住防止回退
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.services import answer_input_paths


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir()
    pd.DataFrame(
        {"订单号": ["O1", "O2"], "客户编号": [" C1 ", "C2"], "金额": [100, 200]}
    ).to_excel(src / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {
            "客户编号": ["C1", "C2"],
            "客户名称": ["甲公司", "乙公司"],
            "所属大区": ["华北", "华东"],
            "联系电话": ["139…", "138…"],
        }
    ).to_excel(src / "客户档案.xlsx", index=False)
    return src


def _deliver(inputs: Path, tmp_path: Path, goal: str, name: str) -> dict:
    answer_input_paths(
        [inputs], goal=goal, output_dir=tmp_path / name, confirm_destructive=True
    )
    book = tmp_path / name / "final_result.xlsx"
    excel = pd.ExcelFile(book)
    return {
        "sheets": excel.sheet_names,
        "frames": {s: pd.read_excel(book, sheet_name=s) for s in excel.sheet_names},
    }


# --- 1. 本次改动：要求才出 -----------------------------------------------------


def test_a_cleaning_goal_alone_delivers_one_sheet(inputs: Path, tmp_path: Path) -> None:
    out = _deliver(inputs, tmp_path, "清洗订单明细，去掉前后空格", "clean_only")

    assert out["sheets"] == ["处理结果"], f"多给了没要的表：{out['sheets']}"


def test_asking_what_changed_delivers_the_manifest(inputs: Path, tmp_path: Path) -> None:
    out = _deliver(
        inputs, tmp_path, "清洗订单明细，去掉前后空格，并列出本次改了什么", "clean_manifest"
    )

    assert "本次改动" in out["sheets"]


# --- 2. 关联：只带点名的字段 ---------------------------------------------------


def test_a_lookup_brings_only_the_named_column(inputs: Path, tmp_path: Path) -> None:
    """目标只提了客户名称，所属大区和联系电话就不该出现在交付表里。"""

    out = _deliver(inputs, tmp_path, "把客户名称关联到订单明细", "narrow_lookup")
    columns = set(out["frames"]["处理结果"].columns)

    assert "客户名称" in columns
    assert "所属大区" not in columns, "带回了用户没提过的列"
    assert "联系电话" not in columns, "带回了用户没提过的列"


def test_naming_two_columns_brings_both(inputs: Path, tmp_path: Path) -> None:
    out = _deliver(inputs, tmp_path, "把客户名称和所属大区关联到订单明细", "two_columns")
    columns = set(out["frames"]["处理结果"].columns)

    assert {"客户名称", "所属大区"} <= columns
    assert "联系电话" not in columns


def test_naming_no_column_keeps_the_broad_default(inputs: Path, tmp_path: Path) -> None:
    """「关联客户档案」没点名字段 —— 没有可收窄的依据，就沿用原来的行为。"""

    out = _deliver(inputs, tmp_path, "把客户档案关联到订单明细", "broad_lookup")
    columns = set(out["frames"]["处理结果"].columns)

    assert "客户名称" in columns


# --- 3. 问题说明：要求才出 -----------------------------------------------------


def test_problems_are_not_reported_unless_asked(inputs: Path, tmp_path: Path) -> None:
    out = _deliver(inputs, tmp_path, "把客户名称关联到订单明细", "no_review")

    assert "问题说明" not in out["sheets"]


def test_asking_for_problems_delivers_them(inputs: Path, tmp_path: Path) -> None:
    out = _deliver(
        inputs, tmp_path, "把客户名称关联到订单明细，并列出不能使用的记录", "with_review"
    )

    assert "问题说明" in out["sheets"]


def test_a_phrasing_the_token_table_misses_is_a_known_limit(
    inputs: Path, tmp_path: Path
) -> None:
    """「关联不上的记录」在确定性层命中不了 —— 记下来，不要以为它成立。

    理解侧仍然是词表驱动的（见 AGENTS.md）。漏词的后果已经不是删错数据，而是
    「我说了你没做」：用户要求列出关联不上的记录，交付里没有这张表。配了模型时
    这句话能被理解，但确定性底线守不住它。
    """

    out = _deliver(inputs, tmp_path, "把客户名称关联到订单明细，并列出关联不上的记录", "missed")

    assert out["sheets"] == ["处理结果"], (
        "确定性层开始认识「关联不上」了 —— 这是好事，请把这条测试改成正向断言"
    )
