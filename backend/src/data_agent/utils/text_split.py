"""Split one column into several, the way Excel's 分列 does.

「把地址拆成省、市、区」 is one of the most ordinary things anyone does to a
spreadsheet, and users rarely name the delimiter — they say 「拆开」 and expect the
tool to look at the data. So the separator is inferred from the column's own values
when the goal does not state one, and inference only succeeds when the evidence is
unambiguous: the same delimiter, the same number of parts, in nearly every row.

A column that splits into three parts in some rows and five in others is not a
delimited field, it is prose. Splitting it produces columns that mean different
things per row, which is worse than not splitting at all.
"""

from __future__ import annotations

from collections import Counter

import pandas as pd

# Delimiters worth considering, in the order a tie is broken. Chinese business data
# uses full-width punctuation as readily as ASCII.
CANDIDATE_SEPARATORS: tuple[str, ...] = (
    "|", "-", "/", "\\", "_", ",", "，", ";", "；", "、", " ", "－", "—",
)
# Below this share of rows agreeing on the same part count, the column is not a
# delimited field and inference declines rather than guessing.
MIN_CONSISTENCY = 0.9
MAX_PARTS = 10


def infer_separator(values: pd.Series) -> tuple[str, int]:
    """Return ``(separator, part_count)``, or ``("", 0)`` when nothing fits.

    Declining is a real outcome: the caller then asks the user instead of splitting a
    column into pieces that do not line up.
    """

    text = [
        str(value).strip()
        for value in values.dropna().tolist()
        if str(value).strip()
    ]
    if len(text) < 2:
        return "", 0

    best: tuple[str, int, float] = ("", 0, 0.0)
    for separator in CANDIDATE_SEPARATORS:
        counts = Counter(item.count(separator) for item in text)
        occurrences, agreeing = counts.most_common(1)[0]
        if occurrences < 1 or occurrences + 1 > MAX_PARTS:
            continue
        consistency = agreeing / len(text)
        if consistency >= MIN_CONSISTENCY and consistency > best[2]:
            best = (separator, occurrences + 1, consistency)
    return best[0], best[1]


def split_part_names(source: str, count: int, wanted: list[str] | None = None) -> list[str]:
    """Names for the produced columns.

    Names the user wrote win outright — 「拆成省、市、区」 says what each piece is, and
    no generated name beats that. Otherwise they are numbered after the source column,
    which at least says where they came from.
    """

    if wanted:
        return list(wanted[:count])
    return [f"{source}_{index + 1}" for index in range(count)]
