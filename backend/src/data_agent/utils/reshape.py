"""Turn month-per-column into month-per-row — Excel's unpivot.

Business reports arrive wide because that is how people read them: one row per
customer, twelve columns for twelve months. Every analysis then wants the opposite
shape — 「按月统计」 needs a 月份 column, and there isn't one, there are twelve columns
whose *names* are the months.

The columns that hold values are identified by the shape of their names, not by a list
of month words: 1月/2月/3月 and 2024-01/2024-02 and Q1/Q2 all become one pattern once
the digits are folded out. That generalises to quarters, years, weeks and store codes
without a vocabulary to maintain, and it declines when the evidence is thin instead of
reshaping a table that was never wide.
"""

from __future__ import annotations

import re

import pandas as pd

# A wide block needs at least this many sibling columns. Two columns that happen to
# share a shape (金额1/金额2) are more often a pair of real fields than a dimension.
MIN_VALUE_COLUMNS = 3
_DIGITS = re.compile(r"\d+")


def name_shape(column: str) -> str:
    """The column name with every run of digits folded to ``#``.

    1月 / 2月 / 12月 all become #月, which is what makes them siblings. The fold is
    the whole trick: it needs no list of month names, so it works for quarters, years,
    weeks and store codes alike.
    """

    return _DIGITS.sub("#", str(column))


def wide_value_columns(frame: pd.DataFrame) -> list[str]:
    """The largest group of same-shaped numeric columns, or [] when there is none."""

    groups: dict[str, list[str]] = {}
    for column in frame.columns:
        name = str(column)
        if name.startswith("_") or not _DIGITS.search(name):
            continue
        if not pd.api.types.is_numeric_dtype(frame[column]):
            continue
        groups.setdefault(name_shape(name), []).append(name)
    if not groups:
        return []
    largest = max(groups.values(), key=len)
    return largest if len(largest) >= MIN_VALUE_COLUMNS else []


def melt_wide_to_long(
    frame: pd.DataFrame,
    *,
    value_columns: list[str],
    variable_name: str,
    value_name: str,
) -> pd.DataFrame:
    """Stack ``value_columns`` into two columns, keeping every other column as an id.

    Row count multiplies by the number of value columns, by construction — that is
    what unpivoting is. Nothing is dropped: a cell that was empty in the wide table is
    an empty row in the long one, because "this customer had no March sales" is a fact
    the user may well be counting.
    """

    present = [column for column in value_columns if column in frame.columns]
    if not present:
        return frame
    id_columns = [
        str(column) for column in frame.columns if str(column) not in set(present)
    ]
    long = frame.melt(
        id_vars=id_columns,
        value_vars=present,
        var_name=variable_name,
        value_name=value_name,
    )
    # melt orders by value column; ordering by the original rows keeps a delivered
    # table readable — all of one customer's months together, as a person would write it.
    if id_columns:
        long = long.sort_values(
            by=id_columns, kind="stable", ignore_index=True
        )
    return long
