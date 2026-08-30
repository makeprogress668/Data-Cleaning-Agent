"""Read numbers the way business users actually write them.

A 金额 column arriving from an ERP export, a CSV round-trip or a copy-paste is very
often text: ``1,234``、``(567)``、``￥2,000``、``1.2万``. ``pd.to_numeric`` turns every
one of those into NaN, and a NaN inside ``sum()`` is silently zero — so 「按城市统计
金额合计」 produced 北京 0、上海 0, a summary table that exists, looks perfectly
normal, and is entirely wrong. That is worse than refusing to answer: a missing sheet
gets noticed, a plausible 0 does not.

Excel is the benchmark. Paste ``1,234`` into a cell and SUM counts it; write
``(567)`` in an accounting-formatted cell and it is negative five hundred and
sixty-seven. This module is what makes an aggregation agree with the spreadsheet the
user is checking it against.

Parsing here is for *computing only*. The delivered rows keep whatever the user
uploaded — cleaning a cell nobody asked to clean is a different decision, and not this
module's to make.
"""

from __future__ import annotations

import re
import unicodedata

import pandas as pd

# Suffix multipliers. 万/亿 are how Chinese business documents write large amounts, and
# "1.2万" read as 1.2 is off by four orders of magnitude — a silent error large enough
# to invert a business conclusion.
_UNIT_MULTIPLIERS: tuple[tuple[str, float], ...] = (
    ("亿", 1e8),
    ("万", 1e4),
    ("k", 1e3),
    ("千", 1e3),
    ("百万", 1e6),
)
# Symbols and unit words that decorate an amount without changing it.
_STRIP_TOKENS: tuple[str, ...] = (
    "￥", "¥", "$", "€", "£", "rmb", "cny", "usd",
    "元", "块", "人民币", "美元",
    ",", " ", " ", "　",
)
# (123) is the accounting notation for a negative amount. Excel's own currency and
# accounting formats produce it, so a column exported from any finance system is full
# of them.
_ACCOUNTING_NEGATIVE = re.compile(r"^\(\s*(.+?)\s*\)$")


def parse_business_number(value: object) -> float | None:
    """Parse one cell, or return ``None`` when it is not a number at all."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)

    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    if not text:
        return None

    # Decorations come off first: Excel's 会计专用 format writes a negative amount as
    # ¥(567.00), so the currency symbol sits *outside* the parentheses and an anchored
    # ^\(...\)$ test never matches. Stripping first makes the two formats compose.
    for token in _STRIP_TOKENS:
        text = text.replace(token, "")

    negative = False
    match = _ACCOUNTING_NEGATIVE.match(text)
    if match:
        negative, text = True, match.group(1).strip()

    # Percent keeps the convention the analysis layer already used: "85%" reads as 85,
    # because a user summing a 占比 column is adding the numbers they can see.
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()

    multiplier = 1.0
    for unit, factor in _UNIT_MULTIPLIERS:
        if text.endswith(unit):
            text, multiplier = text[: -len(unit)].strip(), factor
            break

    if not text or text in {"-", "+", "."}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number * multiplier if negative else number * multiplier


def to_business_numeric(series: pd.Series) -> pd.Series:
    """Coerce a column to numbers, understanding business notation.

    Already-numeric columns pass straight through: the text path exists for data that
    arrived as strings, and running it over real numbers would only risk mangling them.
    """

    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    parsed = pd.to_numeric(series, errors="coerce")
    unparsed = parsed.isna() & series.notna()
    if not unparsed.any():
        return parsed
    return parsed.where(~unparsed, series.where(unparsed).map(parse_business_number))


def unparseable_count(series: pd.Series) -> int:
    """How many non-blank cells could not be read as a number.

    An aggregate built from 12 of 15 values is a different answer than the user thinks
    they asked for, so the count travels with the number rather than being discarded.
    """

    if pd.api.types.is_numeric_dtype(series):
        return 0
    blank = series.isna() | series.astype(str).str.strip().eq("")
    return int((to_business_numeric(series).isna() & ~blank).sum())
