"""LLM-first goal understanding with a deterministic fallback.

Phase A of the intelligence upgrade. The deterministic ``interpret_goal`` maps a
goal to focus/fields/base-table purely by keyword/substring scoring, which fails on
paraphrases and unfamiliar business domains. This layer wraps it: when an LLM is
configured, the model reads the goal against the *real* tables/columns and returns
a semantic understanding (focus, capabilities, base table, target fields,
clarification questions). That overlay is validated against the actual schema — any
unknown table/field is dropped — and merged onto the deterministic baseline.

Safety: the model receives a policy-controlled DataContext (metadata, masked samples,
or explicitly trusted samples) but never executes transformations itself. Every
downstream JobConfig still passes whitelist + schema + field validation. When no LLM
is configured (or anything fails), we return the deterministic ``interpret_goal``
result.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

import pandas as pd

from data_agent.agent.data_context import build_column_context
from data_agent.agent.llm_client import build_default_llm_planner, llm_model_name
from data_agent.agent.prompts import GOAL_PROMPT_VERSION, GOAL_UNDERSTANDING_SYSTEM_PROMPT
from data_agent.agent.task_spec import build_task_spec
from data_agent.agent.understanding_cache import cache_key, load_entry, store_entry
from data_agent.planning.goal_interpreter import (
    derive_capabilities,
    inplace_operation_is_negated,
    interpret_goal,
)
from data_agent.planning.provenance import schema_names, verify_evidence

if TYPE_CHECKING:
    # Import for typing only; a runtime import would create an agent.planner <->
    # agent.goal_understanding cycle (planner imports this module).
    from data_agent.agent.planner import LLMPlannerCallable

logger = logging.getLogger(__name__)

# Capabilities whose effect is to change the user's cells or withhold their rows. The
# model may turn one ON only by quoting the goal span that asks for it, exactly as it
# must for a filter — that is how a phrasing the token table misses ("把这份表整理一下")
# still gets acted on. It may never turn one OFF, because the deterministic layer only
# set it after finding the user's own word in the goal, and overriding that erased an
# explicit request: asked to "清洗订单明细，去掉前后空格", the model cleared wants_inplace
# and the cleaning silently never happened.
#
# Output capabilities also require a quote. They do not mutate source cells, but a
# false positive still changes the product contract by adding sheets/reports the user
# never requested. Evidence lets semantic paraphrases work without returning that
# authority to an inevitably incomplete keyword table.
_EVIDENCE_CAPABILITIES = frozenset(
    {
        "needs_analysis",
        "needs_charts",
        "needs_exception_review",
        "wants_inplace",
        "wants_row_review",
        "wants_annotation",
        "needs_change_manifest",
        "needs_report",
    }
)
# The capability flags the overlay may set; anything else is ignored. Mirrors the
# keys derive_capabilities() returns so the plan builder consumes one shape.
_CAPABILITY_KEYS = (
    "needs_lookup",
    "needs_analysis",
    "needs_charts",
    "needs_exception_review",
    "wants_inplace",
    "wants_row_review",
    "wants_annotation",
    "needs_change_manifest",
    "needs_report",
)

# List-valued overlay keys the LLM may sharpen. Kept minimal so the model can only
# refine intent, never restructure the deterministic evidence layer.
_LIST_OVERLAY_KEYS = ("focus", "business_views", "requested_outputs")
_STRUCTURED_OVERLAY_KEYS = ("filters", "deduplication")

def understand_goal(
    goal: str,
    tables: dict[str, pd.DataFrame],
    *,
    column_profile: Optional[pd.DataFrame] = None,
    llm_planner: Optional[LLMPlannerCallable] = None,
) -> dict[str, Any]:
    """Return an enriched goal plan: deterministic baseline + optional LLM overlay.

    The result is always a superset of ``interpret_goal``'s dict, plus
    ``capabilities``, ``understanding_source``, ``clarification_questions`` and
    ``assumptions``. Callers pass it into ``apply_goal_to_job_config(..., goal_plan=...)``
    so understanding actually gates the plan instead of only decorating the response.

    ``understanding_source`` distinguishes four states, because they mean different
    things to whoever reads the result:

    ``llm``                    the model read the goal against the real schema
    ``llm_cached``             an identical earlier run's understanding was reused
    ``deterministic``          no model is configured; keyword interpretation by design
    ``deterministic_fallback`` a model *is* configured but could not be reached

    The last two used to be one value, so a timeout was indistinguishable from a
    deliberate rules-only deployment — and the run that quietly understood less had
    nothing in it to say so.
    """

    base_plan = interpret_goal(goal, tables)
    capabilities = derive_capabilities(goal, tables)
    columns_context, data_access = build_column_context(
        tables,
        column_profile=column_profile,
    )
    deterministic = {
        **base_plan,
        "capabilities": capabilities,
        "understanding_source": "deterministic",
        "clarification_questions": [],
        "assumptions": [],
        "data_access": data_access,
    }

    if not goal or not goal.strip():
        return _finalize(deterministic, goal, tables)

    planner = llm_planner or build_default_llm_planner()
    if planner is None:
        return _finalize(deterministic, goal, tables)

    key = cache_key(
        goal,
        tables,
        prompt_version=GOAL_PROMPT_VERSION,
        model=llm_model_name(),
        data_access_mode=str(data_access.get("mode") or ""),
    )
    cached = load_entry(key)
    if cached is not None:
        # Reuse before calling: the point is that the same request cannot drift, and a
        # replay must survive the model being down. data_access describes this run's
        # policy, so it is re-attached rather than restored.
        cached["understanding_source"] = "llm_cached"
        cached["data_access"] = data_access
        return _finalize(cached, goal, tables)

    try:
        messages = _build_messages(goal, tables, columns_context, data_access)
        raw_response = planner(messages)
        overlay = _parse_overlay(raw_response)
    except Exception as exc:  # noqa: BLE001 - any LLM/transport/parse failure is non-fatal
        logger.warning("目标语义理解调用失败，回退到确定性理解：%s", exc)
        deterministic["understanding_source"] = "deterministic_fallback"
        deterministic["understanding_fallback_reason"] = str(exc)
        return _finalize(deterministic, goal, tables)

    if not overlay:
        deterministic["understanding_source"] = "deterministic_fallback"
        deterministic["understanding_fallback_reason"] = "模型未返回可用的结构化理解结果"
        return _finalize(deterministic, goal, tables)

    merged = _finalize(_merge_overlay(deterministic, overlay, tables, goal), goal, tables)
    store_entry(key, merged)
    return merged


def _finalize(
    plan: dict[str, Any],
    goal: str,
    tables: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Attach the two client-facing visibility signals to any goal plan.

    Runs on every return path (rule-only, LLM overlay, and fallback) because
    ``understand_goal`` is the single merge point for both sources, so the console
    can always tell the user *how* the goal was located and whether the agent had to
    fall back to generic cleaning — instead of a mislocation passing silently.
    """

    plan["understanding_confidence"] = _understanding_confidence(plan)
    plan["is_generic_fallback"] = _is_generic_fallback(plan)
    if goal.strip():
        plan["task_spec"] = build_task_spec(goal, plan, tables).model_dump(mode="json")
    return plan


def _understanding_confidence(plan: dict[str, Any]) -> str:
    """Aggregate the scattered per-field/table evidence into one overall grade.

    High when the plan pinned down concrete fields (a high-confidence deterministic
    match, or real LLM-validated ``target_fields``); medium when it at least matched
    a candidate table; low when neither held, i.e. the goal never grounded onto the
    data.
    """

    data_location = plan.get("data_location") or {}
    if data_location.get("high_confidence_fields") or plan.get("target_fields"):
        return "high"
    if plan.get("table_matches"):
        return "medium"
    return "low"


def _is_generic_fallback(plan: dict[str, Any]) -> bool:
    """True when the goal could not be located and the run defaults to generic
    cleaning — no table matched and no base table was chosen."""

    return not plan.get("table_matches") and not plan.get("suggested_base_table")


def _build_messages(
    goal: str,
    tables: dict[str, pd.DataFrame],
    columns_context: list[dict[str, Any]],
    data_access: dict[str, Any],
) -> list[dict[str, str]]:
    context = {
        "goal": goal,
        "available_tables": [
            {"table": name, "row_count": int(len(frame)), "column_count": int(frame.shape[1])}
            for name, frame in tables.items()
        ],
        "available_columns": columns_context,
        "data_access": data_access,
        "contract": {
            "rules": [
                "Use only the table names in available_tables.",
                "Use only the columns in available_columns; never invent fields.",
                "Respect data_access: samples are policy-controlled context, not a "
                "complete copy of the dataset.",
                "Understand intent by meaning, not by exact keywords.",
                "Ask a clarification_question when the target table/field/key is "
                "ambiguous or missing instead of guessing.",
            ],
        },
    }
    return [
        {"role": "system", "content": GOAL_UNDERSTANDING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Understand the goal below against the real tables and columns. "
                "Return strict JSON only in the required shape:\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"
            ),
        },
    ]


def _parse_overlay(payload: Any) -> dict[str, Any]:
    text = str(payload).strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("目标语义理解返回的不是合法 JSON，回退到确定性理解：%s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_overlay(
    deterministic: dict[str, Any],
    overlay: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    goal: str,
) -> dict[str, Any]:
    """Validate the overlay against the real schema and merge it onto the baseline.

    The LLM wins for intent fields (focus / views / outputs / base table /
    capabilities); the deterministic evidence layer (field_matches, table_matches,
    data_location, execution_steps) is preserved untouched.
    """

    merged = dict(deterministic)
    merged["understanding_source"] = "llm"

    # Intent lists: accept only lists of strings; otherwise keep deterministic.
    for key in _LIST_OVERLAY_KEYS:
        value = overlay.get(key)
        if isinstance(value, list) and value:
            merged[key] = [str(item) for item in value if str(item).strip()]
    for key in _STRUCTURED_OVERLAY_KEYS:
        value = overlay.get(key)
        if isinstance(value, list):
            merged[key] = [dict(item) for item in value if isinstance(item, dict)]

    # Base table: only if it's a real table.
    suggested = overlay.get("suggested_base_table")
    if isinstance(suggested, str) and suggested in tables:
        merged["suggested_base_table"] = suggested

    # Capabilities may be switched on from semantic understanding only with a quote.
    # A model may never switch off a capability already grounded by the deterministic
    # layer, because that would erase an explicit request.
    overlay_caps = overlay.get("capabilities")
    if isinstance(overlay_caps, dict):
        capabilities = dict(merged["capabilities"])
        evidence = overlay.get("capability_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        names = schema_names(tables)
        for key in _CAPABILITY_KEYS:
            value = overlay_caps.get(key)
            if not isinstance(value, bool):
                continue
            if key in _EVIDENCE_CAPABILITIES:
                explicitly_negated = key == "wants_inplace" and inplace_operation_is_negated(
                    goal
                )
                if (
                    value
                    and not explicitly_negated
                    and verify_evidence(evidence.get(key), goal, names=names)
                ):
                    capabilities[key] = True
                continue
            capabilities[key] = value
        merged["capabilities"] = capabilities

    aggregation = _valid_aggregation(overlay.get("aggregation"), tables)
    if aggregation and merged["capabilities"].get("needs_analysis"):
        merged["aggregation"] = aggregation
    elif (
        merged["capabilities"].get("needs_analysis")
        and merged["capabilities"].get("needs_exception_review")
        and not merged["capabilities"].get("needs_charts")
    ):
        # A model sometimes reads "分析样本并标记空值" as two deliverables even though
        # it supplies no aggregate shape. In an exception-only task that unsupported
        # interpretation would manufacture a blocking "按什么统计" question.
        merged["capabilities"]["needs_analysis"] = False

    chart_type = str(overlay.get("chart_type") or "").strip().lower()
    if chart_type in {"line", "bar", "pie"} and merged["capabilities"].get(
        "needs_charts"
    ):
        merged["chart_type"] = chart_type

    # Target fields: keep only real table.field columns.
    target_fields = _valid_target_fields(overlay.get("target_fields"), tables)
    if target_fields:
        merged["target_fields"] = target_fields

    # Clarification questions / assumptions: string lists only.
    questions, non_blocking = _clarification_questions(overlay.get("clarification_questions"))
    merged["clarification_questions"] = questions
    merged["non_blocking_questions"] = non_blocking
    merged["assumptions"] = _string_list(overlay.get("assumptions"))

    return merged


_AGGREGATIONS = frozenset(
    {"count", "sum", "mean", "min", "max", "median", "nunique", "first", "last"}
)


def _valid_aggregation(
    raw: Any,
    tables: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Validate an additive semantic summary against the uploaded schema."""

    if not isinstance(raw, dict):
        return {}
    available = {
        str(column)
        for frame in tables.values()
        for column in frame.columns
    }
    group_by = [
        str(field)
        for field in raw.get("group_by") or []
        if str(field) in available
    ]
    metrics: list[dict[str, Any]] = []
    for item in raw.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        agg = str(item.get("agg") or "")
        column = item.get("column")
        column = str(column) if column is not None else None
        output_name = str(item.get("output_name") or "").strip()
        if (
            agg not in _AGGREGATIONS
            or (agg != "count" and column not in available)
            or (agg == "count" and column is not None and column not in available)
            or not output_name
        ):
            continue
        metrics.append(
            {"column": column, "agg": agg, "output_name": output_name}
        )
    if not metrics:
        return {}
    return {
        "group_by": list(dict.fromkeys(group_by)),
        "metrics": metrics,
        "source": "llm",
    }


def _valid_target_fields(
    raw: Any,
    tables: dict[str, pd.DataFrame],
) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    valid: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table", ""))
        field = str(item.get("field", ""))
        if table in tables and field in {str(col) for col in tables[table].columns}:
            valid.append({"table": table, "field": field})
    return valid


def _clarification_questions(raw: Any) -> tuple[list[str], list[str]]:
    """Split the model's questions into (all, the ones it says can be assumed).

    A question the model marks ``blocking: false`` has a standard answer — 括号是负数,
    合计行不进汇总 — and stopping a one-sentence request to ask it is the opposite of
    what this product promises. A bare string carries no verdict, so it stays blocking:
    when the model has not said the run can proceed, asking is the safe direction.
    """

    if not isinstance(raw, list):
        return [], []
    questions: list[str] = []
    non_blocking: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("question") or "").strip()
            if text and item.get("blocking") is False:
                non_blocking.append(text)
        else:
            text = str(item).strip()
        if text:
            questions.append(text)
    return questions, non_blocking


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]
