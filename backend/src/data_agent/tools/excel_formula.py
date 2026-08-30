"""Deliver the lookup as a formula, the way the user would have written it.

「用 VLOOKUP 把客户名称匹配过来」 is not a request for the same values by another
route — it is a request for a workbook whose filled column *shows its work*. Click the
cell, read ``=VLOOKUP($B2,客户档案!$A:$C,3,FALSE)``, and the fill is verified the same
way the user verifies their own spreadsheet. No 核对 sheet, no 说明 column, nothing to
read: the formula is the proof, and it recalculates if the source data changes.

This is why the source table ships in the same workbook when formulas are requested.
A formula pointing at a table that is not there is not a deliverable — the dependency
is part of what was asked for, not an extra sheet nobody wanted.

VLOOKUP can only look rightward from its key. When the wanted column sits to the left
of the key in the source table, INDEX/MATCH is what an Excel user writes instead, so
that is what gets written.
"""

from __future__ import annotations

from openpyxl.utils import get_column_letter

# Excel's own argument separator differs by locale, but the file format always stores
# the comma form; Excel renders it with the user's separator on open.
_SEP = ","


def column_letter(columns: list[str], name: str) -> str:
    """Excel column letter for ``name`` within a sheet written from ``columns``."""

    return get_column_letter(columns.index(str(name)) + 1)


def lookup_formula(
    *,
    result_columns: list[str],
    left_key: str,
    source_sheet: str,
    source_columns: list[str],
    right_key: str,
    value_field: str,
    row: int,
) -> str:
    """One cell's formula. ``row`` is the 1-based worksheet row (header is row 1)."""

    key_cell = f"${column_letter(result_columns, left_key)}{row}"
    key_col = column_letter(source_columns, right_key)
    value_col = column_letter(source_columns, value_field)
    sheet = _quote_sheet(source_sheet)

    key_index = source_columns.index(str(right_key))
    value_index = source_columns.index(str(value_field))
    if value_index > key_index:
        span = f"{sheet}!${key_col}:${value_col}"
        offset = value_index - key_index + 1
        return f"=VLOOKUP({key_cell}{_SEP}{span}{_SEP}{offset}{_SEP}FALSE)"

    # The wanted column is left of the key: VLOOKUP cannot reach backwards.
    return (
        f"=INDEX({sheet}!${value_col}:${value_col}{_SEP}"
        f"MATCH({key_cell}{_SEP}{sheet}!${key_col}:${key_col}{_SEP}0))"
    )


def _quote_sheet(name: str) -> str:
    """Excel needs quotes around a sheet name containing spaces or punctuation."""

    text = str(name)
    if any(char in text for char in " -()'[]{}!"):
        return "'" + text.replace("'", "''") + "'"
    return text


def formula_column(
    *,
    row_count: int,
    result_columns: list[str],
    left_key: str,
    source_sheet: str,
    source_columns: list[str],
    right_key: str,
    value_field: str,
) -> list[str]:
    """The whole filled column, one formula per data row."""

    return [
        lookup_formula(
            result_columns=result_columns,
            left_key=left_key,
            source_sheet=source_sheet,
            source_columns=source_columns,
            right_key=right_key,
            value_field=value_field,
            row=index + 2,  # row 1 holds the header
        )
        for index in range(row_count)
    ]
