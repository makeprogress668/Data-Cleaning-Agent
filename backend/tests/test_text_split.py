"""把一列拆成几列 —— Excel 的分列。

「把地址拆成省、市、区」是中文业务表上最常做的操作之一，而用户几乎不说分隔符，他们
说「拆开」，指望工具自己看数据。所以分隔符先从目标里读，读不到就从这一列的值里推。

推不出来就什么都不做。一列在有些行分成三段、有些行分成五段，那不是分隔字段，是句子；
按错误的字符拆开，得到的是每行含义都不同的几列，比不拆糟糕得多。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from data_agent.services import answer_input_paths
from data_agent.utils.text_split import infer_separator, split_part_names


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    warnings.filterwarnings("ignore")


def _inputs(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3"],
            "地址": ["北京-朝阳-三里屯", "上海-浦东-张江", "广州-天河-珠江新城"],
            "金额": [100, 200, 300],
        }
    ).to_excel(src / "订单.xlsx", index=False)
    return src


def _deliver(tmp_path: Path, goal: str, name: str) -> pd.DataFrame:
    answer_input_paths([_inputs(tmp_path)], goal=goal, output_dir=tmp_path / name)
    return pd.read_excel(tmp_path / name / "final_result.xlsx", sheet_name="处理结果")


# --- 分隔符推断 ---------------------------------------------------------------


def test_a_consistent_delimiter_is_inferred() -> None:
    values = pd.Series(["北京-朝阳-三里屯", "上海-浦东-张江", "广州-天河-珠江新城"])
    assert infer_separator(values) == ("-", 3)


def test_inconsistent_part_counts_infer_nothing() -> None:
    """三段、四段、五段混在一起 —— 拆出来的列每行含义都不一样。"""

    assert infer_separator(pd.Series(["a-b", "c-d-e", "f-g-h-i"])) == ("", 0)


def test_plain_text_infers_nothing() -> None:
    assert infer_separator(pd.Series(["张三", "李四", "王五"])) == ("", 0)


def test_user_names_beat_generated_ones() -> None:
    assert split_part_names("地址", 3, ["省", "市", "区"]) == ["省", "市", "区"]
    assert split_part_names("地址", 3) == ["地址_1", "地址_2", "地址_3"]


# --- 交付 ---------------------------------------------------------------------


def test_named_parts_become_named_columns(tmp_path: Path) -> None:
    delivered = _deliver(tmp_path, "把地址拆成省、市、区", "named")

    assert {"省", "市", "区"} <= set(delivered.columns)
    assert list(delivered["省"]) == ["北京", "上海", "广州"]
    assert list(delivered["区"]) == ["三里屯", "张江", "珠江新城"]


def test_unnamed_parts_are_numbered_after_the_source(tmp_path: Path) -> None:
    delivered = _deliver(tmp_path, "把地址分列", "auto")

    assert {"地址_1", "地址_2", "地址_3"} <= set(delivered.columns)


def test_the_source_column_is_kept(tmp_path: Path) -> None:
    """拆分是加列，不是替换。原列还有别的用处，删掉它是用户没要求的改动。"""

    delivered = _deliver(tmp_path, "把地址拆成省、市、区", "keep_source")

    assert "地址" in delivered.columns
    assert list(delivered["地址"])[0] == "北京-朝阳-三里屯"


def test_no_split_verb_means_no_split(tmp_path: Path) -> None:
    delivered = _deliver(tmp_path, "把订单数据整理一下", "no_split")

    assert not any(str(column).startswith("地址_") for column in delivered.columns)
    assert "省" not in delivered.columns
