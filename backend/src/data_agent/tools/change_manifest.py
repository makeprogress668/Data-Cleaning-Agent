"""What this run actually did to the user's data.

The engine already recorded every edit and every removal, but only into the internal
audit workbook — the user had no way to check the one thing that matters most about
handing their spreadsheet to an agent: that nothing else was touched. Trust here is
not a promise in the docs, it is a list you can read in thirty seconds.

So this is deliberately a *summary*, not the cell-by-cell audit: one line per kind of
change, with a count and one real example. A per-cell dump of a 100k-row table answers
the question by burying it.
"""

from __future__ import annotations

import pandas as pd

from data_agent.schemas.output import CHANGE_MANIFEST_ARTIFACT
from data_agent.tools.issue_wording import display_dirty_issue_name

__all__ = ["CHANGE_MANIFEST_ARTIFACT", "MANIFEST_COLUMNS", "build_change_manifest"]
MANIFEST_COLUMNS = ["改动类型", "字段", "影响行数", "说明"]

_MAX_EXAMPLE_CHARS = 60


def build_change_manifest(
    fix_audit: pd.DataFrame,
    removed_row_count: int = 0,
    removal_reason: str = "",
) -> pd.DataFrame:
    """One row per kind of change, most-affected first."""

    rows: list[dict[str, object]] = []
    if removed_row_count > 0:
        rows.append(
            {
                "改动类型": "删除行",
                "字段": "-",
                "影响行数": int(removed_row_count),
                "说明": removal_reason or "按目标条件移除",
            }
        )
    rows.extend(_edit_rows(fix_audit))
    if not rows:
        return pd.DataFrame(columns=MANIFEST_COLUMNS)
    return pd.DataFrame(rows, columns=MANIFEST_COLUMNS)


def _edit_rows(fix_audit: pd.DataFrame) -> list[dict[str, object]]:
    required = {"field", "fix_type", "before_value", "after_value"}
    if fix_audit.empty or not required.issubset(fix_audit.columns):
        return []

    grouped = fix_audit.groupby(["fix_type", "field"], dropna=False)
    rows = [
        {
            "改动类型": display_dirty_issue_name(fix_type),
            "字段": str(field),
            "影响行数": int(len(group)),
            "说明": _example(group),
        }
        for (fix_type, field), group in grouped
    ]
    rows.sort(key=lambda row: (-int(row["影响行数"]), str(row["字段"])))
    return rows


def _example(group: pd.DataFrame) -> str:
    """One real before/after pair, so the line can be spot-checked against the file."""

    first = group.iloc[0]
    before = _readable(first.get("before_value"))
    after = _readable(first.get("after_value"))
    return f"{before} → {after}"


def _readable(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "(空)"
    text = str(value)
    if text != text.strip() or not text:
        # Whitespace edits are the most common kind here and are invisible without
        # quotes — "客户编号 →  客户编号" reads as a no-op.
        text = f"「{text}」"
    if len(text) > _MAX_EXAMPLE_CHARS:
        text = text[:_MAX_EXAMPLE_CHARS] + "…"
    return text
