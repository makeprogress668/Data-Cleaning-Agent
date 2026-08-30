from __future__ import annotations

import copy
import re
from typing import Any

import pandas as pd

from data_agent.planning.issue_scope import PROBLEM_REPORT_TOKENS, wants_row_review
from data_agent.planning.provenance import (
    IMPLIED,
    INFERRED,
    STATED,
    operation_is_negated,
    wants_formula_output,
)
from data_agent.schemas.task import TaskSpec
from data_agent.utils.collections import unique
from data_agent.utils.target_schema import goal_schema, template_schema

MAX_FIELD_MATCHES = 30
MAX_TABLE_MATCHES = 8
MAX_SAMPLE_VALUES = 3

FIELD_FAMILIES = {
    "address": {
        "label": "地址",
        "goal_tokens": ("address", "addr", "地址", "收货地址", "详细地址"),
        "field_tokens": ("address", "addr", "地址", "收货地址", "详细地址"),
    },
    "amount": {
        "label": "金额",
        "goal_tokens": ("amount", "price", "cost", "金额", "价格", "费用", "收入"),
        "field_tokens": ("amount", "price", "cost", "金额", "价格", "费用", "收入"),
    },
    "status": {
        "label": "状态",
        "goal_tokens": ("status", "state", "状态", "结果", "阶段"),
        "field_tokens": ("status", "state", "状态", "结果", "阶段"),
    },
    "date": {
        "label": "日期",
        "goal_tokens": ("date", "time", "日期", "时间", "月份", "年度"),
        "field_tokens": ("date", "time", "日期", "时间", "月份", "年度"),
    },
    "phone": {
        "label": "电话",
        "goal_tokens": ("phone", "mobile", "手机号", "电话", "联系方式"),
        "field_tokens": ("phone", "mobile", "手机号", "电话", "联系方式"),
    },
    "email": {
        "label": "邮箱",
        "goal_tokens": ("email", "mail", "邮箱", "邮件"),
        "field_tokens": ("email", "mail", "邮箱", "邮件"),
    },
    "name": {
        "label": "名称",
        "goal_tokens": ("name", "名称", "姓名", "客户", "供应商", "联系人"),
        "field_tokens": ("name", "名称", "姓名", "客户", "供应商", "联系人"),
    },
    "code": {
        "label": "编码",
        "goal_tokens": ("code", "id", "key", "编码", "编号", "单号", "主键"),
        "field_tokens": ("code", "id", "key", "编码", "编号", "单号"),
    },
    "order": {
        "label": "订单",
        "goal_tokens": ("order", "orders", "订单", "销售单", "交易", "明细"),
        "field_tokens": ("order", "orders", "订单", "单号", "交易"),
    },
    "customer": {
        "label": "客户",
        "goal_tokens": ("customer", "client", "客户", "会员", "买家"),
        "field_tokens": ("customer", "client", "客户", "会员", "买家"),
    },
    "product": {
        "label": "商品",
        "goal_tokens": ("product", "sku", "spu", "商品", "产品", "物料"),
        "field_tokens": ("product", "sku", "spu", "商品", "产品", "物料"),
    },
    "site": {
        "label": "站点",
        "goal_tokens": ("site", "sitecode", "building", "站点", "楼宇", "建筑"),
        "field_tokens": ("site", "sitecode", "building", "站点", "楼宇", "建筑"),
    },
}

TABLE_FAMILIES = {
    "order": ("order", "orders", "订单", "销售单", "交易", "明细"),
    "customer": ("customer", "customers", "client", "客户", "会员", "买家"),
    "product": ("product", "products", "sku", "商品", "产品", "物料"),
    "address": ("address", "addr", "地址", "收货地址"),
    "site": ("site", "building", "楼宇", "站点", "sitecode"),
    "asset": ("asset", "assets", "资产", "台账", "设备", "固定资产"),
    "supplier": ("supplier", "vendor", "供应商", "厂商"),
    # NOTE: no "template/import" family here on purpose — an import template is a
    # header-only schema, never a cleaning target, so it must not attract the goal.
}

GOAL_TERM_STOPWORDS = {
    "数据",
    "信息",
    "业务",
    "用户",
    "目标",
    "需求",
    "清洗",
    "处理",
    "分析",
    "统计",
    "合并",
    "匹配",
    "输出",
    "生成",
    "需要",
    "复核",
    "异常",
    "记录",
    "结果",
    "系统",
    "导入",
    "按照",
    "根据",
    "进行",
    "逐步",
}


def interpret_goal(goal: str, tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    normalized_goal = _normalize_text(goal)
    field_matches = _field_matches(normalized_goal, goal, tables)
    matched_fields = [
        {"table": item["table"], "field": item["field"]} for item in field_matches
    ][:MAX_FIELD_MATCHES]
    focus = _focus_from_goal(goal)
    business_views = business_views_from_goal(goal)
    critical_fields = critical_fields_from_goal(goal, tables)
    suggested_base_table = suggest_base_table(goal, tables)
    table_matches = _table_matches(
        normalized_goal=normalized_goal,
        tables=tables,
        field_matches=field_matches,
    )
    requested_outputs = _requested_outputs(goal)
    data_location = _data_location_summary(
        goal=goal,
        tables=tables,
        suggested_base_table=suggested_base_table,
        table_matches=table_matches,
        field_matches=field_matches,
    )
    execution_steps = _execution_steps(
        focus=focus,
        business_views=business_views,
        requested_outputs=requested_outputs,
        suggested_base_table=suggested_base_table,
        field_matches=field_matches,
    )
    return {
        "goal": goal,
        "focus": focus,
        "business_views": business_views,
        "critical_fields": critical_fields,
        "matched_fields": matched_fields,
        "field_matches": field_matches,
        "table_matches": table_matches,
        "data_location": data_location,
        "execution_steps": execution_steps,
        "requested_outputs": requested_outputs,
        "suggested_base_table": suggested_base_table,
    }


def apply_goal_to_job_config(
    job_config: dict[str, Any],
    goal: str,
    tables: dict[str, pd.DataFrame],
    goal_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # When an enriched goal_plan is supplied (LLM-first understanding), reuse its
    # base-table decision instead of recomputing it, so the semantic layer actually
    # drives the plan. Without it, fall back to the deterministic interpretation.
    if goal_plan is None:
        goal_plan = interpret_goal(goal, tables)
    planned = copy.deepcopy(job_config)
    planned["goal"] = goal
    # A request about the *shape of the deliverable*, not about the computation: the
    # values are the same either way, but a formula shows its own work.
    if wants_formula_output(goal):
        planned["formula_output"] = True
    planned["output_mode"] = "business_answer"

    suggested_base = goal_plan.get("suggested_base_table")
    if suggested_base and suggested_base in tables:
        planned["base_table"] = suggested_base

    return apply_goal_semantic_defaults(planned, goal=goal, tables=tables, goal_plan=goal_plan)


def apply_goal_semantic_defaults(
    job_config: dict[str, Any],
    goal: str,
    tables: dict[str, pd.DataFrame],
    goal_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply goal-driven semantic defaults shared by every planning entry point.

    Capabilities are gated by the goal: only what the user actually asked for is
    switched on. A pure clean/standardize goal must not silently enable analysis,
    charts or extra matching modes, otherwise the delivery is padded with columns
    and sections the user never requested. Both the deterministic
    ``apply_goal_to_job_config`` and the richer planner build the same block, so
    centralising it here keeps the two entry points from drifting apart; the
    planner layers its own job-name / input / export / dirty_data / report / audit
    defaults on top of this shared core.

    When an enriched ``goal_plan`` is supplied (LLM-first understanding), its
    validated ``capabilities`` are used instead of recomputing them by keyword, so
    the semantic layer — not the token tables — decides what the plan enables.
    """

    planned = job_config
    if goal_plan and isinstance(goal_plan.get("capabilities"), dict):
        capabilities = goal_plan["capabilities"]
    else:
        capabilities = derive_capabilities(goal, tables)
    planned["business_views"] = business_views_from_goal(goal)

    planned.setdefault("quality_score", {})
    planned["quality_score"] = {
        **planned["quality_score"],
        "enabled": True,
        "critical_fields": critical_fields_from_goal(goal, tables),
    }

    # Matching modes: keep the cheap deterministic modes always; only add fuzzy
    # (expensive, can mis-match) and join-explosion detection when the goal is
    # actually about cross-table matching/merging.
    planned.setdefault("matching", {})
    match_modes = ["exact", "trim", "case_insensitive", "normalized_exact"]
    if capabilities["needs_lookup"]:
        match_modes.append("fuzzy")
    planned["matching"] = {
        **planned["matching"],
        "match_modes": match_modes,
        "duplicate_key_strategy": "review",
    }
    # Detection and editing are separate decisions. A goal that only asks to *flag*
    # problems ("标记异常，不要修改数据") still needs the engine to run — otherwise the
    # plan carries a `validate` action it cannot cover and the whole job is rejected —
    # but it must never rewrite a cell. Only an explicit in-place goal enables fixing.
    planned.setdefault("dirty_data", {})
    wants_detection = capabilities["wants_inplace"] or capabilities["needs_exception_review"]
    planned["dirty_data"] = {
        **planned["dirty_data"],
        "enabled": wants_detection,
        "auto_fix_safe_issues": capabilities["wants_inplace"],
        "mark_uncertain_for_review": capabilities["needs_exception_review"],
    }

    # Whether a row reaches the user is the single most consequential thing this
    # pipeline decides, so it is authorised by the goal — never by a default.
    #
    # These three flags used to be constants on ExceptionPolicyConfig, all defaulted
    # on. "把客户名称关联过来" therefore held back every order whose customer was not
    # in the archive: the user asked for one column to be filled in and silently got
    # back fewer rows than they uploaded, with the missing ones written only to an
    # internal file. Withholding is a judgement about the user's data, and nobody
    # asked for it.
    #
    # Asking to see problems ("列出异常/需要复核的") is what turns it on. Then holding
    # rows back is the point of the request, and they come back on the review sheet.
    planned.setdefault("exception_policy", {})
    # Reporting a problem and pulling the record out of the delivery are separate
    # asks. Tying them together meant "有什么问题" held back 14 of 15 rows.
    withholds_rows = capabilities.get("wants_row_review", False)
    planned["exception_policy"] = {
        **planned["exception_policy"],
        "exclude_critical_from_final": withholds_rows,
        "require_review_for_unmatched_lookup": withholds_rows,
        "include_minor_in_final": True,
    }

    # Analysis + charts are opt-in: only when the goal mentions analysis/stats.
    planned.setdefault("analysis", {})
    planned["analysis"] = {
        **planned["analysis"],
        "enabled": capabilities["needs_analysis"],
    }
    planned.setdefault("charts", {})
    planned["charts"] = {
        **planned["charts"],
        "enabled": capabilities["needs_charts"],
        "formats": ["html"],
    }

    # Anomaly rules exist to mark records unusable, which only makes sense when the
    # goal is about cleaning, validating or reviewing. The recommender proposes them
    # from the data profile regardless, so a pure analysis goal ("按城市统计总金额")
    # ended up with rule-validation steps it never authorised — rejected by the
    # closed-world action check, which aborted the whole run.
    if not (
        capabilities["wants_inplace"]
        or capabilities["wants_annotation"]
        or capabilities["needs_exception_review"]
    ):
        planned["anomaly_rules"] = []

    # Narrow only after optional anomaly rules have been removed. Doing it earlier
    # made discarded diagnostics look like hard field dependencies, so a rollup
    # copied raw detail columns onto the master before adding the requested total.
    planned = _narrow_lookup_fields(planned, goal_plan)
    return apply_task_spec_operations(planned, goal_plan)


# Operators that remove rows. Everything else a task compiles (derived columns)
# only adds information and is never part of the destructive-rule contract.
DESTRUCTIVE_OPERATORS: frozenset[str] = frozenset({"filter_rows", "dedupe"})

_INVERTED_CONDITION_OPS = {
    "gt": "lte",
    "gte": "lt",
    "lt": "gte",
    "lte": "gt",
    "equals": "not_equals",
    "not_equals": "equals",
    "in": "not_in",
    "not_in": "in",
}


def apply_task_spec_operations(
    job_config: dict[str, Any],
    goal_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compile explicit TaskSpec filters and deduplication into JobConfig.

    The goal-understanding layer owns intent. This helper is the single bridge from
    its structured destructive actions to the existing deterministic formula
    executor, and can be re-applied after LLM refinement without duplicating steps.
    """

    if not goal_plan or not goal_plan.get("task_spec"):
        return job_config
    task_spec = TaskSpec.model_validate(goal_plan["task_spec"]).model_dump(mode="json")
    actions = set(task_spec.get("actions") or [])
    output_intent = task_spec.get("output_intent") or {}

    # TaskSpec is the business source of truth. Reapplying it after an LLM JobConfig
    # refinement must restore all execution gates, not only filter/dedupe formulas;
    # otherwise a valid-looking candidate can turn requested cleaning off, enable an
    # unrequested analysis, or change which rows are withheld.
    needs_detection = bool(actions & {"clean", "validate", "review"})
    dirty_data = dict(job_config.get("dirty_data") or {})
    dirty_data.update(
        {
            "enabled": needs_detection,
            "auto_fix_safe_issues": "clean" in actions,
            "mark_uncertain_for_review": "review" in actions,
        }
    )
    job_config["dirty_data"] = dirty_data

    withholds_rows = bool(output_intent.get("withhold_flagged_rows"))
    exception_policy = dict(job_config.get("exception_policy") or {})
    exception_policy.update(
        {
            "exclude_critical_from_final": withholds_rows,
            "require_review_for_unmatched_lookup": withholds_rows,
            "include_minor_in_final": True,
        }
    )
    job_config["exception_policy"] = exception_policy

    analysis = dict(job_config.get("analysis") or {})
    analysis["enabled"] = "analyze" in actions
    job_config["analysis"] = analysis
    charts = dict(job_config.get("charts") or {})
    charts["enabled"] = "chart" in actions
    job_config["charts"] = charts

    formulas = list(job_config.get("formulas") or [])

    schema = [str(name) for name in task_spec.get("target_schema") or [] if str(name)]
    if schema:
        job_config["target_schema"] = schema

    rollups = [
        dict(item) for item in task_spec.get("rollups") or [] if isinstance(item, dict)
    ]
    if rollups:
        job_config["rollups"] = rollups

    melt = task_spec.get("melt")
    if isinstance(melt, dict) and melt.get("value_columns"):
        job_config["melt"] = dict(melt)

    union_tables: list[str] = []
    for requirement in task_spec.get("union") or []:
        if not isinstance(requirement, dict):
            continue
        union_tables.extend(
            str(name) for name in requirement.get("tables") or [] if str(name)
        )
    if union_tables:
        job_config["union_tables"] = unique(union_tables)

    for index, requirement in enumerate(task_spec.get("filters") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        condition = _retention_condition(requirement)
        if not condition or _has_filter_formula(formulas, condition):
            continue
        formulas.append(
            {
                "output": f"_filter_rows_{index}",
                "op": "filter_rows",
                "condition": condition,
                "authorization": _authorization_of(requirement),
            }
        )

    for index, requirement in enumerate(task_spec.get("derivations") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        if str(requirement.get("kind") or "") == "split":
            for formula in _split_formulas(requirement):
                if not _has_formula_output(formulas, formula["output"]):
                    formulas.append(formula)
            continue
        formula = _derivation_formula(requirement, index)
        if formula and not _has_formula_output(formulas, formula["output"]):
            formulas.append(formula)

    for index, requirement in enumerate(
        task_spec.get("deduplication") or [],
        start=1,
    ):
        if not isinstance(requirement, dict):
            continue
        fields = [str(field) for field in requirement.get("fields") or []]
        strategy = str(requirement.get("strategy") or "first")
        order_by = str(requirement.get("order_by") or "")
        if not fields or _has_dedup_formula(formulas, fields, strategy, order_by):
            continue
        formula: dict[str, Any] = {
            "output": f"_deduplicate_rows_{index}",
            "op": "dedupe",
            "columns": fields,
            "value": strategy,
            "authorization": _authorization_of(requirement),
        }
        if order_by:
            formula["source"] = order_by
        formulas.append(formula)

    job_config["formulas"] = formulas
    return job_config


def _authorization_of(requirement: dict[str, Any]) -> str:
    """Carry the TaskSpec's authorization onto the compiled rule.

    Anything without one is treated as inferred, so a requirement that reached here by
    a path that never established the user's intent still stops at the gate.
    """

    declared = str(requirement.get("authorization") or "")
    return declared if declared in {STATED, IMPLIED} else INFERRED


def _derivation_formula(
    requirement: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    """Compile one labelling rule into the executor's existing ``ifs`` operator.

    Branch conditions and their labels line up positionally, and ``otherwise`` becomes
    the default — so a whole "A 标记为 X，其余标记为 Y" rule is one deterministic
    formula and needs no new execution capability.
    """

    output = str(requirement.get("output") or "").strip()
    branches = [
        branch
        for branch in (requirement.get("branches") or [])
        if isinstance(branch, dict)
        and isinstance(branch.get("when"), dict)
        and branch["when"].get("field")
        and branch["when"].get("op")
        and str(branch.get("then") or "").strip()
    ]
    if not output or not branches:
        return None
    return {
        "output": output,
        "op": "ifs",
        "conditions": [
            {
                "field": str(branch["when"]["field"]),
                "op": str(branch["when"]["op"]),
                "value": branch["when"].get("value"),
            }
            for branch in branches
        ],
        "values": [str(branch["then"]).strip() for branch in branches],
        "default": str(requirement.get("otherwise") or ""),
    }


def _has_formula_output(formulas: list[dict[str, Any]], output: str) -> bool:
    return any(
        isinstance(formula, dict) and str(formula.get("output")) == output
        for formula in formulas
    )


def validate_task_spec_operations(
    task_spec: TaskSpec | dict[str, Any],
    job_config: dict[str, Any],
) -> None:
    """Require destructive formulas to match the validated TaskSpec exactly.

    Only row-removing operators are compared. The task also compiles additive
    formulas (derived label columns), and those must not be weighed here: comparing
    every formula made any labelling rule look like a filter/dedupe mismatch.
    """

    validated_task = (
        task_spec
        if isinstance(task_spec, TaskSpec)
        else TaskSpec.model_validate(task_spec)
    )
    expected_config = apply_task_spec_operations(
        {"formulas": []},
        {"task_spec": validated_task.model_dump(mode="json")},
    )
    expected = _destructive_signatures(expected_config.get("formulas", []))
    actual = _destructive_signatures(job_config.get("formulas", []))
    if expected != actual:
        raise ValueError(
            "处理配置中的过滤/去重规则与 TaskSpec 不一致。"
        )


def _destructive_signatures(formulas: list[Any]) -> set[str]:
    return {
        _destructive_formula_signature(formula)
        for formula in formulas
        if isinstance(formula, dict) and formula.get("op") in DESTRUCTIVE_OPERATORS
    }


def _destructive_formula_signature(formula: dict[str, Any]) -> str:
    raw_condition = formula.get("condition")
    condition = (
        {
            "field": raw_condition.get("field"),
            "op": raw_condition.get("op"),
            "value": raw_condition.get("value"),
            "values": list(raw_condition.get("values") or []),
            "case_sensitive": bool(raw_condition.get("case_sensitive", False)),
        }
        if isinstance(raw_condition, dict)
        else None
    )
    payload = {
        "op": formula.get("op"),
        "condition": condition,
        "columns": list(formula.get("columns") or []),
        "value": formula.get("value"),
        "source": formula.get("source"),
    }
    return repr(payload)


def _retention_condition(requirement: dict[str, Any]) -> dict[str, Any]:
    field = str(requirement.get("field") or "")
    op = str(requirement.get("op") or "")
    if not field or not op:
        return {}
    # Negate the match instead of flipping the comparison. "删掉金额小于0的" became
    # "keep 金额 >= 0", which quietly also deleted every row whose 金额 was blank or
    # non-numeric — neither comparison holds for those. Rows that cannot be evaluated
    # were never shown to match, so they stay.
    negate = str(requirement.get("mode") or "keep") == "exclude"
    condition: dict[str, Any] = {"field": field, "op": op, "negate": negate}
    if "value" in requirement:
        condition["value"] = requirement["value"]
    if isinstance(requirement.get("values"), list):
        condition["values"] = list(requirement["values"])
    return condition


def _has_filter_formula(
    formulas: list[dict[str, Any]],
    condition: dict[str, Any],
) -> bool:
    return any(
        formula.get("op") == "filter_rows"
        and formula.get("condition") == condition
        for formula in formulas
        if isinstance(formula, dict)
    )


def _has_dedup_formula(
    formulas: list[dict[str, Any]],
    fields: list[str],
    strategy: str,
    order_by: str,
) -> bool:
    return any(
        formula.get("op") == "dedupe"
        and [str(field) for field in formula.get("columns") or []] == fields
        and str(formula.get("value") or "first") == strategy
        and str(formula.get("source") or "") == order_by
        for formula in formulas
        if isinstance(formula, dict)
    )


# Verbs that decide how results land back on the data. "inplace" edits the target
# fields on the original table (clean/standardize/fill/dedup); "annotation" leaves
# the data untouched and only adds marker columns (flag/classify/find). A goal may
# hit both, but neither is assumed by default — the goal must ask for it.
_INPLACE_TOKENS: tuple[str, ...] = (
    "清洗", "规范", "标准化", "补齐", "补全", "去重", "替换", "修正", "修复",
    "格式化", "转换", "统一", "填充", "clean", "standardize", "normalize",
    "dedup", "fill", "fix", "format",
)
_ANNOTATION_TOKENS: tuple[str, ...] = (
    "标注", "标记", "打标", "识别", "分类", "找出", "筛出", "筛选", "标签",
    "圈出", "标出", "flag", "label", "annotate", "classify", "identify", "mark", "tag",
)
_LOOKUP_TOKENS: tuple[str, ...] = (
    "匹配", "关联", "补齐", "合并", "带回", "join", "lookup", "vlookup", "映射", "对应",
)
_ANALYSIS_TOKENS: tuple[str, ...] = (
    "分析", "统计", "汇总", "看板", "占比", "趋势", "排名", "topn", "top n",
    "分布", "对比", "报表", "dashboard", "analyze", "summary", "aggregate",
)
_CHART_TOKENS: tuple[str, ...] = (
    "图表", "图形", "可视化", "画图", "出图", "趋势图", "柱状图", "折线图",
    "饼图", "散点图", "分布图", "热力图", "评分卡", "chart", "plot", "visual",
)
# Owned by planning.issue_scope, which the executor also reads.
_EXCEPTION_TOKENS = PROBLEM_REPORT_TOKENS



def needs_lookup_from_goal(goal: str) -> bool:
    """Whether the goal itself asks for a cross-table join."""

    normalized = _normalize_text(goal)
    return any(_normalize_text(token) in normalized for token in _LOOKUP_TOKENS)

def derive_capabilities(goal: str, tables: dict[str, pd.DataFrame]) -> dict[str, bool]:
    """Map a natural-language goal to the minimal set of capabilities to enable.

    Returns a flat dict of booleans so both the deterministic defaults and the
    delivery layer can gate behaviour identically. When the goal is empty we fall
    back to a conservative clean-only profile rather than turning everything on.
    """

    normalized = _normalize_text(goal)

    def _hit(tokens: tuple[str, ...]) -> bool:
        return any(_normalize_text(token) in normalized for token in tokens)

    # Whether a join is *possible* is decided later, against the real profile. Gating
    # the intent on table count here meant asking to join a single upload silently
    # produced a plan with no join and no explanation.
    # A declared target schema whose columns are spread across several tables is a
    # lookup request whether or not the goal says 关联: "做一张表，包含订单号、客户名称"
    # names a column the order table does not have, and delivering it blank answers
    # nothing. Only the recommender's own gate reads this, so it has to be decided
    # here rather than later in the task spec.
    needs_lookup = _hit(_LOOKUP_TOKENS) or _schema_spans_tables(goal, tables)
    needs_exception_review = _hit(_EXCEPTION_TOKENS)
    # "分析样本，把空值标出来" uses 分析 as inspect, not as a request for a computed
    # summary. Other aggregate words (趋势/统计/分布...) still win, as does 分析 when
    # the sentence is not solely an exception-review instruction.
    non_generic_analysis_tokens = tuple(
        token for token in _ANALYSIS_TOKENS if token != "分析"
    )
    needs_analysis = _hit(non_generic_analysis_tokens) or (
        "分析" in normalized and not needs_exception_review
    )
    needs_charts = _hit(_CHART_TOKENS) or (needs_analysis and _hit(("看板", "dashboard")))
    # 分类 is both a verb ("给客户分类") and a noun modifier ("合并分类名称"). The
    # latter used to cancel an explicit review handoff because the substring detector
    # mistook a requested lookup field for an annotation operation.
    annotation_text = re.sub(
        r"分类(?:名称|名|编码|代码|字段|信息|表|维度|类别)",
        "",
        normalized,
    )
    wants_annotation = any(
        _normalize_text(token) in annotation_text for token in _ANNOTATION_TOKENS
    )
    wants_inplace = _hit(_INPLACE_TOKENS) and not inplace_operation_is_negated(goal)

    # Editing cells is never a default. This used to fall back to in-place cleaning
    # whenever the goal named nothing else, so "把客户名称关联过来" quietly trimmed
    # whitespace, stripped invisible characters and rewrote full-width text across the
    # whole table. The user asked for one column to be filled in; every other edit was
    # the agent's own idea. Cleaning is a real feature — it just has to be requested.

    return {
        "needs_lookup": needs_lookup,
        "needs_analysis": needs_analysis,
        "needs_charts": needs_charts,
        "needs_exception_review": needs_exception_review,
        # A deterministic annotation request preserves rows. Semantic understanding
        # may still switch wants_row_review on with verbatim evidence when a richer
        # sentence explicitly asks for both marking and a separate usable-data set.
        "wants_row_review": wants_row_review(goal) and not wants_annotation,
        "wants_inplace": wants_inplace,
        "wants_annotation": wants_annotation,
    }


def inplace_operation_is_negated(goal: str) -> bool:
    """Whether all in-place operation mentions are explicitly under negation."""

    return operation_is_negated(goal, _INPLACE_TOKENS)


def business_views_from_goal(goal: str) -> list[str]:
    lowered = goal.lower()
    views = ["standardized_dataset_view", "exception_review_view", "summary_analysis_view"]
    if any(token in lowered or token in goal for token in ("import", "导入", "系统", "erp")):
        views.append("system_import_view")
    if any(token in lowered or token in goal for token in ("发现", "理解", "画像", "discover")):
        views.append("data_discovery_view")
    if any(
        token in lowered or token in goal
        for token in ("统计", "分析", "看板", "图表", "dashboard")
    ):
        views.append("analysis_dashboard_view")
    if any(
        token in lowered or token in goal
        for token in ("复核", "异常", "不能", "失败", "review")
    ):
        views.append("exception_review_view")
    return unique(views)


def critical_fields_from_goal(goal: str, tables: dict[str, pd.DataFrame]) -> list[str]:
    normalized_goal = _normalize_text(goal)
    selected: list[str] = []
    for table in tables.values():
        for column in table.columns:
            column_text = str(column)
            normalized_column = _normalize_text(column_text)
            if normalized_column and normalized_column in normalized_goal:
                selected.append(column_text)
            if _goal_mentions_column_family(normalized_goal, column_text):
                selected.append(column_text)

    for table in tables.values():
        for column in table.columns:
            column_text = str(column)
            lowered = column_text.lower()
            if any(token in lowered for token in ("id", "code", "key", "amount", "date", "status")):
                selected.append(column_text)
            if any(
                token in column_text
                for token in ("编号", "编码", "金额", "日期", "状态", "邮箱", "地址")
            ):
                selected.append(column_text)
    return unique(selected)[:12]


def suggest_base_table(goal: str, tables: dict[str, pd.DataFrame]) -> str | None:
    if not tables:
        return None
    normalized_goal = _normalize_text(goal)
    # A base table is the data to be cleaned, so it must have rows. Empty tables
    # (e.g. an import template that only carries a header) must never win, even
    # when the goal mentions "导入/import" — otherwise anomaly rules run against a
    # 0-row frame that lacks the data columns. Fall back to all tables only if
    # every uploaded table is empty.
    non_empty = {name: table for name, table in tables.items() if len(table) > 0}
    candidates = non_empty or tables
    # A destination phrase settles it outright. 「把订单明细的金额汇总到客户档案」 names
    # both tables, and name-matching alone picked 订单明细 — the source of the numbers
    # rather than the table being filled, so the deliverable was the wrong table.
    destination = _destination_table(goal, candidates)
    if destination:
        return destination
    scored = []
    field_matches = _field_matches(normalized_goal, goal, candidates)
    field_score_by_table: dict[str, int] = {}
    for match in field_matches:
        field_score_by_table[match["table"]] = field_score_by_table.get(match["table"], 0) + int(
            match["score"]
        )
    for table_name, table in candidates.items():
        table_score = 0
        if _normalize_text(table_name) in normalized_goal:
            table_score += 1000
        table_score += _table_semantic_score(normalized_goal, str(table_name))[0]
        # Prefer the primary fact/detail table (the cleaning subject) over dimension
        # tables when both match the goal's vocabulary. A goal often names both the
        # data to clean and the desired output view (e.g. "清洗 sitecode 数据并输出
        # 活跃楼宇清单"), and the raw/main/detail table is the one to operate on.
        lowered_name = str(table_name).lower()
        if any(token in lowered_name for token in ("raw", "main", "detail", "明细", "原始")):
            table_score += 120
        for column in table.columns:
            column_text = str(column)
            normalized_column = _normalize_text(column_text)
            if normalized_column and normalized_column in normalized_goal:
                table_score += 80
            if _goal_mentions_column_family(normalized_goal, column_text):
                table_score += 50
        table_score += min(field_score_by_table.get(table_name, 0), 240)
        if _named_as_lookup_source(goal, str(table_name)):
            # "把所属大区从客户档案关联过来" names 客户档案 as where the field comes
            # *from*. Merely being mentioned used to be worth +1000, so the dimension
            # table won and the fact table got joined into it — 3 orders came back as
            # 2 rows, one per customer, with no warning anywhere. Rows the user never
            # asked to lose are the worst thing this product can do, so a table the
            # goal points at as a source is disqualified from being the subject.
            table_score -= 2000
        scored.append((table_score, len(table), table_name))

    selected = sorted(scored, reverse=True)[0]
    if selected[0] <= 0:
        return None
    return _prefer_detail_side(str(selected[2]), candidates, goal)


def _prefer_detail_side(
    selected: str,
    candidates: dict[str, pd.DataFrame],
    goal: str,
) -> str:
    """When rows are about to be joined, the subject is the side holding the detail.

    Wording alone picks the wrong side constantly. "把订单明细和客户档案关联起来，补上
    客户名称、所属大区" names both and the archive wins because two of its columns get
    mentioned; "把客户名称关联过来" names neither and the archive wins for the same
    reason. Either way the orders get joined *into* the customers — one row per
    customer, every other order gone, and the user asked for a column to be filled in,
    not for their data to be collapsed.

    Whichever side repeats the shared key is the detail table, so that is the subject
    and the other is the lookup. Naming exactly one table still overrides this: asking
    to work on the archive is a legitimate thing to want.
    """

    # Only applies when the goal actually asks to join. "清洗客户档案，参考订单明细"
    # names both tables but the subject really is the archive, and nothing is joined
    # into it, so there are no rows to lose.
    if not any(token in goal.lower() or token in goal for token in _LOOKUP_TOKENS):
        return selected

    # A table the goal names, and does not name as a source, is the stated subject.
    # Chinese puts the object of the action first ("清洗客户档案，参考订单明细"), so the
    # earliest mention is the one being worked on. Word-count scoring got this wrong
    # whenever the goal happened to mention more of the other table's columns.
    named = sorted(
        (_normalize_text(goal).find(_normalize_text(str(name))), str(name))
        for name in candidates
        if _normalize_text(str(name)) in _normalize_text(goal)
        and not _named_as_lookup_source(goal, str(name))
    )
    if named:
        return named[0][1]

    chosen = candidates[selected]
    for other_name in candidates:
        if other_name == selected:
            continue
        other = candidates[other_name]
        shared = [column for column in chosen.columns if column in set(other.columns)]
        for column in shared:
            if _is_unique_key(chosen, column) and not _is_unique_key(other, column):
                return str(other_name)
    return selected


def _is_unique_key(table: pd.DataFrame, column: str) -> bool:
    populated = table.dropna(subset=[column])
    return bool(len(populated)) and table[column].nunique(dropna=True) == len(populated)


# "从/根据 <表名> …… 关联/匹配/带出/补齐" — the table supplying fields, not the subject.
_LOOKUP_SOURCE_MARKERS: tuple[str, ...] = (
    "关联",
    "匹配",
    "带出",
    "补齐",
    "补上",
    "取数",
    "vlookup",
)
_LOOKUP_SOURCE_PREFIXES: tuple[str, ...] = ("从", "根据", "依据", "用", "按照")


def _named_as_lookup_source(goal: str, table_name: str) -> bool:
    for prefix in _LOOKUP_SOURCE_PREFIXES:
        marker = f"{prefix}{table_name}"
        position = goal.find(marker)
        while position >= 0:
            tail = goal[position + len(marker) : position + len(marker) + 20]
            if any(token in tail.lower() for token in _LOOKUP_SOURCE_MARKERS):
                return True
            position = goal.find(marker, position + 1)
    return False


def _field_matches(
    normalized_goal: str,
    goal: str,
    tables: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    rows = []
    for table_name, table in tables.items():
        for column in table.columns:
            column_text = str(column)
            score, evidence, matched_terms, match_type = _field_match_score(
                normalized_goal,
                goal,
                table,
                column_text,
            )
            if score <= 0:
                continue
            rows.append(
                {
                    "table": str(table_name),
                    "field": column_text,
                    "score": score,
                    "confidence": _confidence(score),
                    "match_type": match_type,
                    "evidence": evidence,
                    "matched_terms": sorted(matched_terms),
                    "sample_values": _sample_values(table[column]),
                }
            )
    rows.sort(key=lambda row: (int(row["score"]), row["table"], row["field"]), reverse=True)
    return rows[:MAX_FIELD_MATCHES]


def _table_matches(
    normalized_goal: str,
    tables: dict[str, pd.DataFrame],
    field_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    field_score_by_table: dict[str, int] = {}
    matched_terms_by_table: dict[str, set[str]] = {}
    for match in field_matches:
        table = match["table"]
        field_score_by_table[table] = field_score_by_table.get(table, 0) + int(match["score"])
        matched_terms_by_table.setdefault(table, set()).update(match.get("matched_terms", []))

    rows = []
    for table_name, table in tables.items():
        table_text = str(table_name)
        table_score, table_evidence, table_terms = _table_semantic_score(
            normalized_goal,
            table_text,
        )
        direct_name_match = _normalize_text(table_text) in normalized_goal
        if direct_name_match:
            table_score += 160
            table_evidence.append("目标直接提到表名")
            table_terms.add(table_text)

        field_score = min(field_score_by_table.get(table_text, 0), 260)
        total_score = table_score + field_score
        if total_score <= 0:
            continue
        matched_fields = [
            match["field"] for match in field_matches if match["table"] == table_text
        ][:8]
        terms = matched_terms_by_table.get(table_text, set()) | table_terms
        reason = _table_reason(table_evidence, matched_fields)
        rows.append(
            {
                "table": table_text,
                "score": int(total_score),
                "confidence": _confidence(total_score),
                "row_count": int(len(table)),
                "column_count": int(len(table.columns)),
                "matched_fields": matched_fields,
                "matched_terms": sorted(term for term in terms if term),
                "reason": reason,
            }
        )
    rows.sort(key=lambda row: (int(row["score"]), int(row["row_count"])), reverse=True)
    return rows[:MAX_TABLE_MATCHES]


def _focus_from_goal(goal: str) -> list[str]:
    lowered = goal.lower()
    focus = []
    if any(token in lowered or token in goal for token in ("清洗", "clean")):
        focus.append("数据清洗")
    if any(
        token in lowered or token in goal
        for token in ("匹配", "合并", "lookup", "join", "关联")
    ):
        focus.append("多表匹配")
    if any(
        token in lowered or token in goal
        for token in ("异常", "不能", "失败", "复核", "review")
    ):
        focus.append("异常识别与复核")
    if any(token in lowered or token in goal for token in ("导入", "系统", "erp", "import")):
        focus.append("系统导入结果")
    if any(token in lowered or token in goal for token in ("分析", "统计", "图表", "看板")):
        focus.append("分析看板")
    return focus or ["通用数据处理"]


def _requested_outputs(goal: str) -> list[str]:
    lowered = goal.lower()
    outputs = ["最终结果 Excel"]
    if any(token in lowered or token in goal for token in ("异常", "复核", "不能", "失败")):
        outputs.append("异常复核清单")
    if any(token in lowered or token in goal for token in ("导入", "系统", "import", "erp")):
        outputs.append("系统导入视图")
    if any(token in lowered or token in goal for token in ("分析", "统计", "图表", "看板")):
        outputs.append("分析看板")
    if any(
        token in lowered or token in goal
        for token in ("报告", "业务结论", "结论说明", "report")
    ):
        outputs.append("业务报告")
    return unique(outputs)


def _field_match_score(
    normalized_goal: str,
    goal: str,
    table: pd.DataFrame,
    column: str,
) -> tuple[int, list[str], set[str], str]:
    normalized_column = _normalize_text(column)
    score = 0
    evidence: list[str] = []
    matched_terms: set[str] = set()
    match_types: list[str] = []

    if normalized_column and normalized_column in normalized_goal:
        score += 180
        evidence.append("目标直接提到字段名")
        matched_terms.add(column)
        match_types.append("field_name")

    family_score, family_evidence, family_terms = _field_family_score(normalized_goal, column)
    if family_score:
        score += family_score
        evidence.extend(family_evidence)
        matched_terms.update(family_terms)
        match_types.append("field_family")

    semantic_score, semantic_terms = _goal_term_score(goal, column)
    if semantic_score:
        score += semantic_score
        evidence.append("字段名与目标业务词有交集")
        matched_terms.update(semantic_terms)
        match_types.append("semantic_token")

    sample_score, sample_terms = _sample_value_score(normalized_goal, table[column])
    if sample_score:
        score += sample_score
        evidence.append("字段样本值出现在目标中")
        matched_terms.update(sample_terms)
        match_types.append("sample_value")

    if not evidence:
        return 0, [], set(), "none"

    return int(score), unique(evidence), matched_terms, "+".join(unique(match_types))


def _field_family_score(
    normalized_goal: str,
    column: str,
) -> tuple[int, list[str], set[str]]:
    lowered_column = column.lower()
    normalized_column = _normalize_text(column)
    score = 0
    evidence = []
    matched_terms: set[str] = set()
    for family in FIELD_FAMILIES.values():
        goal_tokens = tuple(str(token) for token in family["goal_tokens"])
        field_tokens = tuple(str(token) for token in family["field_tokens"])
        goal_hits = [
            token for token in goal_tokens if _normalize_text(token) in normalized_goal
        ]
        field_hits = [
            token
            for token in field_tokens
            if token.lower() in lowered_column or _normalize_text(token) in normalized_column
        ]
        if not goal_hits or not field_hits:
            continue
        score += 95
        evidence.append(f"目标提到{family['label']}类信息，字段名属于同类字段")
        matched_terms.update(goal_hits)
    return score, evidence, matched_terms


def _table_semantic_score(
    normalized_goal: str,
    table_name: str,
) -> tuple[int, list[str], set[str]]:
    lowered_table = table_name.lower()
    normalized_table = _normalize_text(table_name)
    score = 0
    evidence = []
    matched_terms: set[str] = set()
    for tokens in TABLE_FAMILIES.values():
        goal_hits = [token for token in tokens if _normalize_text(token) in normalized_goal]
        table_hits = [
            token
            for token in tokens
            if token.lower() in lowered_table or _normalize_text(token) in normalized_table
        ]
        if not goal_hits or not table_hits:
            continue
        score += 140
        evidence.append("表名与目标业务对象一致")
        matched_terms.update(goal_hits)
    return score, unique(evidence), matched_terms


def _goal_term_score(goal: str, column: str) -> tuple[int, set[str]]:
    terms = _goal_terms(goal)
    normalized_column = _normalize_text(column)
    matched = {
        term
        for term in terms
        if len(term) >= 2 and _normalize_text(term) and _normalize_text(term) in normalized_column
    }
    return min(len(matched) * 35, 105), matched


def _sample_value_score(normalized_goal: str, series: pd.Series) -> tuple[int, set[str]]:
    matched = set()
    for value in series.dropna().astype(str).head(20):
        normalized_value = _normalize_text(value)
        if len(normalized_value) < 2:
            continue
        if normalized_value in normalized_goal:
            matched.add(value[:40])
        if len(matched) >= 3:
            break
    return min(len(matched) * 25, 75), matched


def _goal_terms(goal: str) -> set[str]:
    ascii_terms = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", goal)
        if token.lower() not in GOAL_TERM_STOPWORDS
    }
    chinese_terms = {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", goal)
        if token not in GOAL_TERM_STOPWORDS
    }
    family_terms = {
        str(token)
        for family in FIELD_FAMILIES.values()
        for token in family["goal_tokens"]
        if _normalize_text(str(token)) in _normalize_text(goal)
    }
    return ascii_terms | chinese_terms | family_terms


def _sample_values(series: pd.Series) -> list[str]:
    values = []
    for value in series.dropna().astype(str).head(20):
        cleaned = value.strip()
        if not cleaned or cleaned in values:
            continue
        values.append(cleaned[:80])
        if len(values) >= MAX_SAMPLE_VALUES:
            break
    return values


def _confidence(score: int | float) -> str:
    if score >= 180:
        return "high"
    if score >= 95:
        return "medium"
    return "low"


def _table_reason(evidence: list[str], matched_fields: list[str]) -> str:
    parts = list(evidence)
    if matched_fields:
        parts.append(f"命中字段：{', '.join(matched_fields[:5])}")
    return "；".join(unique(parts)) or "目标语义与表结构存在弱相关"


def _data_location_summary(
    goal: str,
    tables: dict[str, pd.DataFrame],
    suggested_base_table: str | None,
    table_matches: list[dict[str, Any]],
    field_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    high_confidence_fields = [
        f"{match['table']}.{match['field']}"
        for match in field_matches
        if match.get("confidence") == "high"
    ]
    target_tables = [match["table"] for match in table_matches[:5]]
    if suggested_base_table and suggested_base_table not in target_tables:
        target_tables.insert(0, suggested_base_table)

    if high_confidence_fields:
        summary = (
            "已在上传数据中定位到与目标直接相关的字段："
            f"{'、'.join(high_confidence_fields[:6])}。"
        )
    elif table_matches:
        summary = (
            "已根据表名和字段语义定位到候选数据范围："
            f"{'、'.join(target_tables[:5])}。"
        )
    else:
        summary = "暂未发现与目标强匹配的字段，将按通用画像、清洗、匹配和复核流程处理。"

    if suggested_base_table:
        summary += f" 建议以 `{suggested_base_table}` 作为主表开展处理。"

    return {
        "summary": summary,
        "target_tables": target_tables,
        "primary_table": suggested_base_table,
        "high_confidence_fields": high_confidence_fields[:10],
        "coverage": {
            "uploaded_table_count": len(tables),
            "matched_table_count": len(table_matches),
            "matched_field_count": len(field_matches),
            "goal_term_count": len(_goal_terms(goal)),
        },
    }


def _execution_steps(
    focus: list[str],
    business_views: list[str],
    requested_outputs: list[str],
    suggested_base_table: str | None,
    field_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched_field_refs = [f"{item['table']}.{item['field']}" for item in field_matches[:6]]
    steps = [
        {
            "step": 1,
            "name": "目标定位",
            "objective": "把业务目标落到具体表和字段",
            "inputs": ["用户业务目标", "上传文件画像"],
            "outputs": ["目标字段命中结果", "推荐主表"],
            "evidence": matched_field_refs
            or ([f"推荐主表：{suggested_base_table}"] if suggested_base_table else []),
        },
        {
            "step": 2,
            "name": "数据画像",
            "objective": "检查字段完整性、唯一性、跨表关系和基础质量问题",
            "inputs": [suggested_base_table or "全部上传表"],
            "outputs": ["表画像", "候选匹配键", "基础质量问题"],
            "evidence": [],
        },
    ]

    next_step = 3
    if "多表匹配" in focus:
        steps.append(
            {
                "step": next_step,
                "name": "跨表匹配",
                "objective": "按照候选 key 合并维表字段并标记未匹配记录",
                "inputs": ["主表", "候选维表"],
                "outputs": ["补齐后的业务明细", "未匹配复核项"],
                "evidence": matched_field_refs,
            }
        )
        next_step += 1

    if "数据清洗" in focus or "通用数据处理" in focus:
        steps.append(
            {
                "step": next_step,
                "name": "规则清洗",
                "objective": "修复安全的脏数据，保留不确定记录供人工复核",
                "inputs": ["原始数据", "文档规则", "字段画像"],
                "outputs": ["标准化数据", "字段变更审计"],
                "evidence": [],
            }
        )
        next_step += 1

    if "异常识别与复核" in focus:
        steps.append(
            {
                "step": next_step,
                "name": "异常分层",
                "objective": "按严重程度区分可交付记录和需业务确认记录",
                "inputs": ["清洗后数据", "异常规则", "匹配结果"],
                "outputs": ["needs_review", "异常复核清单"],
                "evidence": [],
            }
        )
        next_step += 1

    if "分析看板" in focus or "analysis_dashboard_view" in business_views:
        steps.append(
            {
                "step": next_step,
                "name": "分析汇总",
                "objective": "按目标自动生成指标、TopN 和可视化摘要",
                "inputs": ["处理结果", "需复核数据"],
                "outputs": ["analysis_summary", "charts"],
                "evidence": [],
            }
        )
        next_step += 1

    steps.append(
        {
            "step": next_step,
            "name": "交付输出",
            "objective": "生成用户可下载和可复核的结果包",
            "inputs": ["最终可用数据", "异常复核数据", "业务结论"],
            "outputs": requested_outputs,
            "evidence": [],
        }
    )
    return steps


def _goal_mentions_column_family(normalized_goal: str, column: str) -> bool:
    normalized_column = _normalize_text(column)
    lowered_column = column.lower()
    for family in FIELD_FAMILIES.values():
        goal_tokens = tuple(str(token) for token in family["goal_tokens"])
        field_tokens = tuple(str(token) for token in family["field_tokens"])
        if any(_normalize_text(token) in normalized_goal for token in goal_tokens) and any(
            token.lower() in lowered_column or _normalize_text(token) in normalized_column
            for token in field_tokens
        ):
            return True
    return False


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _narrow_lookup_fields(
    job_config: dict[str, Any],
    goal_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bring back only the columns the goal named.

    The recommender proposes every column the related table has that the main table
    lacks — the most useful default when nobody said what they wanted. But asked for
    「把客户名称关联过来」 it also delivered 所属大区、联系电话、开户行, columns the user
    never mentioned and now has to explain to whoever reads the file.

    Naming nothing ("关联客户档案") is not the same as naming nothing useful: with no
    field named there is nothing to narrow to, so the broad default stands.
    """

    task_spec = (goal_plan or {}).get("task_spec")
    if not isinstance(task_spec, dict):
        return job_config
    wanted_by_table: dict[str, set[str]] = {}
    for item in task_spec.get("target_fields") or []:
        if isinstance(item, dict) and item.get("table") and item.get("field"):
            wanted_by_table.setdefault(str(item["table"]), set()).add(str(item["field"]))
    if not wanted_by_table:
        return job_config

    objective = str(task_spec.get("objective") or "").lower()
    rollup_measures: dict[str, set[str]] = {}
    for rollup in task_spec.get("rollups") or []:
        if not isinstance(rollup, dict):
            continue
        source = str(rollup.get("source_table") or "")
        measure = str(rollup.get("measure") or "")
        if source and measure:
            rollup_measures.setdefault(source, set()).add(measure)

    lookups = job_config.get("lookups")
    if not isinstance(lookups, list):
        return job_config
    needed = _fields_the_plan_depends_on(job_config)
    narrowed = []
    for lookup in lookups:
        if not isinstance(lookup, dict):
            continue
        source_table = str(lookup.get("source_table") or "")
        wanted = wanted_by_table.get(source_table)
        fields = [str(field) for field in lookup.get("fields") or []]
        if source_table in rollup_measures:
            # A rollup reads a detail measure; it does not ask to copy raw detail
            # columns onto the master. Deterministic field-family matching can label
            # 订单号 as a target merely because the table is called 订单明细, so retain
            # only separately named, non-measure fields or actual plan dependencies.
            explicitly_copied = {
                field
                for field in (wanted or set())
                if field not in rollup_measures[source_table]
                and field.lower() in objective
            }
            kept = [
                field for field in fields
                if field in explicitly_copied or field in needed
            ]
            if not kept:
                continue
            lookup = {**lookup, "fields": kept}
            narrowed.append(lookup)
            continue
        if wanted:
            kept = [field for field in fields if field in wanted or field in needed]
            # Every wanted field missing means the goal named this table for something
            # else — the key, say. Narrowing to nothing would silently drop the lookup.
            if kept:
                lookup = {**lookup, "fields": kept}
        narrowed.append(lookup)
    job_config["lookups"] = narrowed
    return job_config


def _fields_the_plan_depends_on(job_config: dict[str, Any]) -> set[str]:
    """Columns the rest of the plan reads, whether or not the goal named them.

    Narrowing a lookup to the goal's own words is right for the *deliverable*, but a
    validation rule or formula that reads a brought-across column still needs it to
    exist. Quality scoring is intentionally excluded: diagnostics must score the
    delivered schema, never expand that schema with fields the user did not request.
    Trimming hard dependencies away turned a working plan into
    「校验规则引用了结果中不存在的字段」.
    """

    needed: set[str] = set()
    for rule in job_config.get("anomaly_rules") or []:
        condition = (rule or {}).get("condition") or {}
        field = str(condition.get("field") or "").strip()
        if field:
            needed.add(field)
    for formula in job_config.get("formulas") or []:
        if not isinstance(formula, dict):
            continue
        source = str(formula.get("source") or "").strip()
        if source:
            needed.add(source)
        needed.update(str(column) for column in formula.get("columns") or [])
        condition = formula.get("condition") or {}
        field = str(condition.get("field") or "").strip()
        if field:
            needed.add(field)
    pivot = job_config.get("pivot") or {}
    needed.update(str(field) for field in pivot.get("group_by") or [])
    for metric in pivot.get("metrics") or []:
        column = str((metric or {}).get("column") or "").strip()
        if column:
            needed.add(column)
    return needed


def _schema_spans_tables(goal: str, tables: dict[str, pd.DataFrame]) -> bool:
    """Whether a declared target schema needs columns from more than one table."""

    _name, columns = template_schema(tables)
    if not columns:
        available = {str(column) for frame in tables.values() for column in frame.columns}
        columns = goal_schema(goal, available)
    if not columns:
        return False
    holders = {
        name
        for name, frame in tables.items()
        if any(str(column) in columns for column in frame.columns)
    }
    return len(holders) > 1


def _split_formulas(requirement: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``split`` formula per produced column.

    The executor's split operator returns a single part per call, which is the right
    shape: each produced column is its own declared output, so the delivery contract
    can name them and the output linter can check them like any other field.
    """

    source = str(requirement.get("source") or "").strip()
    separator = str(requirement.get("sep") or "")
    outputs = [str(name).strip() for name in requirement.get("outputs") or [] if str(name).strip()]
    if not source or not separator or len(outputs) < 2:
        return []
    return [
        {
            "output": name,
            "op": "split",
            "source": source,
            "sep": separator,
            "value": position,
            "default": "",
        }
        for position, name in enumerate(outputs)
    ]


# 「汇总到客户档案」「填到主表」「写入结果表」—— 目的地就是要交付的那张表。
_DESTINATION_PREFIXES: tuple[str, ...] = (
    "汇总到", "填到", "填充到", "写到", "写入", "补充到", "回填到", "追加到", "到",
)


def _destination_table(goal: str, tables: dict[str, pd.DataFrame]) -> str | None:
    """The table a goal says to write into, when it says so."""

    text = str(goal)
    best: tuple[int, str] | None = None
    for prefix in _DESTINATION_PREFIXES:
        start = 0
        while True:
            position = text.find(prefix, start)
            if position < 0:
                break
            tail = text[position + len(prefix) :]
            for name in sorted(tables, key=len, reverse=True):
                if tail.startswith(str(name)):
                    length = len(str(name))
                    if best is None or length > best[0]:
                        best = (length, str(name))
                    break
            start = position + len(prefix)
    return best[1] if best else None
