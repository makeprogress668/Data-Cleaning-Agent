"""Find where the real column names are, when they are not on the first row.

Chinese business spreadsheets are laid out to be read by people. A title sits on row
one ("2024年销售明细表"), or the header is two rows deep with the top one merged across
groups (订单信息 spanning 订单号 and 客户; 金额 spanning 不含税 and 含税). Reading such
a file with the default ``header=0`` produces columns named ``订单信息``、``订单信息.1``
and turns the actual header into a data row — after which every field reference misses,
no summary can be built, and the run finishes successfully with a workbook that answers
nothing.

The danger in fixing this is doing it too eagerly: a wrong guess mangles an ordinary
file that was fine. So detection only fires on evidence a normal header cannot produce
— duplicate names, or gaps left behind by merged cells — and returns row 0 in every
other case. When in doubt, behave exactly as before.
"""

from __future__ import annotations

import pandas as pd

# Beyond this many rows a "title block" stops being a title block and the file is
# something else entirely — several tables in one sheet, most likely, which is a
# different problem and not one to guess at.
MAX_HEADER_SEARCH_ROWS = 6
# Joins the two levels of a stacked header: 金额 + 含税 -> "金额_含税". Keeping the upper
# level matters when the lower one is only meaningful underneath it — two groups can
# both end in 不含税, and collapsing to the lower level alone would collide.
LEVEL_SEPARATOR = "_"


def _cells(row: pd.Series) -> list[str]:
    return [
        "" if value is None or pd.isna(value) else str(value).strip()
        for value in row.tolist()
    ]


def _is_plausible_header(cells: list[str]) -> bool:
    """A header names every column, once each, in words."""

    filled = [cell for cell in cells if cell]
    if len(filled) != len(cells) or not filled:
        return False
    if len(set(filled)) != len(filled):
        return False
    # A row of numbers is data. Header cells that parse as numbers ("2024") happen, but
    # a row where *every* cell is numeric is not naming anything.
    return not all(_is_number(cell) for cell in filled)


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def _looks_like_upper_level(cells: list[str]) -> bool:
    """Whether a row is the top half of a stacked header rather than a header itself.

    Two shapes say so, and both are what a merged cell leaves behind: repeated values
    (openpyxl repeats them, pandas suffixes them into 金额.1) or blanks between filled
    cells (the merge is stored only in its top-left cell).
    """

    filled = [cell for cell in cells if cell]
    if len(filled) < 2 or all(_is_number(cell) for cell in filled):
        return False
    has_gap = any(not cell for cell in cells)
    has_repeat = len(set(filled)) < len(filled)
    return has_gap or has_repeat


def detect_header(raw: pd.DataFrame) -> tuple[int, int]:
    """Return ``(first_header_row, header_row_count)`` for a header-less read.

    ``(0, 1)`` means "the ordinary case" — the caller should read the file the way it
    always did.
    """

    if raw.empty or raw.shape[1] == 0:
        return 0, 1

    limit = min(MAX_HEADER_SEARCH_ROWS, len(raw))
    for index in range(limit):
        cells = _cells(raw.iloc[index])
        if not any(cells):
            continue  # blank spacer row above the table
        if _is_plausible_header(cells):
            return index, 1
        # A stacked header needs something underneath it. Without this, the two rows of
        # a tiny table — a broken header with a repeated name, then its single data row
        # — read as two header levels and the only record disappears.
        if _looks_like_upper_level(cells) and index + 2 < len(raw):
            below = _cells(raw.iloc[index + 1])
            if _is_plausible_header(below):
                return index, 2
        # A title row ("2024年销售明细表") occupies one cell of a wider sheet.
        if sum(1 for cell in cells if cell) == 1 and raw.shape[1] > 1:
            continue
        break
    return 0, 1


def combine_header_levels(upper: list[str], lower: list[str]) -> list[str]:
    """Merge a two-row header into one name per column.

    The upper level is forward-filled first, because that is what a merged cell means:
    one stored value covers the columns to its right until the next one.

    The group name is then only prepended where the lower name needs it. Under
    订单信息 / 金额, prefixing everything yields 订单信息_订单号 — longer than 订单号,
    no more informative, and harder to match a goal against, which matters because
    field matching is the weakest link in the chain. Two groups that both end in
    不含税 do need it, and those are the columns that get it.
    """

    carried_upper: list[str] = []
    carried = ""
    for index in range(len(lower)):
        upper_name = upper[index] if index < len(upper) else ""
        if upper_name:
            carried = upper_name
        carried_upper.append(carried)

    counts: dict[str, int] = {}
    for name in lower:
        if name:
            counts[name] = counts.get(name, 0) + 1

    names: list[str] = []
    for index, lower_name in enumerate(lower):
        group = carried_upper[index]
        if not lower_name:
            names.append(group or f"列{index + 1}")
        elif counts.get(lower_name, 0) > 1 and group and group != lower_name:
            names.append(f"{group}{LEVEL_SEPARATOR}{lower_name}")
        else:
            names.append(lower_name)
    return _deduplicate(names)


def _deduplicate(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        if name in seen:
            seen[name] += 1
            result.append(f"{name}{LEVEL_SEPARATOR}{seen[name]}")
        else:
            seen[name] = 0
            result.append(name)
    return result


def reframe_with_header(raw: pd.DataFrame) -> pd.DataFrame | None:
    """Re-read a header-less frame using the header it actually has.

    Returns ``None`` when the ordinary ``header=0`` reading is already right, so the
    caller keeps its existing fast path and nothing changes for normal files.
    """

    start, levels = detect_header(raw)
    if start == 0 and levels == 1:
        return None

    if levels == 2:
        names = combine_header_levels(_cells(raw.iloc[start]), _cells(raw.iloc[start + 1]))
    else:
        names = _deduplicate(_cells(raw.iloc[start]))

    body = raw.iloc[start + levels :].reset_index(drop=True)
    body.columns = names
    # Columns that were blank in the header carry no business meaning and would only
    # show up in the delivery as 列3 full of nothing.
    return body.dropna(axis=1, how="all")
