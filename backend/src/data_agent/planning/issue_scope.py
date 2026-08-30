"""Which problems the user actually asked to be shown.

Asking "列出关联不上的异常记录供我复核" is a request about one thing. What came back
was every finding ten detectors could produce — 备注 为空, duplicate_客户编号 (a foreign
key in a detail table repeats by definition), 数字存成文本, 数值极端值 — and because
*every* row carried at least one of them, every row was held back and the delivery
came out empty. The one line the user actually wanted, 关联数据未匹配, was buried in
the middle of it.

So the goal names a scope, and that scope governs both what the review sheet reports
and which rows are withheld. Naming no particular problem still means "show me
whatever you found" — the difference is that it is then the user's choice.
"""

from __future__ import annotations

# Issue kind -> the words a business user uses for it. Kept as plain vocabulary
# because the alternative — asking the user to configure issue categories — is the
# kind of ceremony this product exists to avoid.
ISSUE_SCOPE_TOKENS: dict[str, tuple[str, ...]] = {
    "unmatched_lookup": (
        "关联不上",
        "关联不到",
        "匹配不上",
        "匹配不到",
        "未匹配",
        "没匹配",
        "关联失败",
        "匹配失败",
        "找不到对应",
        "查不到",
        "unmatched",
    ),
    "missing": (
        "缺失",
        "为空",
        "空值",
        "漏填",
        "没填",
        "未填",
        "缺数据",
        "missing",
    ),
    "duplicate": ("重复", "重号", "撞号", "duplicate"),
    "negative": ("负数", "为负", "小于零", "小于 0", "negative"),
    "outlier": ("异常值", "极端值", "离群", "outlier"),
    "date": ("日期错", "日期格式", "日期无效", "时间格式"),
    "format": ("格式错", "格式不对", "格式问题", "存成文本", "数字文本"),
}

# Issue kind -> the raw labels the detectors emit for it.
_UNMATCHED_SUFFIX = "_not_found"
_ISSUE_KIND_TYPES: dict[str, frozenset[str]] = {
    "missing": frozenset({"null_values", "empty_strings"}),
    "duplicate": frozenset(
        {"duplicate_rows", "duplicate_key", "duplicate_columns", "case_mixed_duplicates"}
    ),
    "negative": frozenset({"negative_amount", "negative_numeric_value"}),
    "outlier": frozenset({"numeric_outlier"}),
    "date": frozenset({"date_parse_failed", "mixed_date_formats"}),
    "format": frozenset(
        {"numeric_stored_as_text", "fullwidth_characters", "invisible_characters"}
    ),
}


# Words that mean "show me what went wrong". Owned here rather than in the goal
# interpreter because this module is the leaf both the planner and the executor read.
PROBLEM_REPORT_TOKENS: tuple[str, ...] = (
    "异常",
    "复核",
    "不能",
    "无法",
    "失败",
    "校验",
    "错误",
    "问题",
    "可疑",
    "review",
    "invalid",
    "error",
)


# Asking *about* problems and asking for records to be *set aside* are different
# requests. A generic "列出异常" authorises the problem view, not row withholding.
# These phrases name a distinct usable/normal result or an explicit review handoff;
# semantic paraphrases outside this deterministic fallback are admitted only through
# the model's verbatim capability evidence.
ROW_REVIEW_TOKENS: tuple[str, ...] = (
    "供我复核",
    "供人工复核",
    "待处理",
    "需要处理的",
    "单独说明",
    "单独列出",
    "单独输出",
    "分开输出",
    "拆分输出",
    "保留可用",
    "只保留可用",
    "输出可用",
    "输出正常",
    "review separately",
)


def wants_problem_report(goal: str) -> bool:
    """Whether the goal asks to be shown problems at all."""

    return any(token in goal.lower() or token in goal for token in PROBLEM_REPORT_TOKENS)


def wants_row_review(goal: str) -> bool:
    """Whether the goal asks for problem records to be held out of the delivery."""

    lowered = goal.lower()
    return any(token in lowered or token in goal for token in ROW_REVIEW_TOKENS)


def requested_issue_kinds(goal: str) -> frozenset[str] | None:
    """The problem scope a goal declares.

    Three states, and the difference between the first two matters:
      * ``None``  — problems were never mentioned. Report nothing.
      * ``set()`` — problems were asked about generally. Report everything found.
      * ``{...}`` — particular problems were named. Report only those.
    """

    kinds = frozenset(
        kind
        for kind, tokens in ISSUE_SCOPE_TOKENS.items()
        if any(token in goal for token in tokens)
    )
    # Naming a concrete issue is itself a request about that issue. Requiring a second
    # generic word such as 问题/异常 made "标记重复记录" authorize an annotation action
    # while authorizing no duplicate finding to populate it.
    if not wants_problem_report(goal) and not kinds:
        return None
    return kinds


def reason_in_scope(reason: str, kinds: frozenset[str] | None) -> bool:
    """Whether one flagged reason falls inside the scope the user asked about."""

    if kinds is None:
        # Nobody asked. Volunteering a list of findings about data the user did not
        # ask to be inspected is the same overreach as editing it uninvited.
        return False
    if not kinds:
        return True
    if reason.endswith(_UNMATCHED_SUFFIX):
        return "unmatched_lookup" in kinds
    issue_type = reason.rpartition(":")[2] if reason.startswith("dirty:") else reason
    for kind in kinds:
        if issue_type in _ISSUE_KIND_TYPES.get(kind, frozenset()):
            return True
        if kind == "duplicate" and issue_type.startswith("duplicate_"):
            # Recommender rules carry the business key in their reason label
            # (duplicate_订单号), not the generic detector id duplicate_key.
            return True
        # Rule-driven reasons carry their own wording (e.g. "amount_null_values"),
        # so fall back to matching the label's tail against the same type table.
        if any(issue_type.endswith(known) for known in _ISSUE_KIND_TYPES.get(kind, ())):
            return True
    return False
