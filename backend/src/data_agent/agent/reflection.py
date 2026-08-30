"""Post-execution self-check and bounded reflection for the delivery flow.

Phase two of the agent upgrade. After the deterministic engine runs a job once,
we read the quality signals it produced (overall score, deduction items, lookup
match rates, join-expansion risk). If the result is below the target and an LLM is
configured, we let the model look at those signals and propose a small, targeted
JobConfig change. The proposal is untrusted: it passes the same whitelist + schema
+ profile-field validation as the planning draft before it can be re-executed.

The reflection layer receives only plan and quality signals and never executes
transformations or re-runs the job itself; it only proposes an override dict. The
caller (business delivery) owns the run-observe-revise-rerun loop and always keeps
the better-scoring result, so reflection can only help, never degrade.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import pandas as pd

from data_agent.agent.llm_client import build_default_llm_planner
from data_agent.agent.planner import LLMPlannerCallable, validate_reflection_overrides
from data_agent.agent.prompts import REFLECTION_SYSTEM_PROMPT
from data_agent.schemas.job import REFLECTION_OVERRIDE_KEYS
from data_agent.utils.collections import deep_merge, frame_records

logger = logging.getLogger(__name__)

# Re-exported for backward compatibility; the canonical definition lives in
# schemas.job so the processing overrides gate and this layer never drift apart.
__all__ = [
    "REFLECTION_OVERRIDE_KEYS",
    "ReflectionSettings",
    "ScoredRun",
    "load_reflection_settings",
    "propose_reflection_overrides",
    "build_reflection_messages",
    "run_with_bounded_reflection",
]

DEFAULT_MAX_ROUNDS = 1
# Attempt work directories are scratch, so they stay out of a delivery folder's
# top level (they hold a second, losing copy of every artifact).
ATTEMPT_DIR_PREFIX = ".data-agent-attempt"
DEFAULT_TARGET_SCORE = 90


@dataclass(frozen=True)
class ReflectionSettings:
    """Env-driven controls for the bounded post-execution reflection loop."""

    enabled: bool
    max_rounds: int
    target_score: int


class ScoredRun(Protocol):
    """The minimal contract the bounded reflection loop needs from any run.

    Both the CLI delivery path (``_RunBundle``) and the API path implement this,
    so the keep-best loop can be shared verbatim instead of each path carrying its
    own copy that could drift in behaviour.
    """

    @property
    def score(self) -> int:
        ...

    @property
    def job_config(self) -> dict[str, Any]:
        ...

    @property
    def quality_summary(self) -> pd.DataFrame:
        ...

    @property
    def quality_deductions(self) -> pd.DataFrame:
        ...

    # Optional verifiable signals used only as tie-breakers when scores are equal.
    # Read via getattr with defaults, so implementers may omit them.
    @property
    def match_rate_value(self) -> float:
        ...

    @property
    def missing_field_count(self) -> int:
        ...


def load_reflection_settings() -> ReflectionSettings:
    """Resolve reflection controls from environment variables.

    - ``DATA_AGENT_REFLECTION_ENABLED`` (default on; a no-op without an LLM)
    - ``DATA_AGENT_REFLECTION_MAX_ROUNDS`` (default 1, bounded)
    - ``DATA_AGENT_REFLECTION_TARGET_SCORE`` (default 90; reflection only fires
      when the first run scores below this)
    """

    enabled = _env_flag("DATA_AGENT_REFLECTION_ENABLED", default=True)
    max_rounds = max(0, _env_int("DATA_AGENT_REFLECTION_MAX_ROUNDS", DEFAULT_MAX_ROUNDS))
    target_score = _env_int("DATA_AGENT_REFLECTION_TARGET_SCORE", DEFAULT_TARGET_SCORE)
    return ReflectionSettings(enabled=enabled, max_rounds=max_rounds, target_score=target_score)


def propose_reflection_overrides(
    job_config: dict[str, Any],
    goal: str,
    quality_summary: pd.DataFrame,
    quality_deductions: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    *,
    match_rate: pd.DataFrame | None = None,
    llm_planner: LLMPlannerCallable | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask the LLM for a validated JobConfig override based on run quality signals.

    Returns ``(overrides, attempt)`` where ``overrides`` is the accepted, validated
    partial JobConfig (empty dict when there is nothing safe to change) and
    ``attempt`` is a record describing what happened for auditing. When no LLM is
    configured/available, returns ``({}, attempt)`` so the caller keeps the current
    result unchanged.
    """

    if llm_planner is None:
        llm_planner = build_default_llm_planner()
    if llm_planner is None:
        return {}, _attempt("skipped", "no LLM configured; kept deterministic result")

    messages = build_reflection_messages(
        job_config=job_config,
        goal=goal,
        quality_summary=quality_summary,
        quality_deductions=quality_deductions,
        match_rate=match_rate,
    )
    try:
        raw_response = llm_planner(messages)
    except Exception as exc:  # noqa: BLE001 - any LLM/transport failure is non-fatal
        logger.warning("Reflection LLM call failed, keeping current result: %s", exc)
        return {}, _attempt("unavailable", f"LLM 调用失败，保留当前结果: {exc}")

    overrides, diagnosis, parse_error = _parse_reflection_payload(raw_response)
    if parse_error:
        return {}, _attempt("rejected", parse_error)
    if not overrides:
        return {}, _attempt("no_change", diagnosis or "LLM 认为无需调整")

    _merged, validation_error = validate_reflection_overrides(job_config, overrides, tables)
    if validation_error:
        return {}, _attempt("rejected", f"{diagnosis or ''} | 校验失败: {validation_error}".strip())

    return overrides, _attempt("accepted", diagnosis or "LLM 提出结构化调整", overrides=overrides)


def run_with_bounded_reflection(
    execute_and_score: Callable[[dict[str, Any], Path], ScoredRun],
    *,
    base_config: dict[str, Any],
    output_path: Path,
    validation_tables: dict[str, pd.DataFrame],
    goal: str,
    relationship_candidates: Optional[pd.DataFrame] = None,
    settings: Optional[ReflectionSettings] = None,
    llm_planner: Optional[LLMPlannerCallable] = None,
) -> tuple[ScoredRun, list[dict[str, Any]]]:
    """Run a scored job once, then optionally reflect-and-rerun, keeping the best.

    This is the single implementation of the run-observe-revise-rerun loop shared
    by the CLI delivery path and the API path, so both get identical agent
    behaviour. ``execute_and_score`` is the only thing that differs between callers:
    it takes ``(run_config, work_dir)`` and returns any object implementing
    :class:`ScoredRun`.

    The loop is deliberately bounded and conservative:
    - The first run always uses the deterministic/planned config (unchanged behaviour).
    - Reflection only fires when an LLM is configured, the run scores below the
      target, and there are rounds left.
    - Each round's proposal is validated (whitelist + schema + field existence) and
      merged into the full structural config before re-running in its own work dir.
    - The higher-scoring run always wins, so reflection can never degrade output.
    """

    settings = settings or load_reflection_settings()
    if llm_planner is None:
        llm_planner = build_default_llm_planner()

    best = execute_and_score(base_config, output_path / ATTEMPT_DIR_PREFIX)
    attempts: list[dict[str, Any]] = []

    reflection_possible = settings.enabled and settings.max_rounds > 0 and llm_planner is not None
    if not reflection_possible:
        return best, attempts

    match_rate = _match_rate_frame(relationship_candidates)
    current_config = base_config
    for round_no in range(1, settings.max_rounds + 1):
        if best.score >= settings.target_score:
            attempts.append(
                {
                    "round": round_no,
                    "source": "reflection",
                    "status": "skipped",
                    "message": f"评分 {best.score} 已达目标 {settings.target_score}，无需反思",
                    "score_before": best.score,
                    "score_after": best.score,
                }
            )
            break

        overrides, attempt = propose_reflection_overrides(
            job_config=best.job_config,
            goal=goal,
            quality_summary=best.quality_summary,
            quality_deductions=best.quality_deductions,
            tables=validation_tables,
            match_rate=match_rate,
            llm_planner=llm_planner,
        )
        attempt = {"round": round_no, "score_before": best.score, **attempt}
        if not overrides:
            attempt.setdefault("score_after", best.score)
            attempts.append(attempt)
            break

        # Apply the validated proposal onto the full structural config and re-run.
        merged, error = _apply_structural_overrides(best.job_config, overrides, validation_tables)
        if error:
            attempt["status"] = "rejected"
            attempt["message"] = f"{attempt.get('message', '')} | 合并失败: {error}".strip(" |")
            attempt["score_after"] = best.score
            attempts.append(attempt)
            break

        round_config = deep_merge(current_config, {"job_overrides": merged})
        try:
            candidate = execute_and_score(
                round_config,
                output_path / f"{ATTEMPT_DIR_PREFIX}_reflection_{round_no}",
            )
        except ValueError as exc:
            attempt["status"] = "rejected"
            attempt["message"] = (
                f"{attempt.get('message', '')} | 候选计划不符合任务契约: {exc}"
            ).strip(" |")
            attempt["score_after"] = best.score
            attempts.append(attempt)
            break
        attempt["score_after"] = candidate.score
        attempt["match_rate_after"] = _run_match_rate(candidate)
        attempt["missing_after"] = _run_missing_fields(candidate)
        if _candidate_is_better(candidate, best):
            attempt["status"] = "accepted"
            attempt["message"] = (
                f"{attempt.get('message', '')} | 评分 {best.score} -> {candidate.score}"
            ).strip(" |")
            best = candidate
            current_config = round_config
        else:
            attempt["status"] = "reverted"
            attempt["message"] = (
                f"{attempt.get('message', '')} | 评分未提升（{best.score} -> "
                f"{candidate.score}），保留原结果"
            ).strip(" |")
        attempts.append(attempt)
        if attempt["status"] != "accepted":
            break

    return best, attempts


def _candidate_is_better(candidate: ScoredRun, best: ScoredRun) -> bool:
    """Keep-best comparator. Score is the primary, non-negotiable judge.

    A run only wins on a higher quality score, so reflection can never lower the
    delivered score (the core "only help, never degrade" invariant). Only when the
    scores tie do the verifiable signals break the tie: prefer a higher lookup match
    rate, then fewer missing template fields.
    """

    if candidate.score != best.score:
        return candidate.score > best.score
    candidate_match = _run_match_rate(candidate)
    best_match = _run_match_rate(best)
    if candidate_match != best_match:
        return candidate_match > best_match
    return _run_missing_fields(candidate) < _run_missing_fields(best)


def _run_match_rate(run: ScoredRun) -> float:
    try:
        return float(getattr(run, "match_rate_value", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _run_missing_fields(run: ScoredRun) -> int:
    try:
        return int(getattr(run, "missing_field_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _apply_structural_overrides(
    job_config: dict[str, Any],
    overrides: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> tuple[dict[str, Any], str]:
    """Validate reflection overrides and return only the structural keys to re-run.

    The proposal is validated against the full merged config (schema + field
    existence). We then hand back only the whitelisted structural keys, because the
    processing layer applies job_overrides restricted to those keys.
    """

    merged, error = validate_reflection_overrides(job_config, overrides, tables)
    if error:
        return {}, error
    allowed = (*REFLECTION_OVERRIDE_KEYS, "quality_score", "business_views")
    structural = {key: merged[key] for key in allowed if key in merged}
    return structural, ""


def _match_rate_frame(relationship_candidates: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Project the profiler relationship candidates into a compact match-rate view."""

    if relationship_candidates is None or relationship_candidates.empty:
        return pd.DataFrame()
    columns = [
        column
        for column in (
            "left_table",
            "left_field",
            "right_table",
            "right_field",
            "left_match_rate",
            "unmatched_left_count",
            "join_will_expand",
            "recommendation",
        )
        if column in relationship_candidates.columns
    ]
    if not columns:
        return pd.DataFrame()
    return relationship_candidates[columns].copy()


def build_reflection_messages(
    job_config: dict[str, Any],
    goal: str,
    quality_summary: pd.DataFrame,
    quality_deductions: pd.DataFrame,
    match_rate: pd.DataFrame | None = None,
) -> list[dict[str, str]]:
    """Build a provider-agnostic reflection prompt from the executed run's signals."""

    context = {
        "goal": goal,
        "executed_job_config": _plan_view(job_config),
        "quality_metrics": frame_records(quality_summary),
        "quality_deductions": frame_records(quality_deductions),
        "weakest_axis": _weakest_axis(quality_deductions),
        "lookup_match_rate": frame_records(match_rate),
        "contract": {
            "response_format": {
                "diagnosis": "one sentence naming the biggest quality problem",
                "overrides": "only the JobConfig keys you are changing, or {}",
                "expected_effect": "one sentence on why this should improve the result",
            },
            "rules": [
                "Fix the weakest_axis first: it is the single biggest score deduction.",
                "Return only the JobConfig keys you are changing, not the full config.",
                "Use only tables and fields already present in the executed config.",
                "Prefer one or two targeted changes; never rewrite the whole plan.",
                "Return empty overrides when nothing can safely improve the result.",
            ],
        },
    }
    return [
        {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Review this executed run and propose at most one or two targeted "
                "JobConfig overrides to improve accuracy or remove redundancy. "
                "Return strict JSON only:\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"
            ),
        },
    ]


def _plan_view(job_config: dict[str, Any]) -> dict[str, Any]:
    """Expose only the structural plan keys the reflection layer may adjust."""

    keys = ("base_table", *REFLECTION_OVERRIDE_KEYS, "quality_score", "business_views")
    return {key: job_config[key] for key in keys if key in job_config}


def _weakest_axis(quality_deductions: pd.DataFrame) -> dict[str, Any]:
    """Return the single highest-scoring deduction so reflection can target it.

    Turns the scalar quality score into a directed objective: instead of "make the
    score go up", the model is told which axis (异常率 / 严重业务问题 / 脏数据 / Join
    膨胀风险 …) is costing the most points and to fix that first.
    """

    if quality_deductions is None or quality_deductions.empty:
        return {}
    if "扣分" not in quality_deductions.columns:
        return {}
    scored = quality_deductions.copy()
    scored["_points"] = pd.to_numeric(scored["扣分"], errors="coerce").fillna(0)
    scored = scored[scored["_points"] > 0]
    if scored.empty:
        return {}
    top = scored.sort_values("_points", ascending=False).iloc[0]
    return {
        "axis": str(top.get("扣分项", "")),
        "points": int(top["_points"]),
        "reason": str(top.get("原因", "")),
    }


def _parse_reflection_payload(payload: str) -> tuple[dict[str, Any], str, str]:
    text = str(payload).strip()
    if not text:
        return {}, "", "reflection response is empty"
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, "", f"reflection response is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "", "reflection response must be a JSON object"
    overrides = parsed.get("overrides", {})
    diagnosis = str(parsed.get("diagnosis", "")).strip()
    if not isinstance(overrides, dict):
        return {}, diagnosis, "reflection overrides must be a JSON object"
    return overrides, diagnosis, ""


def _attempt(status: str, message: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "source": "reflection",
        "status": status,
        "message": message,
        "overrides": overrides or {},
    }


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %s", name, raw, default)
        return default
