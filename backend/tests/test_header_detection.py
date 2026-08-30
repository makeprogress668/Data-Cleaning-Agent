"""表头不在第一行的时候，要找得到它在哪。

中文业务表是做给人看的：第一行写着「2024年销售明细表」，或者表头有两层、上面一层
跨列合并（订单信息 横跨 订单号 和 客户，金额 横跨 不含税 和 含税）。用默认的
``header=0`` 读这种文件，列名会变成 ``订单信息``、``订单信息.1``，真正的表头行掉进
数据里 —— 之后所有字段引用都落空，汇总建不出来，而整个任务「成功」结束，交出一份
什么都没回答的工作簿。

修这件事的风险在于修得太积极：判断错了会把本来好好的文件弄坏。所以只在正常表头
不可能出现的证据下才触发（重名，或者合并单元格留下的空档），其余一律按原样读。
"""

from __future__ import annotations

import pandas as pd

from data_agent.tools.header_detection import (
    combine_header_levels,
    detect_header,
    reframe_with_header,
)


def _raw(rows: list[list[object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- 不该动的：普通文件必须原样通过 --------------------------------------------


def test_an_ordinary_header_is_left_alone() -> None:
    raw = _raw([["订单号", "客户", "金额"], ["O1", "张三", 100]])

    assert detect_header(raw) == (0, 1)
    assert reframe_with_header(raw) is None, "普通文件被改动了"


def test_an_empty_sheet_is_left_alone() -> None:
    assert detect_header(pd.DataFrame()) == (0, 1)


def test_a_header_whose_names_look_like_years_is_still_a_header() -> None:
    """「2023」「2024」当列名很常见，不能因为是数字就当成数据行。"""

    raw = _raw([["城市", "2023", "2024"], ["北京", 100, 200]])
    assert detect_header(raw) == (0, 1)


# --- 双层表头 -----------------------------------------------------------------


def test_a_merged_two_level_header_is_found() -> None:
    """真实合并单元格：只有左上角存值，右边留空。"""

    raw = _raw(
        [
            ["订单信息", None, "金额", None],
            ["订单号", "客户", "不含税", "含税"],
            ["O1", "张三", 100, 113],
        ]
    )
    assert detect_header(raw) == (0, 2)

    framed = reframe_with_header(raw)
    assert list(framed.columns) == ["订单号", "客户", "不含税", "含税"]
    assert len(framed) == 1, "真表头行没被吃掉，或者数据行丢了"


def test_a_repeated_two_level_header_is_found() -> None:
    """有些导出把合并值重复写在每一列上，pandas 会renames成 金额.1。"""

    raw = _raw(
        [
            ["订单信息", "订单信息", "金额", "金额"],
            ["订单号", "客户", "不含税", "含税"],
            ["O1", "张三", 100, 113],
        ]
    )
    assert detect_header(raw) == (0, 2)


def test_the_group_name_is_only_added_where_it_is_needed() -> None:
    """订单信息_订单号 比 订单号 更长、没多给信息，还更难和目标对上。"""

    assert combine_header_levels(
        ["订单信息", "订单信息", "金额", "金额"],
        ["订单号", "客户", "不含税", "含税"],
    ) == ["订单号", "客户", "不含税", "含税"]


def test_the_group_name_is_added_when_two_groups_collide() -> None:
    """金额和数量下面都有「不含税」，这时候不加前缀就分不清了。"""

    assert combine_header_levels(
        ["金额", "金额", "数量", "数量"],
        ["不含税", "含税", "不含税", "含税"],
    ) == ["金额_不含税", "金额_含税", "数量_不含税", "数量_含税"]


# --- 标题行 -------------------------------------------------------------------


def test_a_title_row_above_the_table_is_skipped() -> None:
    raw = _raw(
        [
            ["2024年销售明细表", None, None],
            ["订单号", "客户", "金额"],
            ["O1", "张三", 100],
        ]
    )
    assert detect_header(raw) == (1, 1)

    framed = reframe_with_header(raw)
    assert list(framed.columns) == ["订单号", "客户", "金额"]
    assert len(framed) == 1


def test_a_blank_spacer_row_above_the_table_is_skipped() -> None:
    raw = _raw([[None, None, None], ["订单号", "客户", "金额"], ["O1", "张三", 100]])
    assert detect_header(raw) == (1, 1)


def test_a_sheet_with_no_header_at_all_is_left_alone() -> None:
    """认不出来就别动。猜错的代价是把一份本来能用的文件弄坏。"""

    assert detect_header(_raw([["O1", "张三", 100], ["O2", "李四", 200]])) == (0, 1)


def test_two_rows_are_never_read_as_two_header_levels() -> None:
    """重名表头 + 一行数据，如果当成双层表头，那一行数据就没了。

    双层表头底下必须有东西。这条约束比任何「这行看起来像不像表头」的判断都可靠。
    """

    raw = _raw([["订单号", "订单号", "金额"], ["O1", "O2", 100]])
    assert detect_header(raw) == (0, 1)
    assert reframe_with_header(raw) is None
