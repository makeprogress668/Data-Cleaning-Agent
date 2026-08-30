"""Build a TaskSpec from the merged deterministic/LLM goal understanding."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, NamedTuple

import pandas as pd

from data_agent.agent.memory import remembered_choices
from data_agent.planning.clarification_options import (
    dedup_key_options,
    filter_field_options,
    group_by_options,
    lead_with_remembered,
)
from data_agent.planning.goal_interpreter import _destination_table, derive_capabilities
from data_agent.planning.provenance import (
    GOAL_TEXT_SOURCE,
    INFERRED,
    LLM_SOURCE,
    STATED,
    authorization_of,
    operation_is_negated,
    schema_names,
    wants_change_manifest,
    wants_table_union,
)
from data_agent.schemas.task import MissingSlot, MissingSlotKind, TaskAction, TaskSpec
from data_agent.tools.table_union import unionable_groups
from data_agent.utils.collections import unique
from data_agent.utils.field_names import looks_like_date_field, looks_like_key
from data_agent.utils.numeric import to_business_numeric
from data_agent.utils.reshape import wide_value_columns
from data_agent.utils.target_schema import goal_schema, template_schema
from data_agent.utils.text_split import infer_separator, split_part_names


def build_task_spec(
    goal: str,
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> TaskSpec:
    """Translate existing goal evidence into the single validated intent contract."""

    table_names = {str(name) for name in tables}
    primary = str(goal_plan.get("suggested_base_table") or "").strip() or None
    if primary not in table_names:
        primary = None

    target_fields = _target_fields(goal_plan, tables)
    target_tables = _target_tables(goal_plan, table_names, primary, target_fields)
    # Every capability-driven action (clean / lookup / analyze / review) is read from
    # here. A goal_plan without them produced a TaskSpec claiming only "export" — the
    # user's request silently reduced to "hand the file back". Deriving them rather than
    # defaulting to {} means a caller cannot accidentally strip the task down to nothing.
    capabilities = goal_plan.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        capabilities = derive_capabilities(goal, tables)
    union = _union_requirements(goal, tables)
    rollups = _rollup_requirements(goal, tables, primary)
    melt = _melt_requirement(goal, tables, primary)
    target_schema = _target_schema(goal, tables)
    if target_schema:
        # Declaring a column *is* asking for it. "做一张表，包含客户名称" names a column
        # the base table does not have, and delivering it blank answers nothing — the
        # request is to go and fill it from wherever it lives.
        target_fields = _schema_target_fields(
            target_schema, tables, primary, target_fields
        )
        if any(item["table"] != primary for item in target_fields):
            capabilities = {**capabilities, "needs_lookup": True}
    filter_rules = _filter_requirements(goal, goal_plan, tables, primary)
    dedup_rules = _deduplication_requirements(goal, goal_plan, tables, primary)
    filters = filter_rules.accepted
    deduplication = dedup_rules.accepted
    derivations = [
        *_derivation_requirements(goal, goal_plan, tables, primary),
        *_split_requirements(goal, tables, primary),
    ]
    aggregation = _aggregation_requirement(goal, goal_plan, tables, primary, melt)
    actions = _actions(
        goal, capabilities, filters, derivations, deduplication, aggregation, rollups
    )
    missing_slots = _missing_slots(
        goal_plan,
        tables,
        primary,
        target_fields=target_fields,
        actions=actions,
        filters=filters,
        deduplication=deduplication,
        unverified=[*filter_rules.unverified, *dedup_rules.unverified],
        has_union=bool(union),
        unasked_unions=[] if union else unionable_groups(tables),
        aggregation=aggregation,
    )

    return TaskSpec(
        objective=goal.strip(),
        primary_entity=primary,
        target_tables=target_tables,
        target_fields=target_fields,
        actions=actions,
        filters=filters,
        deduplication=deduplication,
        derivations=derivations,
        union=union,
        rollups=rollups,
        melt=melt,
        target_schema=target_schema,
        aggregation=aggregation,
        join_requirements=(
            [{"required": True, "candidate_tables": target_tables}]
            if capabilities.get("needs_lookup")
            else []
        ),
        acceptance_criteria=[
            "结果只能引用输入中真实存在的表和字段",
            "交付物只能包含用户目标直接要求的内容",
        ],
        output_intent={
            "requested_outputs": list(goal_plan.get("requested_outputs") or []),
            "business_views": list(goal_plan.get("business_views") or []),
            "needs_charts": bool(capabilities.get("needs_charts")),
            "chart_type": str(goal_plan.get("chart_type") or ""),
            "needs_review": bool(
                capabilities.get("needs_exception_review")
                or (capabilities.get("wants_annotation") and not derivations)
            ),
            "withhold_flagged_rows": bool(
                capabilities.get("wants_row_review")
            ),
            "needs_report": bool(capabilities.get("needs_report")),
            "needs_change_manifest": bool(
                capabilities.get("needs_change_manifest")
            )
            or wants_change_manifest(goal),
        },
        assumptions=list(goal_plan.get("assumptions") or []),
        confidence=str(goal_plan.get("understanding_confidence") or "low"),
        missing_slots=missing_slots,
    )


def _target_fields(
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> list[dict[str, str]]:
    candidates = goal_plan.get("target_fields") or goal_plan.get("matched_fields") or []
    valid: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "")
        field = str(item.get("field") or "")
        key = (table, field)
        if (
            table in tables
            and field in {str(column) for column in tables[table].columns}
            and key not in seen
        ):
            valid.append({"table": table, "field": field})
            seen.add(key)
    return valid


def _target_tables(
    goal_plan: dict[str, Any],
    table_names: set[str],
    primary: str | None,
    target_fields: list[dict[str, str]],
) -> list[str]:
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    candidates.extend(item["table"] for item in target_fields)
    for item in goal_plan.get("table_matches") or []:
        if isinstance(item, dict):
            candidates.append(str(item.get("table") or ""))
    return unique([name for name in candidates if name in table_names])


# "按<字段>统计/汇总/分组" —— 分组维度紧跟在这些介词之后。
# "各" is the other everyday way to name a dimension: "按月统计各城市销售额" asks for
# two of them, and reading only the one after 按 answered half the question.
_GROUP_BY_PREFIXES: tuple[str, ...] = (
    "按照", "按", "根据", "以", "各", "每个", "每一个", "分", "group by", "分组",
)
_AGGREGATE_TOKENS: tuple[str, ...] = (
    "统计", "汇总", "合计", "求和", "总额", "总金额", "总数", "数量", "平均", "均值",
    "最大", "最小", "计数", "sum", "count", "avg", "average", "total",
    # Business users rarely say "统计". They say "按城市看看销售情况" or "分城市对比
    # 一下" and expect a pivot; without these the request produced no summary at all.
    "看看", "看一下", "对比", "分布", "情况", "表现", "排名", "占比", "各是多少",
    # 「按 category 分析订单」 named a dimension and asked for a chart, yet produced no
    # aggregation at all — 分析 was missing from this list, so the chart had nothing to
    # plot. It is about as central a word as this vocabulary has.
    "分析",
    # Ranking questions are aggregation questions: "每个城市金额最高的前3个销售员"
    # names a group, a measure and a cutoff, and produced nothing at all.
    "最高", "最低", "最多", "最少", "前", "top",
)
# Words that name a measure without naming a column. "按城市统计销售额" has no column
# called 销售额 — the number the user means is the amount column, and answering with a
# row count instead is answering a different question.
_MEASURE_SYNONYMS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("销售额", "销售金额", "营业额", "成交额", "收入", "销售情况", "业绩", "流水"),
        ("金额", "amount", "价格", "售价", "总价", "金额合计"),
    ),
    (("销量", "销售量", "出货量"), ("数量", "qty", "quantity", "件数")),
)
# 指标词 -> (聚合函数, 输出列后缀)
_METRIC_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("总金额", "总额", "求和", "合计", "sum", "total"), "sum", "合计"),
    (("平均", "均值", "avg", "average"), "mean", "平均"),
    (("最大", "max"), "max", "最大"),
    (("最小", "min"), "min", "最小"),
)


# 「行是城市，列是月份」/「按城市和月份做交叉表」—— 人说「透视表」时想要的形状。
_CROSSTAB_TOKENS: tuple[str, ...] = (
    "交叉表", "透视表", "交叉分析", "行列", "crosstab", "pivot table",
)
_COLUMN_DIMENSION = re.compile(r"列(?:是|为|用|放)\s*([^\s，,。；;]+)")


def _crosstab_column(goal: str, group_by: list[str]) -> str:
    """Which dimension becomes columns rather than more rows.

    Named explicitly (「列是月份」) or, when the goal asks for a 交叉表 with two
    dimensions, the second one — which is how people write them: the first is the row.
    """

    match = _COLUMN_DIMENSION.search(goal)
    if match:
        named = match.group(1).strip()
        for field in group_by:
            if field in named or named in field:
                return field
    if len(group_by) >= 2 and any(
        token in goal.lower() or token in goal for token in _CROSSTAB_TOKENS
    ):
        return group_by[-1]
    return ""


def _aggregation_requirement(
    goal: str,
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
    melt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The grouped summary shape the goal asks for, if any.

    "按城市统计订单总金额和订单数量" declares an output *shape*: one row per 城市 with
    a summed amount and a record count. Expressing it here lets the delivery contract
    declare a real summary sheet instead of handing back the untouched detail rows.
    """

    overlay = goal_plan.get("aggregation")
    if not any(
        token in goal.lower() or token in goal for token in _AGGREGATE_TOKENS
    ) and not isinstance(overlay, dict):
        return {}
    # Every table's columns are in scope here, not just the base table's: the summary is
    # built from the *delivered* result, which already carries whatever a lookup brought
    # across. Scoping this to the base table meant the most ordinary business request of
    # all — "关联出所属大区，再按大区汇总" — matched the lookup, matched the labels, and
    # then silently produced no summary at all, because 所属大区 lives in the other table.
    available = _available_fields(tables, primary) | _available_fields(tables, None)
    # A reshape creates columns that exist only after it runs. Deriving the summary
    # from the pre-reshape schema meant 「宽转长后按月份统计金额合计」 could not find
    # 金额 — the very column the reshape was about to produce — and fell back to
    # counting rows.
    melt = melt or {}
    melt_columns = {
        str(melt.get("variable_name") or ""),
        str(melt.get("value_name") or ""),
    } - {""}
    available |= melt_columns
    numeric = _numeric_fields(tables) | (
        {str(melt.get("value_name"))} if melt.get("value_name") else set()
    )
    group_by = _group_by_fields(goal, available)
    # Columns the reshape is about to fold away cannot be a date dimension: deriving a
    # 月份 bucket from the 1月 *column header* produced a second 月份 alongside the one
    # the reshape creates, and the pivot died inserting the same name twice.
    melted_away = {str(column) for column in melt.get("value_columns") or []}
    time_bucket = (
        {}
        if melted_away
        else _time_bucket(goal, tables, primary)
    )
    if time_bucket:
        group_by = [time_bucket["output_name"], *group_by]
    if not group_by and isinstance(overlay, dict):
        group_by = [
            str(field)
            for field in (overlay.get("group_by") or [])
            if str(field) in available
        ]
    overlay_metrics = (
        [dict(item) for item in overlay.get("metrics") or []]
        if isinstance(overlay, dict)
        else []
    )
    if not group_by:
        if overlay_metrics:
            return {
                "group_by": [],
                "metrics": overlay_metrics,
                "source": "llm",
            }
        # "统计所有订单的金额总和" names a measure and no dimension. The answer is one
        # number, and returning nothing at all handed back the detail rows and left the
        # question unanswered. Gated on an explicit total word rather than the broad
        # aggregate vocabulary, because "看看这份数据" must not sprout a summary sheet
        # nobody asked for.
        totals = _aggregate_metrics(goal, available, [], numeric)
        if not _asks_grand_total(goal) or not totals:
            return {}
        return {"group_by": [], "metrics": totals, "source": "goal_text"}
    column_field = _crosstab_column(goal, group_by)
    if column_field:
        # 「行是城市，列是月份」 asks for a layout, not just numbers. Reading it as a
        # second grouping level gives the same values down the page instead of across
        # it — the report a user cannot use.
        group_by = [field for field in group_by if field != column_field]
    metrics = _aggregate_metrics(goal, available, group_by, numeric)
    if overlay_metrics and metrics == [
        {"column": None, "agg": "count", "output_name": "记录数"}
    ]:
        # The deterministic parser found the dimension but not a semantically named
        # measure (e.g. “销售走势” over amount), so its generic row count is a fallback,
        # not stronger evidence than the schema-validated semantic metric.
        metrics = overlay_metrics
    requirement: dict[str, Any] = {
        "group_by": unique(group_by),
        "column_field": column_field,
        "metrics": metrics or overlay_metrics,
        "source": "llm" if metrics == overlay_metrics else "goal_text",
    }
    if time_bucket:
        requirement["time_bucket"] = time_bucket
    top_n = _top_n(goal)
    if top_n:
        # "前3个销售员" names the thing being ranked, so it is a dimension too. Without
        # it the answer was three cities rather than three salespeople per city.
        ranked_entity = _entity_after_top_n(goal, available)
        if ranked_entity and ranked_entity not in group_by:
            group_by = [*group_by, ranked_entity]
            requirement["group_by"] = group_by
            requirement["metrics"] = _aggregate_metrics(goal, available, group_by, numeric)
        requirement["top_n"] = top_n
        # With more than one dimension, "前3" means three per group, not three in total:
        # "每个城市金额最高的前3个客户" is a within-group ranking — the thing Excel needs
        # an array formula or Power Query for.
        requirement["rank_within"] = group_by[:-1] if len(group_by) > 1 else []
    return requirement


# Words that ask for a single number over everything, as opposed to a breakdown.
# Deliberately narrower than _AGGREGATE_TOKENS: a miss here costs one summary sheet
# that the user can ask for again, while a false positive adds a deliverable to every
# vaguely analytical goal.
_GRAND_TOTAL_TOKENS: tuple[str, ...] = (
    "总和", "总额", "总金额", "总计", "合计", "总数", "求和", "一共", "共计", "共有",
    "累计", "整体", "全部加起来", "sum", "total", "overall",
)


def _asks_grand_total(goal: str) -> bool:
    lowered = goal.lower()
    return any(token in lowered or token in goal for token in _GRAND_TOTAL_TOKENS)


# "取前5名" / "top 10" / "金额最高的前3个" —— how many rows of the summary to keep.
_TOP_N_PATTERN = re.compile(r"(?:前|top\s*)\s*(\d+)\s*(?:名|条|个|行|位)?", re.IGNORECASE)


def _top_n(goal: str) -> int:
    match = _TOP_N_PATTERN.search(goal)
    if not match:
        return 0
    value = int(match.group(1))
    return value if 0 < value <= 1000 else 0


def _entity_after_top_n(goal: str, available: set[str]) -> str:
    """The column named right after "前3个" —— what is being ranked."""

    match = _TOP_N_PATTERN.search(goal)
    if not match:
        return ""
    window = goal[match.end() : match.end() + 12].lstrip("的 ")
    fields = _fields_named_at(window, available)
    return fields[0] if fields else ""


# "按月" is the single most common way a business user asks for a summary, and the
# dimension it names does not exist as a column. Grouping by the raw date instead gives
# one row per day, which answers nothing; so the bucket is derived for the summary only
# and never added to the detail sheet as another column to explain.
_TIME_GRANULARITIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("季度", "季", "quarter"), "quarter", "季度"),
    (("月份", "月", "month"), "month", "月份"),
    (("年度", "年", "year"), "year", "年份"),
    (("周", "星期", "week"), "week", "周"),
)


def _time_bucket(
    goal: str,
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> dict[str, Any]:
    """A 按月/按季度/按年 dimension derived from whichever column holds the dates."""

    granularity = ""
    output_name = ""
    for prefix in _GROUP_BY_PREFIXES:
        for match in re.finditer(re.escape(prefix), goal, flags=re.IGNORECASE):
            window = goal[match.end() : match.end() + 6].lstrip("：: ")
            hit = next(
                (
                    (value, name)
                    for tokens, value, name in _TIME_GRANULARITIES
                    if any(window.startswith(token) for token in tokens)
                ),
                None,
            )
            if hit:
                granularity, output_name = hit
                break
        if granularity:
            break
    if not granularity:
        return {}

    field = _date_field(tables, primary)
    if not field:
        return {}
    return {"field": field, "granularity": granularity, "output_name": output_name}


def _date_field(tables: dict[str, pd.DataFrame], primary: str | None) -> str:
    """The column the dates live in: named like a date, and actually parsing as one."""

    frames = [tables[primary]] if primary in tables else list(tables.values())
    named = [
        str(column)
        for frame in frames
        for column in frame.columns
        if looks_like_date_field(column)
    ]
    for candidate in named:
        for frame in frames:
            if candidate not in frame.columns:
                continue
            parsed = pd.to_datetime(frame[candidate], errors="coerce", format="mixed")
            if parsed.notna().any():
                return candidate
    return named[0] if named else ""


def _group_by_fields(goal: str, available: set[str]) -> list[str]:
    """Fields named in aggregation clauses, not unrelated lookup/key clauses."""

    found: list[str] = []
    clauses = _clauses(goal)
    for index, clause in enumerate(clauses):
        next_clause = clauses[index + 1] if index + 1 < len(clauses) else ""
        if not _is_aggregation_clause(clause, next_clause):
            continue
        for prefix in _GROUP_BY_PREFIXES:
            for match in re.finditer(re.escape(prefix), clause, flags=re.IGNORECASE):
                window = clause[match.end() : match.end() + 24].lstrip("：: ")
                found.extend(_fields_named_at(window, available))
    return unique(found)


_NON_AGGREGATION_BY_TOKENS: tuple[str, ...] = (
    "关联",
    "匹配",
    "带回",
    "带出",
    "补上",
    "主表",
    "为键",
    "作为键",
    "join",
    "lookup",
    "vlookup",
    "xlookup",
)


def _is_aggregation_clause(clause: str, next_clause: str) -> bool:
    lowered = clause.lower()
    if any(token in lowered or token in clause for token in _AGGREGATE_TOKENS):
        return True
    if any(token in lowered or token in clause for token in _CROSSTAB_TOKENS):
        return True
    next_lowered = next_clause.lower()
    next_is_aggregation = any(
        token in next_lowered or token in next_clause for token in _AGGREGATE_TOKENS
    )
    return next_is_aggregation and not any(
        token in lowered or token in clause for token in _NON_AGGREGATION_BY_TOKENS
    )


# "按城市和品类统计" names two dimensions; reading only the first answered half of it.
_DIMENSION_CONJUNCTIONS = re.compile(r"^\s*(?:和|与|以及|及|、|,|，|\+)\s*")


def _fields_named_at(window: str, available: set[str]) -> list[str]:
    """Every dimension the text names, following 和/与/、 to the next one."""

    fields: list[str] = []
    remaining = window
    while remaining:
        field = _field_named_at(remaining, available)
        if not field:
            break
        fields.append(field)
        # Advance past however much of the name the user actually typed. They abbreviate
        # ("按大区" for 所属大区), so consume the matched prefix rather than len(field).
        consumed = next(
            (
                size
                for size in range(min(len(remaining), len(field)), 0, -1)
                if field.endswith(remaining[:size]) or remaining.startswith(field[:size])
            ),
            0,
        )
        remaining = remaining[consumed:]
        conjunction = _DIMENSION_CONJUNCTIONS.match(remaining)
        if not conjunction:
            break
        remaining = remaining[conjunction.end() :]
    return fields


def _field_named_at(window: str, available: set[str]) -> str | None:
    """The column the text right after 按/根据/以 refers to.

    People shorten column names in speech: they write "按大区统计" for a column actually
    called 所属大区, and "按等级" for 客户等级. Requiring the full name meant those goals
    resolved to no dimension at all and the summary was quietly skipped, so a qualifier
    the column name merely *ends with* counts as naming it.
    """

    ordered = sorted(available, key=len, reverse=True)
    for field in ordered:
        if window.startswith(field):
            return field
    # The other half of the abbreviation habit: "前3个客户" for 客户编号. Only accepted
    # when one column starts that way — with 客户编号/客户名称/客户等级 all present, the
    # goal has not said which, and guessing would answer a question nobody asked.
    stem = re.match(r"[一-鿿A-Za-z]{2,6}", window)
    if stem:
        prefixed = [field for field in ordered if field.startswith(stem.group(0))]
        if len(prefixed) == 1:
            return prefixed[0]
    # Fall back to the abbreviation reading: the longest leading run of the window that
    # some column name ends with. Taking the longest keeps 客户等级 from losing to a
    # shorter accidental match elsewhere.
    best: tuple[int, str] | None = None
    for field in ordered:
        for size in range(min(len(window), len(field)), 1, -1):
            if field.endswith(window[:size]):
                if best is None or size > best[0]:
                    best = (size, field)
                break
    return best[1] if best else None


def _numeric_fields(tables: dict[str, pd.DataFrame]) -> set[str]:
    """Columns that hold numbers, so a measure can be recognised without a keyword."""

    fields: set[str] = set()
    for frame in tables.values():
        for column in frame.columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            if values.notna().any():
                fields.add(str(column))
    return fields


def _aggregate_metrics(
    goal: str,
    available: set[str],
    group_by: list[str],
    numeric_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Numeric measures the goal named, plus a record count when it asked for one."""

    metrics: list[dict[str, Any]] = []
    for field in sorted(available - set(group_by), key=len, reverse=True):
        # Every occurrence matters, not just the first: in a paragraph the field is
        # usually mentioned by an earlier clause too ("给金额大于 1000 的…打标…按城市
        # 汇总订单总金额"), and only the later mention carries the aggregation.
        for match in re.finditer(re.escape(field), goal):
            # The window reaches *back* over the field name because the qualifier
            # naming the aggregation precedes it ("总金额" — the 总 sits before 金额).
            window = goal[max(0, match.start() - 6) : match.end() + 10]
            matched = next(
                (
                    (agg, suffix)
                    for tokens, agg, suffix in _METRIC_RULES
                    if any(token in window for token in tokens)
                ),
                None,
            )
            if matched:
                metrics.append(
                    {
                        "column": field,
                        "agg": matched[0],
                        "output_name": f"{field}{matched[1]}",
                    }
                )
                break
    # "订单数" / "件数" / "人数" are the everyday ways to ask for a record count; the
    # generic "数量" alone missed all of them.
    count_tokens = (
        "数量", "条数", "笔数", "计数", "总数", "个数", "人数", "件数", "台数",
        "行数", "订单数", "记录数", "count",
    )
    if any(token in goal for token in count_tokens):
        metrics.append({"column": None, "agg": "count", "output_name": "记录数"})
    if not any(metric["column"] for metric in metrics):
        implied = _measure_by_synonym(goal, available - set(group_by))
        if not implied:
            # "金额最高的前2个销售员" names the measure by its own column name, with a
            # superlative rather than one of the aggregation keywords. Falling through
            # to a row count answered "who has the most orders" instead.
            candidates = sorted(
                (numeric_fields or set()) & (available - set(group_by)),
                key=len,
                reverse=True,
            )
            implied = next((field for field in candidates if field in goal), "")
        if implied:
            metrics.insert(
                0,
                {"column": implied, "agg": "sum", "output_name": f"{implied}合计"},
            )
    return metrics or [{"column": None, "agg": "count", "output_name": "记录数"}]


def _measure_by_synonym(goal: str, available: set[str]) -> str | None:
    """The column a measure word refers to when the goal never names one.

    "按城市统计销售额" asks about money; no column is called 销售额, so the summary came
    back as a row count — a different question from the one that was asked.
    """

    normalized = {field: str(field).lower() for field in available}
    for measure_words, column_hints in _MEASURE_SYNONYMS:
        if not any(word in goal for word in measure_words):
            continue
        for hint in column_hints:
            for field, lowered in normalized.items():
                if hint in lowered:
                    return field
    return None


# 「把地址拆成省、市、区」/「地址按 - 分列」—— Excel 的分列，中文业务表最常做的操作之一。
_SPLIT_VERBS: tuple[str, ...] = (
    "拆成", "拆分成", "拆分为", "分成", "分列", "拆分", "拆开", "split",
)
# 用户点名分隔符时的说法：「按 - 拆分」「用逗号分开」。
_SPLIT_SEPARATOR = re.compile(r"(?:按|用|以|by)\s*[「'\"]?(.)[」'\"]?\s*(?:拆|分|split)")
# 「拆成省、市、区」——动词之后列出的就是各段的名字。
_SPLIT_PART_SEPARATORS = re.compile(r"[、,，/和跟及与\s]+")


# 「把订单明细的金额汇总到客户表」/「统计每个客户的订单总额」—— Excel 的跨表 SUMIF。
_ROLLUP_TOKENS: tuple[str, ...] = (
    "汇总", "合计", "求和", "总额", "总金额", "累计", "sumif", "小计",
    "统计", "笔数", "条数", "个数", "数量",
)
# 汇总方式 -> (聚合函数, 输出列后缀)
_ROLLUP_AGGS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("平均", "均值", "avg"), "mean", "平均"),
    (("笔数", "条数", "数量", "个数", "count"), "count", "笔数"),
    (("最大", "max"), "max", "最大"),
    (("最小", "min"), "min", "最小"),
    (("汇总", "合计", "求和", "总额", "总金额", "累计", "sumif", "小计"), "sum", "合计"),
)


# 「宽转长」「把月份列转成行」「逆透视」—— 报表按人读的方式排版（一行一个客户、
# 十二列十二个月），而任何分析要的都是相反的形状。
_MELT_TOKENS: tuple[str, ...] = (
    "宽转长", "转成长表", "长表", "逆透视", "转成行", "变成行", "列转行", "unpivot", "melt",
)


def _melt_requirement(
    goal: str,
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> dict[str, Any]:
    """The wide block this goal asks to unpivot, if it asks.

    Only ever from the goal. A wide table is a perfectly good deliverable — it is how
    the user's report already looks — so reshaping it uninvited would hand back
    something they did not ask for and cannot recognise.
    """

    if not any(token in goal.lower() or token in goal for token in _MELT_TOKENS):
        return {}
    name = primary if primary in tables else next(iter(tables), None)
    if not name:
        return {}
    value_columns = wide_value_columns(tables[name])
    if len(value_columns) < 2:
        return {}
    return {
        "table": name,
        "value_columns": value_columns,
        "variable_name": _melt_variable_name(goal, value_columns),
        "value_name": _melt_value_name(goal),
    }


def _melt_variable_name(goal: str, value_columns: list[str]) -> str:
    """What the folded-away column headers become. 1月/2月/3月 -> 月份."""

    for token, label in (("月", "月份"), ("季", "季度"), ("年", "年份"), ("周", "周次")):
        if all(token in str(column) for column in value_columns):
            return label
    for label in ("月份", "季度", "年份", "周次", "类别", "维度"):
        if label in goal:
            return label
    return "类别"


def _melt_value_name(goal: str) -> str:
    for label in ("金额", "数量", "销售额", "销量", "数值"):
        if label in goal:
            return label
    return "数值"


def _rollup_requirements(
    goal: str,
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> list[dict[str, Any]]:
    """Detail-table totals the goal asks to carry onto the master.

    The shape is unmistakable once you look for it: the goal names a measure that
    lives in *another* table, that table shares a key with the base table, and the
    goal uses a summing word. Bringing that column across with a plain lookup would
    give one arbitrary matching row instead of the total, and joining the detail
    outright would multiply the master's rows — which is why this is its own
    operation rather than a variation of either.
    """

    if not primary or primary not in tables:
        return []
    if not any(token in goal.lower() or token in goal for token in _ROLLUP_TOKENS):
        return []
    # A destination phrase is what separates a rollup from an ordinary summary:
    # 「按客户统计金额」 wants a summary table, 「统计金额填到客户档案」 wants a column
    # on the master. Without one, the aggregation path already covers the request.
    if not _destination_table(goal, tables):
        return []

    master = tables[primary]
    master_columns = {str(column) for column in master.columns}
    requirements: list[dict[str, Any]] = []
    for table_name, frame in tables.items():
        if table_name == primary or frame.empty:
            continue
        shared = [
            column
            for column in master_columns & {str(item) for item in frame.columns}
            if looks_like_key(column)
        ]
        if not shared:
            continue
        key = shared[0]
        measures = [
            str(column)
            for column in frame.columns
            if str(column) in goal
            and str(column) not in master_columns
            and pd.api.types.is_numeric_dtype(_numeric_view(frame[column]))
        ]
        agg, suffix = _rollup_aggregation(goal)
        # Counting rows needs no measure column: 「统计每个客户的订单笔数」 names none,
        # and requiring one meant the most basic rollup of all could not be asked for.
        if agg == "count" and not measures:
            measures = [key]
        for measure in measures:
            requirements.append(
                {
                    "source_table": table_name,
                    "left_key": key,
                    "right_key": key,
                    "measure": measure,
                    "agg": agg,
                    "output": (
                        f"{suffix}" if agg == "count" and measure == key
                        else f"{measure}{suffix}"
                    ),
                }
            )
    return requirements


def _numeric_view(values: pd.Series) -> pd.Series:
    """A column read as numbers, so 「1,234」 still counts as a measure."""

    return to_business_numeric(values)


def _rollup_aggregation(goal: str) -> tuple[str, str]:
    lowered = goal.lower()
    for tokens, agg, suffix in _ROLLUP_AGGS:
        if any(token in lowered or token in goal for token in tokens):
            return agg, suffix
    return "sum", "合计"


def _split_requirements(
    goal: str,
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> list[dict[str, Any]]:
    """Column-splitting rules the goal asks for.

    The separator is read from the goal when stated and inferred from the column's own
    values otherwise, because users say 「把地址拆开」 far more often than they name a
    delimiter. Inference that cannot find a consistent one produces nothing rather than
    a guess — a column split on the wrong character is worse than one left alone.
    """

    if not any(verb in goal for verb in _SPLIT_VERBS):
        return []
    frame = tables.get(primary) if primary in tables else None
    if frame is None:
        frame = next(iter(tables.values()), None)
    if frame is None or frame.empty:
        return []

    available = {str(column) for column in frame.columns}
    requirements: list[dict[str, Any]] = []
    for clause in _clauses(goal):
        source = next(
            (
                field
                for field in sorted(available, key=len, reverse=True)
                if field in clause
            ),
            "",
        )
        if not source or not any(verb in clause for verb in _SPLIT_VERBS):
            continue
        stated = _SPLIT_SEPARATOR.search(clause)
        separator, count = infer_separator(frame[source])
        if stated:
            separator = stated.group(1)
            counts = frame[source].dropna().astype(str).str.count(re.escape(separator))
            count = int(counts.max()) + 1 if not counts.empty else 0
        if not separator or count < 2:
            continue
        requirements.append(
            {
                "kind": "split",
                "source": source,
                "sep": separator,
                "outputs": split_part_names(source, count, _split_part_names(clause, available)),
            }
        )
    return requirements


def _split_part_names(clause: str, available: set[str]) -> list[str]:
    """Names listed after the split verb: 「拆成省、市、区」 -> [省, 市, 区]."""

    for verb in _SPLIT_VERBS:
        position = clause.find(verb)
        if position < 0:
            continue
        tail = clause[position + len(verb) :].strip()
        names = [
            token
            for token in (part.strip() for part in _SPLIT_PART_SEPARATORS.split(tail))
            if token and token not in available
        ]
        if len(names) >= 2:
            return names
    return []


def _derivation_requirements(
    goal: str,
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> list[dict[str, Any]]:
    """Columns this task must add, from the goal text and the validated overlay."""

    available = _available_fields(tables, primary)
    parsed = [
        {**item, "source": "goal_text"} for item in _derivations_from_goal(goal, available)
    ]
    if parsed:
        return parsed
    # Adding a column cannot lose data, so a model-proposed derivation is accepted on
    # its own once it type-checks — unlike a filter, which must be grounded in the
    # user's own words before it may delete anything.
    return [
        {**item, "source": "llm"}
        for item in _validated_derivations(goal_plan.get("derivations"), available)
    ]


def _actions(
    goal: str,
    capabilities: dict[str, Any],
    filters: list[dict[str, Any]],
    derivations: list[dict[str, Any]] | None = None,
    deduplication: list[dict[str, Any]] | None = None,
    aggregation: dict[str, Any] | None = None,
    rollups: list[dict[str, Any]] | None = None,
) -> list[TaskAction]:
    actions: list[TaskAction] = []
    lowered = goal.lower()
    if capabilities.get("wants_inplace"):
        actions.append("clean")
    # An authorised rule counts even when no listed token appears, so a quote-authorised
    # dedup still claims the action — and still raises the missing-key question if the
    # user never said which field identifies a duplicate.
    if wants_deduplication(goal) or deduplication:
        actions.append("deduplicate")
    if filters or (
        any(token in lowered or token in goal for token in ("筛选", "过滤", "filter"))
        and not _filter_is_negated(goal)
    ):
        actions.append("filter")
    # A rollup is a cross-table operation like any other lookup; without claiming the
    # action, coverage validation rejects the very step the goal asked for.
    if capabilities.get("needs_lookup") or rollups:
        actions.append("lookup")
    if derivations or any(
        token in lowered or token in goal
        for token in (
            "新增字段",
            "新增列",
            "计算字段",
            "计算列",
            "派生字段",
            "derive",
            "calculated field",
        )
    ):
        # A labelling rule *is* a derived column, so it must claim the capability that
        # actually produces one; otherwise coverage validation passed on an unrelated
        # step and the label was never generated. A bare "计算" is deliberately not an
        # action signal: aggregation and numeric parsing both calculate without adding
        # a column, and treating them as derivations makes valid summaries impossible.
        actions.append("derive")
    if capabilities.get("wants_annotation"):
        actions.append("annotate")
    # A marker with a concrete derivation (e.g. amount > 1000 -> 大额) executes as a
    # formula. A marker with no derivation is an issue annotation: the validator must
    # first locate the affected rows, and the problem view is how that annotation is
    # delivered without polluting the primary business columns.
    if capabilities.get("needs_exception_review") or (
        capabilities.get("wants_annotation") and not derivations
    ):
        actions.extend(["validate", "review"])
    # A task that compiled a summary shape *is* analysing, whether or not the
    # capability flag caught it. Reading the action from the flag alone produced a plan
    # that declared a 汇总结果 artifact while claiming no analysis action, and coverage
    # validation then rejected its own aggregate step as "用户未要求的动作".
    # A rollup already answers the summing request by adding one value to each master
    # row; it is a lookup-shaped operation, not a separate summary artifact. Claiming
    # analyze as well opened a blocking "按什么统计" slot after the user had already
    # said what to total and where to put it. A real standalone aggregation still
    # claims analyze even when the same goal also contains a rollup.
    if aggregation or (capabilities.get("needs_analysis") and not rollups):
        actions.append("analyze")
    if capabilities.get("needs_charts"):
        actions.append("chart")
    actions.append("export")
    return unique(actions)


def _missing_slots(
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
    *,
    target_fields: list[dict[str, str]],
    actions: list[TaskAction],
    filters: list[dict[str, Any]],
    deduplication: list[dict[str, Any]],
    unverified: list[dict[str, Any]] | None = None,
    has_union: bool = False,
    unasked_unions: list[list[str]] | None = None,
    aggregation: dict[str, Any] | None = None,
) -> list[MissingSlot]:
    slots: list[MissingSlot] = []
    confidence = str(goal_plan.get("understanding_confidence") or "low")
    if confidence == "low" and primary is None and len(tables) >= 2 and not has_union:
        slots.append(
            MissingSlot(
                slot_id="primary_table",
                kind="primary_table",
                question="请选择本次任务应以哪张表作为主数据表。",
                impact="主表决定记录粒度以及后续清洗、匹配和校验的执行对象。",
                options=[str(name) for name in tables],
            )
        )

    seen_questions = {slot.question for slot in slots}
    non_blocking = {
        str(item).strip() for item in goal_plan.get("non_blocking_questions") or []
    }
    for question in goal_plan.get("clarification_questions") or []:
        text = str(question).strip()
        if (
            not text
            or text in seen_questions
            or _duplicates_primary_table_slot(text, slots)
            or _question_is_satisfied(text, filters, deduplication)
        ):
            continue
        slots.append(
            MissingSlot(
                slot_id=_slot_id(text),
                kind=_slot_kind(text),
                question=text,
                impact="该信息会影响计划准确性，需要确认后再执行。",
                # Blocking unless the understanding layer said the run can proceed
                # without the answer. "能用的留下" names a filter with no criterion —
                # running it through delivers every row and answers a different
                # question. "括号是不是负数" has a standard answer, and stopping a
                # one-sentence request to ask it is how a four-row upload turned into
                # two rounds of forms.
                priority=(
                    "medium"
                    if text in non_blocking
                    or (
                        (
                            _question_is_optional_scope_expansion(text)
                            or _question_has_safe_preservation_default(text)
                        )
                        and _task_contract_can_proceed(
                            primary=primary,
                            target_fields=target_fields,
                            actions=actions,
                            filters=filters,
                            deduplication=deduplication,
                            aggregation=aggregation,
                        )
                    )
                    else "high"
                ),
            )
        )
        seen_questions.add(text)
    # Every question below offers real choices read off this upload. A user who could
    # write 「按 status 字段，保留 paid」 would not have needed asking; given a blank box
    # they answer vaguely, given four buttons they answer correctly.
    if "filter" in actions and not filters:
        slots.append(
            MissingSlot(
                slot_id="filter_rule",
                kind="retention_rule",
                question="按哪个条件保留记录？",
                impact="过滤条件会直接决定哪些记录进入最终结果。",
                options=lead_with_remembered(
                    filter_field_options(tables, primary),
                    remembered_choices("filter_rule"),
                ),
            )
        )
    if "deduplicate" in actions and not deduplication:
        slots.append(
            MissingSlot(
                slot_id="deduplication_rule",
                kind="retention_rule",
                question="按哪个字段判断重复？",
                impact="去重键和保留策略会直接决定哪些重复记录被删除。",
                options=lead_with_remembered(
                    dedup_key_options(tables, primary),
                    remembered_choices("deduplication_rule"),
                ),
            )
        )
    # Claimed an analysis but no shape could be derived from the goal. Delivering the
    # detail table silently is the one thing no mature product does: 「统计一下」 asks
    # for a number and gets rows back, with nothing saying the question went unanswered.
    if "analyze" in actions and not aggregation:
        slots.append(
            MissingSlot(
                slot_id="analysis_target",
                kind="business_rule",
                question="按什么统计？",
                impact="没有统计维度就只能交回明细表，回答不了这个问题。",
                options=lead_with_remembered(
                    group_by_options(tables, primary),
                    remembered_choices("analysis_target"),
                ),
            )
        )
    slots.extend(_unverified_removal_slots(unverified or [], seen_questions))
    slots.extend(_unasked_union_slots(unasked_unions or [], seen_questions))
    return slots


def _question_is_optional_scope_expansion(question: str) -> bool:
    """Whether a question asks to widen an already executable task.

    This is intentionally structural, not a new operation verb list. ``还需要检查
    其他字段吗`` is optional scope expansion; ``最终结果需要哪些字段`` and ``能用的按
    什么判断`` describe missing output/retention decisions and must keep blocking.
    """

    lowered = question.lower()
    return any(
        pattern in lowered
        for pattern in (
            "还需要",
            "是否还",
            "要不要也",
            "是否也要",
            "其他字段吗",
            "其它字段吗",
            "anything else",
            "also check",
        )
    )


def _question_has_safe_preservation_default(question: str) -> bool:
    """Whether an unresolved missing-value choice has a safe executable default.

    The model sometimes asks whether an unfillable identifier should be deleted,
    retained and reported, or invented. The product contract already resolves that
    choice: without explicit removal authority, retain the row; without source data,
    never fabricate a value. The question can remain as an assumption, but it must
    not block a task whose typed contract is otherwise complete.
    """

    lowered = question.lower()
    concerns_missing_value = any(
        token in lowered for token in ("缺失", "为空", "未填写", "missing", "blank")
    )
    offers_removal = any(token in lowered for token in ("删除", "剔除", "remove", "delete"))
    offers_preservation = any(
        token in lowered for token in ("保留", "标记", "retain", "keep", "mark")
    )
    offers_fabrication = any(
        token in lowered for token in ("补全", "补齐", "填充", "fill", "impute")
    )
    return (
        concerns_missing_value
        and offers_removal
        and offers_preservation
        and offers_fabrication
    )


def _task_contract_can_proceed(
    *,
    primary: str | None,
    target_fields: list[dict[str, str]],
    actions: list[TaskAction],
    filters: list[dict[str, Any]],
    deduplication: list[dict[str, Any]],
    aggregation: dict[str, Any] | None,
) -> bool:
    """Whether typed intent already contains every execution-critical decision."""

    if primary is None:
        return False
    action_set = set(actions)
    if "filter" in action_set and not filters:
        return False
    if "deduplicate" in action_set and not deduplication:
        return False
    if "analyze" in action_set and not aggregation:
        return False
    if action_set & {"clean", "validate", "annotate"} and not target_fields:
        return False
    return True


def _unasked_union_slots(
    groups: list[list[str]],
    seen_questions: set[str],
) -> list[MissingSlot]:
    """Ask before answering a question out of one file when several hold the data.

    Same-shaped uploads are usually one dataset the source system had to split, but
    they can also be 预算 vs 实际. Either way the old behaviour was the wrong one: pick
    a main table, ignore the rest, and hand back an answer computed from a fraction of
    what the user provided — with nothing in the workbook to show it happened.
    """

    slots: list[MissingSlot] = []
    for index, group in enumerate(groups, start=1):
        names = "、".join(group)
        question = (
            f"上传的 {names} 结构完全相同。是同一份数据分成了几个文件（需要合并成"
            "一张表统一处理），还是各自独立的表（比如预算与实际）？不合并的话，"
            "本次结果只会基于其中一张表。"
        )
        if question in seen_questions:
            continue
        slots.append(
            MissingSlot(
                slot_id=f"table_union_{index}",
                kind="primary_table",
                question=question,
                impact="若本应合并却没合并，统计口径会只覆盖一部分记录。",
                options=["合并成一张表", "分别处理，不要合并"],
            )
        )
        seen_questions.add(question)
    return slots


def _unverified_removal_slots(
    unverified: list[dict[str, Any]],
    seen_questions: set[str],
) -> list[MissingSlot]:
    """Ask about a row-removal the goal does not visibly authorise.

    Dropping these silently was the remaining hole. The rule never runs either way —
    that part is not negotiable — but staying quiet also throws away the one signal we
    have: the understanding layer read the goal and concluded the user wanted records
    gone. When it paraphrased the goal instead of quoting it, that conclusion may well
    be right and the quote merely sloppy, and the user is left wondering why the thing
    they asked for did not happen.

    Only proposals that came with a quote reach here. One with no quote at all is the
    model's own initiative, and an unrequested suggestion is not worth a question.
    """

    slots: list[MissingSlot] = []
    for index, item in enumerate(unverified, start=1):
        target = ", ".join(str(field) for field in item.get("fields") or []) or str(
            item.get("field") or ""
        )
        subject = f"「{target}」" if target else "部分记录"
        question = (
            f"本次识别到可能需要删除{subject}相关的记录，"
            "但在你的目标原文里没有找到对应的表述。如果确实需要，请说明删除条件；"
            "不需要的话可以忽略，本次不会删除任何记录。"
        )
        if question in seen_questions:
            continue
        slots.append(
            MissingSlot(
                slot_id=f"unverified_removal_{index}",
                kind="confirmation",
                question=question,
                impact="未经确认的删除不会执行，相关记录本次全部保留。",
                options=["确认删除这些记录", "保留，不要删除"],
                priority="medium",
            )
        )
        seen_questions.add(question)
    return slots


_SYMBOL_OPERATORS = {
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "=": "equals",
    "==": "equals",
    "!=": "not_equals",
}
_TEXT_OPERATORS = {
    "大于等于": "gte",
    "不少于": "gte",
    "至少": "gte",
    "小于等于": "lte",
    "不超过": "lte",
    "至多": "lte",
    "大于": "gt",
    "高于": "gt",
    "超过": "gt",
    "小于": "lt",
    "低于": "lt",
    "少于": "lt",
    "不等于": "not_equals",
    "等于": "equals",
}
_FILTER_OPERATORS = {
    "gt",
    "gte",
    "lt",
    "lte",
    "equals",
    "not_equals",
    "in",
    "not_in",
}


def _union_requirements(
    goal: str,
    tables: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    """Which uploaded tables the goal asks to be read as one.

    Only same-shaped groups qualify, and only when the goal says to combine them.
    Stacking on structure alone would eventually merge 预算表 with 实际表 — identical
    columns, opposite meanings — so the structural signal narrows the candidates and
    the user's words decide.
    """

    if not wants_table_union(goal):
        return []
    return [
        {"tables": group, "authorization": STATED, "source": GOAL_TEXT_SOURCE}
        for group in unionable_groups(tables)
    ]


class _Requirements(NamedTuple):
    """Rules that will run, and rules that were dropped for lack of authorization.

    ``unverified`` is deliberately narrower than "everything dropped". A model proposal
    with no ``evidence`` at all is the model's own idea; the user asked for nothing and
    hears nothing about it. One that *carried* a quote which is not in the goal is the
    interesting case: the model believed there was an instruction and merely paraphrased
    instead of copying. That is where silence risks losing something the user really did
    ask for, so that — and only that — becomes a question.
    """

    accepted: list[dict[str, Any]]
    unverified: list[dict[str, Any]]


def _filter_requirements(
    goal: str,
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> _Requirements:
    """Collect the row filters this task must apply, from both understanding layers.

    Every filter carries an ``authorization``: whether the user asked for these rows to
    go. Rules parsed out of the goal string are ``stated`` by construction. A model
    proposal is ``stated`` only if it quotes the goal (see planning.provenance) or the
    goal uses a recognised removal verb; otherwise it is ``inferred`` and dropped here.

    Both halves of that matter. Asked "金额超过5000的订单" — which names a set, not an
    action — the model supplied a filter and 12 of 15 rows were deleted. Asked
    "把作废的订单清理一下", the verb table alone had no entry for 清理 and the
    instruction vanished without a word.
    """

    if _filter_is_negated(goal):
        return _Requirements([], [])
    available = _available_fields(tables, primary)
    parsed = [
        {**item, "source": GOAL_TEXT_SOURCE, "authorization": STATED}
        for item in _filters_from_goal(goal, available)
    ]
    parsed_signatures = {_filter_signature(item) for item in parsed}
    goal_authorises = _authorises_filtering(goal)
    names = schema_names(tables)
    overlay: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for item in _validated_filters(goal_plan.get("filters"), available):
        if _filter_signature(item) in parsed_signatures:
            continue
        authorization = authorization_of(
            item,
            goal,
            names=names,
            goal_authorises=goal_authorises,
        )
        if authorization == INFERRED:
            if item.get("evidence"):
                unverified.append(item)
            continue
        overlay.append({**item, "source": LLM_SOURCE, "authorization": authorization})
    return _Requirements(
        _unique_dicts([*parsed, *overlay], ("field", "op", "value", "values", "mode")),
        unverified,
    )


def _authorises_filtering(goal: str) -> bool:
    """Whether the goal itself asks for rows to be removed."""

    return any(_filter_verb(clause) for clause in _clauses(goal))


# Verbs that mean "add a label", not "remove rows". A clause containing one of these
# describes a derived column: its condition selects which *value* a row gets, never
# which rows survive.
_LABEL_VERBS: tuple[str, ...] = (
    "标记为",
    "标注为",
    "标记成",
    "标注成",
    "标成",
    "标为",
    "标上",
    "归为",
    "归类为",
    "归入",
    "分类为",
    "记为",
    "计为",
    "认定为",
    "判定为",
    "定为",
    "列为",
    "算作",
    "算成",
    "视为",
    "视作",
    "当作",
    "当成",
)
# The catch-all branch of a labelling rule ("其余标记为普通订单").
_ELSE_TOKENS: tuple[str, ...] = ("其余", "其他", "其它", "否则", "剩下", "剩余", "剩余的")
# A comma between two digits is a thousands separator, not a clause break — splitting
# "超过5,000" there is what made the threshold read as 5.
_CLAUSE_SEPARATORS = re.compile(r"[，。；;\n]|(?<!\d),(?!\d)")
DEFAULT_LABEL_COLUMN = "标记"
# The other common phrasing puts the label *inside* the verb: "打上大额订单标记".
_WRAPPED_LABEL_PATTERN = re.compile(
    r"(?:打上|加上|贴上|标上|打)\s*([^，。；;,]+?)\s*(?:标记|标签|标识|标注)"
)


def _clauses(goal: str) -> list[str]:
    return [clause for clause in _CLAUSE_SEPARATORS.split(goal) if clause.strip()]


def _is_label_clause(clause: str) -> bool:
    return any(verb in clause for verb in _LABEL_VERBS) or bool(
        _WRAPPED_LABEL_PATTERN.search(clause)
    )


def _label_value(clause: str) -> str:
    """The label a clause assigns.

    Two phrasings are common and both must work, because business users write either:
    "标记为大额订单" (label follows the verb) and "打上大额订单标记" (label is wrapped
    by the verb).
    """

    for verb in _LABEL_VERBS:
        position = clause.find(verb)
        if position < 0:
            continue
        tail = clause[position + len(verb) :].strip().strip("：: 　")
        # Stop at a trailing connective so "大额订单，其余…" yields just "大额订单".
        return re.split(r"[的了吧呢]?\s*$", tail)[0].strip()
    wrapped = _WRAPPED_LABEL_PATTERN.search(clause)
    if wrapped:
        return wrapped.group(1).strip("的 　")
    return ""


def _derivations_from_goal(goal: str, available: set[str]) -> list[dict[str, Any]]:
    """Compile labelling sentences into one derived column per label group.

    "金额大于 1000 的标记为大额订单，其余标记为普通订单" becomes a single column with
    one conditional branch plus a default, which the deterministic executor renders
    with its existing ``ifs`` operator — no new execution capability is involved.
    """

    branches: list[dict[str, Any]] = []
    default_label = ""
    for clause in _clauses(goal):
        if not _is_label_clause(clause):
            continue
        label = _label_value(clause)
        if not label:
            continue
        conditions = _conditions_in_text(clause, available)
        if conditions:
            branches.extend({"when": condition, "then": label} for condition in conditions)
        elif any(token in clause for token in _ELSE_TOKENS):
            default_label = label
    if not branches:
        return []
    return [
        {
            "output": DEFAULT_LABEL_COLUMN,
            "branches": branches,
            "otherwise": default_label,
        }
    ]


def _conditions_in_text(text: str, available: set[str]) -> list[dict[str, Any]]:
    """Extract ``field op value`` conditions from one clause, without filter semantics."""

    found: list[dict[str, Any]] = []
    for field in sorted(available, key=len, reverse=True):
        escaped = re.escape(field)
        patterns = (
            rf"{escaped}\s*(>=|<=|==|!=|>|<|=)\s*{_OPERAND_PATTERN}",
            rf"{escaped}\s*({'|'.join(map(re.escape, _TEXT_OPERATORS))})\s*{_OPERAND_PATTERN}",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                operator = _SYMBOL_OPERATORS.get(
                    match.group(1), _TEXT_OPERATORS.get(match.group(1))
                )
                if operator is None:
                    continue
                value = _operand_value(match.group(2), operator)
                if value is None:
                    continue
                found.append({"field": field, "op": operator, "value": value})
    return _unique_dicts(found, ("field", "op", "value"))


def _validated_derivations(raw: Any, available: set[str]) -> list[dict[str, Any]]:
    """Keep only model-proposed derivations that reference real fields and real ops."""

    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        output = str(item.get("output") or "").strip()
        branches = [
            {
                "when": {
                    "field": str(branch["when"]["field"]),
                    "op": str(branch["when"]["op"]),
                    "value": branch["when"].get("value"),
                },
                "then": str(branch.get("then") or "").strip(),
            }
            for branch in (item.get("branches") or [])
            if isinstance(branch, dict)
            and isinstance(branch.get("when"), dict)
            and str(branch["when"].get("field")) in available
            and str(branch["when"].get("op")) in _FILTER_OPERATORS
            and str(branch.get("then") or "").strip()
        ]
        if not output or not branches or output in available:
            continue
        result.append(
            {
                "output": output,
                "branches": branches,
                "otherwise": str(item.get("otherwise") or "").strip(),
            }
        )
    return result


# The operand of a comparison. A number with thousands separators (5,000) is tried
# first, because otherwise the comma terminates the token and "超过5,000" reads as 5.
_OPERAND_PATTERN = r"([\"']?(?:[-+]?\d[\d,]*(?:\.\d+)?|[^,\s，。；;]+)[\"']?)"


def _filters_from_goal(goal: str, available: set[str]) -> list[dict[str, Any]]:
    # A labelling clause is not a filter. Reading "金额大于 1000 的标记为大额订单" as a
    # row filter was what silently deleted the records the user only wanted tagged.
    # Each surviving clause is scanned as written: stitching them back into one string
    # rewrote the separators and corrupted values that contain them.
    requirements: list[dict[str, Any]] = []
    for field in sorted(available, key=len, reverse=True):
        escaped = re.escape(field)
        patterns = [
            re.compile(
                rf"{escaped}\s*(>=|<=|==|!=|>|<|=)\s*{_OPERAND_PATTERN}",
                flags=re.IGNORECASE,
            ),
            re.compile(
                rf"{escaped}\s*({'|'.join(map(re.escape, _TEXT_OPERATORS))})\s*"
                rf"{_OPERAND_PATTERN}",
                flags=re.IGNORECASE,
            ),
        ]
        for clause in _clauses(goal):
            if _is_label_clause(clause) or not _filter_verb(clause):
                continue
            for pattern in patterns:
                for match in pattern.finditer(clause):
                    raw_operator = match.group(1)
                    operator = _SYMBOL_OPERATORS.get(
                        raw_operator,
                        _TEXT_OPERATORS.get(raw_operator),
                    )
                    if operator is None:
                        continue
                    value = _operand_value(match.group(2), operator)
                    if value is None:
                        continue
                    requirements.append(
                        {
                            "field": field,
                            "op": operator,
                            "value": value,
                            "mode": _filter_mode(clause),
                        }
                    )
    return requirements


def _validated_filters(raw: Any, available: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        op = str(item.get("op") or "")
        mode = str(item.get("mode") or "keep")
        if field not in available or op not in _FILTER_OPERATORS or mode not in {"keep", "exclude"}:
            continue
        if op in {"in", "not_in"}:
            if not isinstance(item.get("values"), list) or not item["values"]:
                continue
        elif "value" not in item:
            continue
        requirement = {
            "field": field,
            "op": op,
            "mode": mode,
        }
        if "value" in item:
            requirement["value"] = item["value"]
        if isinstance(item.get("values"), list):
            requirement["values"] = list(item["values"])
        _carry_evidence(item, requirement)
        result.append(requirement)
    return result


def _filter_signature(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            "field": item.get("field"),
            "op": item.get("op"),
            "mode": item.get("mode", "keep"),
            "value": item.get("value"),
            "values": item.get("values", []),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _deduplication_requirements(
    goal: str,
    goal_plan: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> _Requirements:
    if _deduplication_is_negated(goal):
        return _Requirements([], [])
    available = _available_fields(tables, primary)
    # Same authorization rule as filters. The token table used to return [] here for the
    # whole function, so 「把重复的记录清理掉」 — plain Chinese, no listed token — dropped
    # a request the user had clearly made.
    goal_authorises = wants_deduplication(goal)
    parsed = [
        {**item, "source": GOAL_TEXT_SOURCE, "authorization": STATED}
        for item in _deduplication_from_goal(goal, available)
    ]
    parsed_keys = {repr((tuple(item["fields"]), item.get("strategy"))) for item in parsed}
    names = schema_names(tables)
    overlay: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for item in _validated_deduplication(goal_plan.get("deduplication"), available):
        if repr((tuple(item["fields"]), item.get("strategy"))) in parsed_keys:
            continue
        authorization = authorization_of(
            item,
            goal,
            names=names,
            goal_authorises=goal_authorises,
        )
        if authorization == INFERRED:
            if item.get("evidence"):
                unverified.append(item)
            continue
        overlay.append({**item, "source": LLM_SOURCE, "authorization": authorization})
    return _Requirements(
        _unique_dicts([*parsed, *overlay], ("fields", "strategy", "order_by")),
        unverified,
    )


def _deduplication_from_goal(goal: str, available: set[str]) -> list[dict[str, Any]]:
    lowered = goal.lower()
    if not wants_deduplication(goal):
        return []

    dedupe_positions = [
        position
        for token in DEDUPLICATION_TOKENS
        if (position := lowered.find(token.lower())) >= 0
    ]
    position = min(dedupe_positions) if dedupe_positions else len(goal)
    prefix = goal[max(0, position - 60) : position]
    scoped = prefix.rsplit("按", 1)[-1] if "按" in prefix else prefix
    keys = [
        field
        for field in sorted(available, key=len, reverse=True)
        if field in scoped
    ]
    if not keys:
        named_key_fields = [
            field
            for field in sorted(available, key=len, reverse=True)
            if field in goal and looks_like_key(field)
        ]
        keys = named_key_fields[:1]
    if not keys:
        # No key named: "把重复的记录去掉" means whole-row duplicates, which is also
        # what Excel's 删除重复项 does when every column stays ticked. Returning nothing
        # here left an authorised action unperformed — the user asked and got silence.
        keys = sorted(available)
    if not keys:
        return []

    strategy = "first"
    if any(token in goal for token in ("保留最后", "最后一条", "keep last")):
        strategy = "last"
    return [{"fields": keys, "strategy": strategy}]


def _validated_deduplication(raw: Any, available: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fields = [str(field) for field in item.get("fields") or []]
        strategy = str(item.get("strategy") or "first").lower()
        order_by = str(item.get("order_by") or "")
        if not fields or any(field not in available for field in fields):
            continue
        if strategy not in {"first", "last", "latest", "earliest"}:
            continue
        if strategy in {"latest", "earliest"} and order_by not in available:
            continue
        requirement: dict[str, Any] = {"fields": fields, "strategy": strategy}
        if order_by:
            requirement["order_by"] = order_by
        _carry_evidence(item, requirement)
        result.append(requirement)
    return result


def _carry_evidence(item: dict[str, Any], requirement: dict[str, Any]) -> None:
    """Keep the model's quote of the goal; it is what authorises a row-removing rule."""

    quote = str(item.get("evidence") or "").strip()
    if quote:
        requirement["evidence"] = quote


def _available_fields(
    tables: dict[str, pd.DataFrame],
    primary: str | None,
) -> set[str]:
    selected = [tables[primary]] if primary in tables else list(tables.values())
    return {str(column) for table in selected for column in table.columns}


# Verbs that authorise removing rows, and which side of the condition survives.
_KEEP_VERBS: tuple[str, ...] = (
    "只保留",
    "保留",
    "留下",
    "只要",
    "仅保留",
    "仅要",
    "筛选出",
    "筛选",
    "选出",
    "挑出",
    "找出",
    "取出",
    "只看",
    "只显示",
    "只输出",
)
_EXCLUDE_VERBS: tuple[str, ...] = (
    "过滤",
    "排除",
    "删除",
    "删掉",
    "去掉",
    "去除",
    "剔除",
    "移除",
    "清除",
    "扣除",
    "不要",
)


def _filter_verb(clause: str) -> bool:
    """Whether the clause actually asks for rows to be removed.

    A bare condition is not an instruction. "金额超过5000的订单" names a set; it was
    executed as "delete everything else", and "看看金额超过5000的订单" — an explicit
    request to *look* — deleted rows too. Removing records is the one operation that
    cannot be undone from the deliverable, so it takes an explicit verb.
    """

    return any(verb in clause for verb in (*_KEEP_VERBS, *_EXCLUDE_VERBS))


def _filter_mode(context: str) -> str:
    """Which side of the condition the user wants kept.

    Position matters, not mere presence: the verb closest to the condition governs.
    "把金额低于500的删掉" used to come back as mode=keep — 删掉 was missing from the
    vocabulary — so the rows the user asked to delete were the only ones kept.
    """

    keep_position = max((context.rfind(verb) for verb in _KEEP_VERBS), default=-1)
    exclude_position = max((context.rfind(verb) for verb in _EXCLUDE_VERBS), default=-1)
    return "exclude" if exclude_position > keep_position else "keep"


def _typed_value(raw: str) -> Any:
    value = raw.strip().strip("\"'")
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


# Comparisons that only mean anything against a number.
_NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_LEADING_NUMBER = re.compile(r"^[-+]?\d[\d,]*(?:\.\d+)?")


def _operand_value(raw: str, operator: str) -> Any | None:
    """The value a comparison clause actually names, or None when it names none.

    Chinese attaches the thing being described to the end of the condition:
    "金额超过5000的订单" means "orders whose 金额 exceeds 5000", so "的订单" is a noun,
    not part of the threshold. The capture is deliberately greedy (values may contain
    almost anything), so the tail has to be trimmed here instead. Without this,
    "只保留金额超过5000的订单" — about as plain as a goal gets — killed the whole job
    with `could not convert string to float: '5000的订单'`.
    """

    text = raw.strip().strip("\"'")
    if not text:
        return None
    if operator in _NUMERIC_OPERATORS:
        match = _LEADING_NUMBER.match(text)
        if match is None:
            # A magnitude comparison against a non-number is never what was meant.
            # Dropping the requirement beats crashing the run or, worse, silently
            # comparing strings and deleting the wrong rows.
            return None
        return _typed_value(match.group(0).replace(",", ""))
    head = text.partition("的")[0]
    return _typed_value(head or text)





# Words that name the operation outright.
DEDUPLICATION_TOKENS: tuple[str, ...] = (
    "去重",
    "除重",
    "排重",
    "dedup",
    "duplicate",
    "distinct",
)
# "重复" on its own is not an instruction — "标出重复的记录" asks to flag them. It only
# means deduplicate when the same clause also says to get rid of them.
_DUPLICATE_REMOVAL_VERBS: tuple[str, ...] = (
    "去掉",
    "去除",
    "删除",
    "删掉",
    "剔除",
    "移除",
    "清除",
    "只保留一条",
    "只留一条",
    "保留一条",
    "只保留最新",
    "只保留第一条",
)


def wants_deduplication(goal: str) -> bool:
    """Whether the goal asks for duplicate records to actually be removed.

    Three call sites each tested for ("去重", "dedup", "duplicate") separately, so
    every phrasing outside those three words did nothing at all: "把重复的记录去掉，
    只保留一条" is about as explicit as a business user gets, and it was ignored.
    Asking for something and getting silence is the same failure as not asking and
    getting an edit — both mean the delivery does not match the request.
    """

    lowered = goal.lower()
    if _deduplication_is_negated(goal):
        return False
    if any(token in lowered or token in goal for token in DEDUPLICATION_TOKENS):
        return True
    return any(
        "重复" in clause and any(verb in clause for verb in _DUPLICATE_REMOVAL_VERBS)
        for clause in _clauses(goal)
    )


def _deduplication_is_negated(goal: str) -> bool:
    lowered = goal.lower()
    return operation_is_negated(goal, DEDUPLICATION_TOKENS) or any(
        token in lowered
        for token in (
            "不去重",
            "不要去重",
            "无需去重",
            "不需要去重",
            "do not deduplicate",
            "no dedup",
            "without deduplication",
        )
    )


def _filter_is_negated(goal: str) -> bool:
    lowered = goal.lower()
    return any(
        token in lowered
        for token in (
            "不筛选",
            "不过滤",
            "不要筛选",
            "不要过滤",
            "无需筛选",
            "无需过滤",
            "保留全部",
            "do not filter",
            "without filtering",
        )
    )


def _question_is_satisfied(
    question: str,
    filters: list[dict[str, Any]],
    deduplication: list[dict[str, Any]],
) -> bool:
    if deduplication and any(
        token in question for token in ("去重", "第一条", "最后一条", "原始行顺序", "保留策略")
    ):
        return True
    if filters and any(
        token in question for token in ("过滤", "筛选", "保留条件", "排除条件")
    ):
        return True
    return False


def _unique_dicts(
    values: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        fingerprint = repr(tuple(value.get(key) for key in keys))
        if fingerprint not in seen:
            result.append(value)
            seen.add(fingerprint)
    return result


def _duplicates_primary_table_slot(question: str, slots: list[MissingSlot]) -> bool:
    if not any(slot.kind == "primary_table" for slot in slots):
        return False
    return any(token in question for token in ("主表", "哪张表", "哪些表", "对应哪张"))


def _slot_kind(question: str) -> MissingSlotKind:
    if any(token in question for token in ("字段", "列", "field", "column")):
        return "target_field"
    if any(token in question for token in ("保留", "有效", "能用", "标准")):
        return "retention_rule"
    if any(token in question for token in ("输出", "图表", "报告", "产物")):
        return "output_requirement"
    return "business_rule"


def _slot_id(question: str) -> str:
    digest = hashlib.sha1(question.encode("utf-8")).hexdigest()[:10]  # noqa: S324
    return f"slot_{digest}"


def _target_schema(goal: str, tables: dict[str, pd.DataFrame]) -> list[str]:
    """The column list the deliverable must match, if the request declares one.

    An uploaded blank template states it structurally; a goal can state it in words.
    The template wins when both are present — someone who uploads a form to fill has
    already been more specific than any sentence.
    """

    _name, columns = template_schema(tables)
    if columns:
        return columns
    available = {str(column) for frame in tables.values() for column in frame.columns}
    return goal_schema(goal, available)


def _schema_target_fields(
    schema: list[str],
    tables: dict[str, pd.DataFrame],
    primary: str | None,
    existing: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Resolve each declared column to the table that actually holds it.

    The base table wins when it has the column — a name that exists on both sides is
    already in hand, and reaching across for it would only risk overwriting it with a
    worse match.
    """

    resolved = list(existing)
    seen = {(item["table"], item["field"]) for item in resolved}
    primary_columns = (
        {str(column) for column in tables[primary].columns} if primary in tables else set()
    )
    for column in schema:
        if column in primary_columns:
            continue
        for table_name, frame in tables.items():
            if table_name == primary:
                continue
            if column in {str(item) for item in frame.columns}:
                key = (str(table_name), str(column))
                if key not in seen:
                    resolved.append({"table": key[0], "field": key[1]})
                    seen.add(key)
                break
    return resolved
