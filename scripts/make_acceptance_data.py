"""Generate the starter adversarial dataset for the acceptance matrix.

These five files are the ones already known to break something, kept small enough
that the right answer is obvious by hand — which is the whole discipline: if you
cannot work out the correct result on paper, you cannot tell a correct answer from a
plausible one. Replace them with your own business files as you go; the manifest
format is the same.

    python scripts/make_acceptance_data.py data/acceptance
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def build(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)

    # A. 两个都能叫「城市」的列。「按城市统计」指哪个？现在会静默选一个。
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4"],
            "发货城市": ["北京", "上海", "北京", "广州"],
            "客户城市": ["天津", "上海", "天津", "深圳"],
            "金额": [1000, 2000, 1500, 800],
        }
    ).to_excel(target / "A_歧义字段.xlsx", index=False)

    # B. 双层表头：第一行是合并的大类，第二行才是真字段名。
    with pd.ExcelWriter(target / "B_双层表头.xlsx") as writer:
        pd.DataFrame(
            [
                ["订单信息", "订单信息", "金额", "金额"],
                ["订单号", "客户", "不含税", "含税"],
                ["O1", "张三", 100, 113],
                ["O2", "李四", 200, 226],
                ["O3", "张三", 300, 339],
            ]
        ).to_excel(writer, index=False, header=False)

    # C. 数字存成文本：千分位与会计负数 (567)。CSV 导出、系统导出、复制粘贴都会产生。
    # 不含 Excel 日期序列号——真正的日期单元格读出来就是 datetime，那不是这个产品的问题。
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4"],
            "城市": ["北京", "上海", "北京", "上海"],
            "金额": ["1,234", "(567)", "2,000", "800"],
        }
    ).to_excel(target / "C_文本数字.xlsx", index=False)

    # D. 同结构的两个月，要求合并成一张表。现在只会交付其中一张。
    for month in ("1月", "2月"):
        pd.DataFrame(
            {"订单号": [f"{month}-1", f"{month}-2"], "金额": [100, 200]}
        ).to_excel(target / f"D_销售_{month}.xlsx", index=False)

    # E. 一对多：主表的总额被复制到每一条明细上，SUM 会算出 800 而不是 500。
    pd.DataFrame(
        {"订单号": ["O1", "O2"], "客户": ["张三", "李四"], "总额": [300, 200]}
    ).to_excel(target / "E_订单主表.xlsx", index=False)
    pd.DataFrame(
        {"订单号": ["O1", "O1", "O2"], "商品": ["A", "B", "C"], "小计": [100, 200, 200]}
    ).to_excel(target / "E_订单明细.xlsx", index=False)

    # F. 授权边界：同一份数据换措辞，左列不许动数据、右列必须动。
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O2", "O3", "O4"],
            "客户": ["张三", "李四", "李四", "王五", "赵六"],
            "金额": [5000, 200, 200, -50, 8000],
        }
    ).to_excel(target / "F_授权边界.xlsx", index=False)

    print(f"已生成 {len(list(target.glob('*.xlsx')))} 个文件到 {target}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "data/acceptance"))
