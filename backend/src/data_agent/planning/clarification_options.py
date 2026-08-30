"""Turn every clarification into a multiple-choice question.

The mature products in this space do not ask open questions. Cortex Analyst returns
suggested questions, WrenAI offers three candidate readings to pick from, AmbiSQL uses
multiple choice. The reason is the same everywhere: a business user who could write
「按 status 字段，保留 paid」 would not have needed to be asked. Given a blank box they
answer vaguely or abandon the task; given four buttons they answer correctly.

So the options come from the data, never from a fixed list. Which columns can actually
be filtered on, which look like keys, which make sense as a dimension — that is a
question about this upload, and it changes with every upload.
"""

from __future__ import annotations

import pandas as pd

from data_agent.utils.field_names import looks_like_date_field, looks_like_key

# A filter criterion is almost always a low-cardinality column: 状态、类型、区域. An
# 订单号 column has one value per row and filtering on it one value at a time is not
# what anybody means.
MAX_FILTER_DISTINCT = 20
# A grouping dimension with more distinct values than this produces a "summary" as long
# as the detail table, which answers nothing.
MAX_GROUP_DISTINCT = 50
# Options past this point stop being a choice and become a list to read.
MAX_OPTIONS = 6

WHOLE_ROW_DUPLICATE = "整行完全相同才算重复"
NO_GROUPING = "不分组，只要总计"


def _frames(
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> list[pd.DataFrame]:
    if primary and primary in tables:
        return [tables[primary]]
    return list(tables.values())


def filter_field_options(
    tables: dict[str, pd.DataFrame],
    primary: str | None = None,
) -> list[str]:
    """Columns a retention rule could plausibly be written against.

    Phrased as 「字段=值」 rather than a bare column name, because the answer the run
    needs is a condition, and a user picking 「状态」 has still not said which status.
    """

    options: list[str] = []
    for frame in _frames(tables, primary):
        for column in frame.columns:
            name = str(column)
            if name.startswith("_") or looks_like_key(name):
                continue
            values = frame[column].dropna()
            distinct = values.unique()
            if 0 < len(distinct) <= MAX_FILTER_DISTINCT and len(distinct) < len(frame):
                sample = str(distinct[0])
                options.append(f"{name} = {sample}")
    return options[:MAX_OPTIONS]


def dedup_key_options(
    tables: dict[str, pd.DataFrame],
    primary: str | None = None,
) -> list[str]:
    """Columns that could identify a duplicate, plus Excel's own default.

    「整行完全相同」 is what Excel's 删除重复项 does with every column ticked, so it
    belongs in the list even when a key-like column exists — it is often what the user
    means and they should not have to describe it.
    """

    options: list[str] = []
    for frame in _frames(tables, primary):
        for column in frame.columns:
            name = str(column)
            if not name.startswith("_") and looks_like_key(name) and name not in options:
                options.append(name)
    return [*options[: MAX_OPTIONS - 1], WHOLE_ROW_DUPLICATE]


def group_by_options(
    tables: dict[str, pd.DataFrame],
    primary: str | None = None,
) -> list[str]:
    """Dimensions worth grouping by, plus the option of not grouping at all.

    A grand total is a real answer to 「统计一下」 and is frequently the intended one,
    so it is offered rather than left for the user to think of.
    """

    options: list[str] = []
    for frame in _frames(tables, primary):
        for column in frame.columns:
            name = str(column)
            if name.startswith("_"):
                continue
            if pd.api.types.is_numeric_dtype(frame[column]) and not looks_like_date_field(name):
                continue  # a measure, not a dimension
            distinct = frame[column].dropna().unique()
            # Cardinality decides, not the name. 「按客户统计」 groups by 客户编号 — a
            # key-like name is a perfectly ordinary dimension in a fact table. What
            # cannot be grouped is a pure identifier: one value per row, so grouping by
            # it returns the detail table under another title.
            if len(distinct) >= len(frame) > 1:
                continue
            if 0 < len(distinct) <= MAX_GROUP_DISTINCT and name not in options:
                options.append(name)
    return [*options[: MAX_OPTIONS - 1], NO_GROUPING]


def lead_with_remembered(options: list[str], remembered: list[str]) -> list[str]:
    """Put previously chosen options first, keeping everything else in place.

    Asking again is not the failure — a dimension that mattered last month still needs
    confirming this month. What improves is the question: 「按什么统计？」 with 「城市」
    at the top, because that is what this user has meant every previous time.

    A remembered choice that is not among this upload's options is dropped. Last
    month's column may simply not be in this file, and leading with a name the data
    cannot deliver would be worse than not remembering at all.
    """

    if not remembered:
        return options
    available = list(options)
    leading = [choice for choice in remembered if choice in available]
    return [*leading, *[item for item in available if item not in leading]]
