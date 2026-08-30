"""One vocabulary for keys: what a key column is called, and how its values match.

Three copies of this judgement had drifted apart — the profiler's, the goal
interpreter's and the dirty-data engine's each recognised a different set of tokens.
The profiler's happened to be English-only, which is why Chinese tables produced no
key candidates and cross-table matching silently never fired. Sharing one definition
is what stops that class of bug from coming back a fourth time.
"""

from __future__ import annotations

import re
import unicodedata

# Tokens that mark a column as an identifier. Kept explicit (rather than a clever
# regex) so adding a business vocabulary is a one-line, reviewable change.
KEY_NAME_TOKENS: tuple[str, ...] = (
    "id",
    "key",
    "code",
    "no",
    "number",
    "sitecode",
    "编号",
    "编码",
    "单号",
    "工号",
    "卡号",
    "主键",
    "代码",
    "序号",
    "键",
    # Folded in from the four private copies this module replaced.
    "guid",
    "uuid",
    "license",
)
# Tokens that only read as a key when the name *ends* with them: a column called
# 备注 or 说明 should never qualify just because it contains "号" somewhere.
KEY_NAME_SUFFIXES: tuple[str, ...] = ("id", "code", "key", "编号", "编码", "主键", "键")

_NON_NAME_CHARS = re.compile(r"[^a-z0-9一-鿿]")


def normalize_field_name(name: object) -> str:
    """Lowercase and strip separators, keeping CJK intact.

    An ASCII-only character class here collapsed every Chinese column name to the
    empty string, which is what broke key detection and cross-table matching.
    """

    return _NON_NAME_CHARS.sub("", str(name).lower())


def looks_like_key(name: object) -> bool:
    """Whether a column name reads as an identifier."""

    normalized = normalize_field_name(name)
    if not normalized:
        return False
    if normalized in {"id", "key", "code"}:
        return True
    return any(token in normalized for token in KEY_NAME_TOKENS)


def looks_like_key_suffix(name: object) -> bool:
    """Stricter variant: the name must *end* with an identifier token."""

    normalized = normalize_field_name(name)
    return bool(normalized) and normalized.endswith(KEY_NAME_SUFFIXES)


def normalize_key_value(value: object) -> str:
    """Fold a key *value* to the form two tables can be matched on.

    NFKC maps full-width characters onto their ASCII equivalents. Chinese input methods
    emit them constantly, so the same customer appears as Ｃ002 in one sheet and C002 in
    the other — indistinguishable on screen, and previously unmatched, which quietly
    pushed the order out of the delivered result.

    The profiler and the lookup executor must agree on this, or the match rate shown to
    the user describes a different join than the one that runs.
    """

    return unicodedata.normalize("NFKC", str(value)).strip().lower()


# Columns that hold dates. The same judgement was written four times — the goal
# interpreter's field families, the aggregation's time bucket, the analysis module's
# column classifier and the dirty-data scanner — with a different word list each time.
DATE_NAME_TOKENS: tuple[str, ...] = (
    "date",
    "time",
    "day",
    "month",
    "year",
    "日期",
    "时间",
    "月份",
    "年度",
    "日",
    "月",
    "年",
)


def looks_like_date_field(name: object) -> bool:
    """Whether a column name reads as holding dates."""

    lowered = str(name).lower()
    return any(token in lowered for token in DATE_NAME_TOKENS)
