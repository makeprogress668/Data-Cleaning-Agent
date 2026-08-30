"""Stack files that are one dataset split across several uploads.

「把两个月的销售数据合并成一张表」 delivered two rows out of four. The pipeline is
built around one main table plus lookup tables, so the second month was never a lookup
target and simply contributed nothing — no error, no warning, a workbook that looks
exactly like a correct answer with half the records missing. Of every failure found in
this round that is the one a person is least likely to catch.

Twelve monthly exports with identical columns are not twelve tables. They are one
table someone had to split because that is how the source system exports. Reading them
as one is not an extra operation performed on the user's behalf; it is reading the
input correctly. What stays off-limits is guessing: identical columns can also mean
预算 vs 实际, and combining those would be nonsense — so a union either comes from the
goal or gets asked about, never assumed.

Every row keeps ``_source_table``, so which file a record came from survives into the
delivered workbook and the totals stay explainable.
"""

from __future__ import annotations

import pandas as pd

SOURCE_TABLE_COLUMN = "_source_table"


def schema_signature(frame: pd.DataFrame) -> frozenset[str]:
    """The column set that decides whether two tables are the same shape.

    Order is deliberately ignored — the same export run twice can reorder columns —
    but membership is not: a near-match is a different table, and stacking those would
    silently produce a column of mostly-blank values.
    """

    return frozenset(str(column) for column in frame.columns)


def unionable_groups(tables: dict[str, pd.DataFrame]) -> list[list[str]]:
    """Groups of two or more tables that share an identical column set."""

    by_signature: dict[frozenset[str], list[str]] = {}
    for name, frame in tables.items():
        if frame is None or frame.empty and not len(frame.columns):
            continue
        by_signature.setdefault(schema_signature(frame), []).append(str(name))
    return [sorted(names) for names in by_signature.values() if len(names) > 1]


def union_tables(
    tables: dict[str, pd.DataFrame],
    names: list[str],
) -> pd.DataFrame:
    """Concatenate the named tables in the order given, tagging each row's origin."""

    frames: list[pd.DataFrame] = []
    for name in names:
        frame = tables.get(name)
        if frame is None:
            continue
        tagged = frame.copy()
        if SOURCE_TABLE_COLUMN not in tagged.columns:
            tagged.insert(0, SOURCE_TABLE_COLUMN, str(name))
        frames.append(tagged)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
