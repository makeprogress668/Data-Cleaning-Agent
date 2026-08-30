"""业务人员怎么写数字，就得怎么读。

一个从 ERP 导出、经过 CSV 往返、或者直接复制粘贴过来的金额列，十有八九是文本：
``1,234``、``(567)``、``￥2,000``、``1.2万``。``pd.to_numeric`` 把这些全变成 NaN，
而 NaN 进 ``sum()`` 就是 0 —— 于是「按城市统计金额合计」交出一张北京 0、上海 0 的
汇总表。它存在、它看起来正常、它全错。这比不出汇总危险得多：少一张表会被发现，
一个像模像样的 0 不会。

对标 Excel：把 ``1,234`` 粘进单元格，SUM 认；会计格式下的 ``(567)`` 就是负五百六十七。
"""

from __future__ import annotations

import pandas as pd
import pytest

from data_agent.utils.numeric import (
    parse_business_number,
    to_business_numeric,
    unparseable_count,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234", 1234),          # 千分位
        ("1，234", 1234),          # 全角逗号
        ("(567)", -567),          # 会计负数
        ("（567）", -567),         # 全角括号
        ("￥2,000", 2000),         # 货币符号
        # Excel「会计专用」格式：货币符号在括号外面，两种写法叠在一起。
        # 中文财务系统导出的负数几乎都长这样。
        ("¥(567.00)", -567),
        ("$(1,234)", -1234),
        ("(1.2万)", -12000),
        ("$1,234.56", 1234.56),
        ("2000元", 2000),
        ("1.2万", 12000),          # 中文数量单位：读成 1.2 会差四个数量级
        ("1.5亿", 150000000),
        ("１００", 100),            # 全角数字
        ("  800  ", 800),
        ("-50", -50),
        ("85%", 85),              # 与既有分析层口径一致：用户加的是看得见的数
    ],
)
def test_business_notation_reads_as_the_number_it_means(raw: str, expected: float) -> None:
    assert parse_business_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "待确认", "O1", "2024-03-15", "张三(已退货)", "-", None],
)
def test_things_that_are_not_numbers_stay_out(raw: object) -> None:
    """认不出来就是 None。硬凑一个数比留空危险得多。"""

    assert parse_business_number(raw) is None


def test_real_numbers_pass_through_untouched() -> None:
    """已经是数值的列不该再走文本那条路，免得把好数据弄坏。"""

    series = pd.Series([1234, -567.5, 0])
    assert to_business_numeric(series).tolist() == [1234, -567.5, 0]


def test_a_text_amount_column_sums_like_excel() -> None:
    series = pd.Series(["1,234", "(567)", "2,000", "800"])
    assert to_business_numeric(series).sum() == pytest.approx(3467)


def test_a_mixed_column_keeps_the_numbers_and_drops_the_labels() -> None:
    """合计行、重复表头行会把中文塞进金额列。Excel 的 SUM 直接跳过，这里也一样。"""

    series = pd.Series(["1,234", "金额", "2,000", "合计"])
    parsed = to_business_numeric(series)

    assert parsed.sum() == pytest.approx(3234)
    assert unparseable_count(series) == 2


def test_blank_cells_are_not_counted_as_unreadable() -> None:
    """空着和写了看不懂的东西是两回事，只有后者值得说。"""

    assert unparseable_count(pd.Series(["1,234", "", None, "2,000"])) == 0


def test_a_clean_numeric_column_reports_nothing_unreadable() -> None:
    assert unparseable_count(pd.Series([1, 2, 3])) == 0
