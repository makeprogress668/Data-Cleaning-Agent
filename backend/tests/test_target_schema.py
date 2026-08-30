"""用户说交付物长什么样，交付物就长什么样。

三种说法，同一件事 —— 一份有序的列清单，每列从它所在的表里取值：

  A 上传一张空模板        表头就是列清单
  B 在目标里点名列        「做一张表，包含订单号、客户名称、金额」
  C 以某张表为底再加列    原有的关联行为

模板文件本身绝不改动：它通常是要喂给下游系统的，列名或顺序变了，等在后面的东西就接
不上。填不上的列留在表里、留空 —— 用户要的就是这个形状，少一列比空一列糟糕得多。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.services import answer_input_paths
from data_agent.utils.target_schema import (
    goal_schema,
    is_blank_column,
    looks_like_template,
    project_onto_schema,
    template_schema,
)

_TEMPLATE_COLUMNS = ["订单号", "客户名称", "所属大区", "金额", "备注"]


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


def _sources(tmp_path: Path, *, with_template: bool) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"订单号": ["O1", "O2"], "客户编号": ["C1", "C2"], "金额": [100, 200]}
    ).to_excel(src / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {
            "客户编号": ["C1", "C2"],
            "客户名称": ["甲公司", "乙公司"],
            "所属大区": ["华北", "华东"],
        }
    ).to_excel(src / "客户档案.xlsx", index=False)
    if with_template:
        pd.DataFrame(columns=_TEMPLATE_COLUMNS).to_excel(
            src / "导入模板.xlsx", index=False
        )
    return src


def _deliver(tmp_path: Path, goal: str, name: str, *, with_template: bool = False):
    src = _sources(tmp_path, with_template=with_template)
    answer_input_paths([src], goal=goal, output_dir=tmp_path / name)
    book = tmp_path / name / "final_result.xlsx"
    return src, pd.ExcelFile(book), pd.read_excel(book, sheet_name="处理结果")


# --- 识别 ---------------------------------------------------------------------


def test_a_header_only_sheet_is_a_template() -> None:
    assert looks_like_template(pd.DataFrame(columns=["a", "b", "c"]))
    assert looks_like_template(pd.DataFrame({"a": [None], "b": [None], "c": [None]}))


def test_a_table_with_data_is_not_a_template() -> None:
    """哪怕只有一行。把数据表读成模板，交付物会被投影到它的列上 —— 比漏认模板糟得多。"""

    assert not looks_like_template(pd.DataFrame({"a": ["x"], "b": ["y"], "c": ["z"]}))


def test_two_templates_are_not_guessed_between() -> None:
    tables = {
        "模板一": pd.DataFrame(columns=["a", "b"]),
        "模板二": pd.DataFrame(columns=["c", "d"]),
    }
    assert template_schema(tables) == ("", [])


def test_a_goal_can_name_the_columns() -> None:
    assert goal_schema("做一张表，包含订单号、客户名称、金额", {"订单号", "客户名称", "金额"}) == [
        "订单号",
        "客户名称",
        "金额",
    ]


def test_a_column_that_does_not_exist_is_not_promised() -> None:
    """声明一列就是承诺它会在。编一个不存在的名字，只会多出一列没人解释得清的空白。"""

    assert goal_schema("做一张表，包含订单号、不存在的列", {"订单号", "金额"}) == []


def test_projection_keeps_order_and_blanks_what_it_cannot_fill() -> None:
    frame = pd.DataFrame({"b": [1], "a": [2]})
    projected = project_onto_schema(frame, ["a", "b", "c"])

    assert list(projected.columns) == ["a", "b", "c"]
    assert is_blank_column(projected["c"])


# --- A：空模板 ----------------------------------------------------------------


def test_an_uploaded_template_becomes_the_delivered_shape(tmp_path: Path) -> None:
    _src, book, delivered = _deliver(
        tmp_path, "按导入模板的格式填好数据", "tpl", with_template=True
    )

    assert list(delivered.columns) == _TEMPLATE_COLUMNS, "模板的列名或顺序被改了"
    assert book.sheet_names == ["处理结果"], f"多给了没要的表：{book.sheet_names}"
    assert list(delivered["客户名称"]) == ["甲公司", "乙公司"], "声明了列却没去填"
    assert is_blank_column(delivered["备注"]), "输入里没有备注，应当留空而不是编造"


def test_the_uploaded_template_file_is_never_modified(tmp_path: Path) -> None:
    """那份模板通常要喂给下游系统，动了它就等于动了别人的输入。"""

    src, _book, _delivered = _deliver(
        tmp_path, "按导入模板的格式填好数据", "untouched", with_template=True
    )
    template = pd.read_excel(src / "导入模板.xlsx")

    assert list(template.columns) == _TEMPLATE_COLUMNS
    assert len(template) == 0, "模板文件被写入了数据"


# --- B：目标里描述 -------------------------------------------------------------


def test_a_goal_declared_schema_is_built_and_filled(tmp_path: Path) -> None:
    _src, book, delivered = _deliver(tmp_path, "做一张表，包含订单号、客户名称、金额", "goal")

    assert list(delivered.columns) == ["订单号", "客户名称", "金额"]
    assert book.sheet_names == ["处理结果"]
    assert list(delivered["客户名称"]) == ["甲公司", "乙公司"]


# --- C：底表加列 ---------------------------------------------------------------


def test_an_ordinary_lookup_keeps_the_base_table_shape(tmp_path: Path) -> None:
    """没有声明结构时，交付的还是底表加上点名的那一列。"""

    _src, _book, delivered = _deliver(tmp_path, "把客户名称关联到订单明细", "plain")

    assert list(delivered.columns) == ["订单号", "客户编号", "金额", "客户名称"]
