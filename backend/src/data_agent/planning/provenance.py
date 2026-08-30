"""Where a row-removing action came from, and whether the user actually asked for it.

Authorization used to be decided by a verb table: ``_authorises_filtering`` scanned the
goal for 删/去掉/剔除…, and a phrasing outside the list dropped the action entirely —
「把作废的订单去掉」 worked, 「把作废的订单清理一下」 silently did nothing. Earlier still,
a miss went the other way and deleted rows nobody asked to delete. Vocabulary lists
cannot be completed; every business dialect adds another way to say "remove these".

This module replaces the verb table with a check that does not depend on one. An action
is authorized when it carries a quote from the user's own goal. The model may understand
freely — 「把重复的记录清理掉」 needs no keyword — but it has to point at the words it
understood, and a quote that is not in the goal is not authorization. The check itself
stays deterministic and auditable: substring containment, nothing more.

Three sources, because "the user did not say this" and "this is plumbing" are different:

``stated``    the user's own words ask for it — authorized; impact may still escalate
``implied``   mechanically required by something stated (a numeric cast before a sum)
``inferred``  nobody asked; the system thought of it — drop it, then ask
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

import pandas as pd

from data_agent.schemas.task import ActionAuthorization

STATED: ActionAuthorization = "stated"
IMPLIED: ActionAuthorization = "implied"
INFERRED: ActionAuthorization = "inferred"

# Set on requirements the deterministic parser read straight out of the goal string.
# Those are quotes by construction, so they never need a separate evidence check.
GOAL_TEXT_SOURCE = "goal_text"
LLM_SOURCE = "llm"

# A quote shorter than this cannot distinguish intent from coincidence: 「的」 appears in
# almost every Chinese goal. Two characters is the shortest real instruction (去重, 删掉).
_MIN_QUOTE_CHARS = 2

# Dropped before comparing, so a model that re-typed the user's punctuation or spacing
# still matches. Content characters are never folded away — only the separators are.
_IGNORED_IN_QUOTE = re.compile(r"[\s　,，.。;；:：!！?？、'\"“”‘’()（）\[\]【】<>《》-]+")
_OPERATION_SCOPE_BREAKS = re.compile(r"[。；;\n]")
_NEGATION_MARKERS: tuple[str, ...] = (
    "不要",
    "不需要",
    "无需",
    "无须",
    "不用",
    "不得",
    "禁止",
    "别",
    "勿",
    "do not",
    "don't",
    "without",
    "no ",
)
_NEGATION_RESETS: tuple[str, ...] = (
    "但是",
    "但要",
    "但需",
    "不过",
    "而是",
    "改为",
    "改成",
    "but ",
    "except ",
)


def normalize_quote(text: Any) -> str:
    """Fold a quote to its content characters for containment comparison."""

    folded = unicodedata.normalize("NFKC", str(text)).strip().lower()
    return _IGNORED_IN_QUOTE.sub("", folded)


def verify_evidence(evidence: Any, goal: str, *, names: Iterable[str] = ()) -> bool:
    """True when ``evidence`` is really a quote from ``goal`` that asks for something.

    ``names`` are the table and column names in play. A field name is not an
    instruction: quoting 「订单」 out of 「订单明细」 says nothing about whether the user
    wanted rows deleted, so a quote that lives entirely inside a name is rejected.
    """

    quote = normalize_quote(evidence)
    if len(quote) < _MIN_QUOTE_CHARS or quote not in normalize_quote(goal):
        return False
    return not any(quote in normalize_quote(name) for name in names if str(name).strip())


def operation_is_negated(goal: str, tokens: Iterable[str]) -> bool:
    """Whether every mention of an operation sits inside an active negation scope.

    Business users coordinate exclusions: ``不要删除、去重、填空或改写`` negates every
    listed action even though ``不要`` appears only once. A plain substring check reads
    ``去重`` as authorization. Sentence boundaries end the scope; contrast words such
    as ``但要`` reset it, so ``不要填空，但要去重`` still requests deduplication.
    """

    normalized_tokens = [str(token).lower() for token in tokens if str(token).strip()]
    mentions: list[bool] = []
    for clause in _OPERATION_SCOPE_BREAKS.split(str(goal).lower()):
        for token in normalized_tokens:
            start = 0
            while (position := clause.find(token, start)) >= 0:
                mentions.append(_prefix_is_negated(clause[:position]))
                start = position + max(1, len(token))
    return bool(mentions) and all(mentions)


def _prefix_is_negated(prefix: str) -> bool:
    marker = max((prefix.rfind(item) for item in _NEGATION_MARKERS), default=-1)
    stripped = prefix.rstrip()
    if stripped.endswith(("不", "勿")):
        marker = max(marker, len(stripped) - 1)
    reset = max((prefix.rfind(item) for item in _NEGATION_RESETS), default=-1)
    return marker >= 0 and marker > reset


def authorization_of(
    requirement: dict[str, Any],
    goal: str,
    *,
    names: Iterable[str] = (),
    goal_authorises: bool = False,
) -> ActionAuthorization:
    """Classify one destructive requirement as stated / implied / inferred.

    ``goal_authorises`` carries the old verb-table verdict. It is kept as a *second way
    to grant*, never as a way to deny: a model proposal that quotes the goal is
    authorized even when no listed verb appears, and one that quotes nothing can still
    ride along on a verb the table did recognise. Because both paths only ever say yes,
    nothing runs here that the verb table alone would not already have run.
    """

    if str(requirement.get("source") or "") == GOAL_TEXT_SOURCE:
        return STATED
    if verify_evidence(requirement.get("evidence"), goal, names=names):
        return STATED
    return STATED if goal_authorises else INFERRED


def is_authorized(requirement: dict[str, Any]) -> bool:
    """Whether a requirement carries authorization to run without asking again."""

    return str(requirement.get("authorization") or INFERRED) in {STATED, IMPLIED}


# Ways a business user says "these files are one table". Same role as every other
# token list here: one of two ways to grant, never a way to deny — a phrasing it misses
# can still be authorized by a quote from the goal.
UNION_TOKENS: tuple[str, ...] = (
    "合并", "汇总到一起", "汇总成一张", "拼接", "拼起来", "放到一张表", "放在一起",
    "整合", "纵向", "叠加", "追加", "合成一张", "并成一张", "union", "combine", "append",
)


def wants_table_union(goal: str) -> bool:
    """Whether the goal asks for several same-shaped tables to be read as one."""

    lowered = str(goal).lower()
    return any(token in lowered or token in goal for token in UNION_TOKENS)


# Ways a user asks for the fill to arrive as a live formula rather than a value.
# "用VLOOKUP匹配" is a request about the *deliverable*, not about the algorithm: the
# values would be identical either way, but a formula can be clicked, read and
# recalculated — it verifies itself, which is why no separate 核对 sheet is needed.
FORMULA_OUTPUT_TOKENS: tuple[str, ...] = (
    "vlookup", "xlookup", "index/match", "公式", "函数",
    "写成公式", "保留公式", "带公式", "用公式",
)


def wants_formula_output(goal: str) -> bool:
    """Whether the delivered workbook should carry formulas instead of plain values."""

    lowered = str(goal).lower()
    return any(token in lowered or token in goal for token in FORMULA_OUTPUT_TOKENS)


# Ways a user asks to be told what the run changed. Without one of these the account
# is not wanted: a cleaning delivery used to carry a 本次改动 sheet nobody requested,
# and every extra sheet costs the reader something.
CHANGE_MANIFEST_TOKENS: tuple[str, ...] = (
    "本次改动", "改动清单", "改了什么", "改了哪些", "修改记录", "变更记录", "变更清单",
    "处理记录", "说明改动", "列出改动", "哪些数据被修改", "改动说明", "change log",
    "changelog",
)


def wants_change_manifest(goal: str) -> bool:
    """Whether the user asked for an account of what this run changed."""

    lowered = str(goal).lower()
    return any(token in lowered or token in goal for token in CHANGE_MANIFEST_TOKENS)


def schema_names(tables: dict[str, pd.DataFrame]) -> set[str]:
    """Every table and column name, so a quote made only of them can be rejected."""

    return {str(name) for name in tables} | {
        str(column) for table in tables.values() for column in table.columns
    }
