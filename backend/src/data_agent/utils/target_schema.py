"""The shape of the table the user wants back.

Three ways a user says what the deliverable should look like, and they are the same
request underneath — an ordered list of columns, each filled from wherever it lives:

  上传一张空模板            表头就是列清单
  在目标里点名列            「做一张表，包含订单号、客户名称、所属大区」
  以某张表为底再加列        现有的关联行为

Only the first two need anything new. Both produce a schema; the executor then
projects the result onto it.

The uploaded template is never modified — it is read for its headers and nothing
else. Its column names and order carry into the deliverable exactly as written,
because that file usually exists to be fed somewhere afterwards, and a renamed or
reordered column breaks whatever waits downstream.
"""

from __future__ import annotations

import re

import pandas as pd

# A template holds headers and nothing else. Allowing "one or two sample rows" sounds
# accommodating and is the wrong direction: a genuine three-row data table would then
# be read as a form, and the deliverable would be projected onto its columns — a much
# worse outcome than failing to notice a template that still has an example line in it.
# Blank-but-present rows are fine; a row with any value in it is data.
# Introduces a list of wanted columns: "做一张表，包含订单号、客户名称、金额".
_SCHEMA_PREFIXES: tuple[str, ...] = (
    "包含", "包括", "字段有", "字段为", "列有", "列为", "需要字段", "需要列",
    "表头是", "表头为", "含", "带上",
)
_LIST_SEPARATORS = re.compile(r"[、,，/和跟及与\s]+")


def looks_like_template(frame: pd.DataFrame) -> bool:
    """Whether an uploaded table is a blank form rather than data."""

    if frame is None or frame.shape[1] == 0:
        return False
    if len(frame) and frame.notna().to_numpy().any():
        return False
    named = [
        str(column).strip()
        for column in frame.columns
        if str(column).strip() and not str(column).startswith("Unnamed:")
    ]
    # A sheet whose headers are mostly unnamed is a fragment, not a form to fill.
    return len(named) >= 2 and len(named) >= frame.shape[1] - 1


def template_schema(tables: dict[str, pd.DataFrame]) -> tuple[str, list[str]]:
    """The uploaded template's name and its column list, or ("", []) if there is none.

    Only one template is honoured. Two blank forms in one upload is a different
    request — which one is the target? — and guessing between them is exactly the
    kind of silent choice that produces a confident wrong answer.
    """

    candidates = [
        (str(name), [str(column) for column in frame.columns])
        for name, frame in tables.items()
        if looks_like_template(frame)
    ]
    if len(candidates) != 1:
        return "", []
    return candidates[0]


def goal_schema(goal: str, available: set[str]) -> list[str]:
    """Columns the goal lists after 「包含 / 字段有 / 列为」, in the order written.

    Every name is checked against the real columns: a schema is a promise that the
    delivered table has those columns, and inventing one turns the promise into an
    empty column the user has to explain.
    """

    text = str(goal)
    for prefix in _SCHEMA_PREFIXES:
        position = text.find(prefix)
        if position < 0:
            continue
        tail = text[position + len(prefix) :]
        # The list ends at the first sentence break.
        tail = re.split(r"[。；;\n]", tail)[0]
        wanted = [
            token
            for token in (part.strip() for part in _LIST_SEPARATORS.split(tail))
            if token in available
        ]
        if len(wanted) >= 2:
            return wanted
    return []


def project_onto_schema(
    delivered: pd.DataFrame,
    schema: list[str],
) -> pd.DataFrame:
    """Return the delivered rows with exactly ``schema``'s columns, in that order.

    A column the run could not fill stays present and empty rather than missing: the
    user asked for that shape, and a form with a blank column is something they can
    act on, while a form with a column silently dropped is not.
    """

    if not schema:
        return delivered
    result = pd.DataFrame(index=delivered.index)
    for column in schema:
        result[column] = delivered[column] if column in delivered.columns else ""
    return result.reset_index(drop=True)


def is_blank_column(values: pd.Series) -> bool:
    """Whether a column carries nothing a user could act on.

    ``astype(str)`` renders a missing value as the four characters "nan", which is
    perfectly truthy — a check written that way reports every empty column as filled,
    which is how the unfilled-column count sat at zero while columns sat empty.
    """

    if values.empty:
        return True
    text = values.where(values.notna(), "").astype(str).str.strip()
    return not text.any()
