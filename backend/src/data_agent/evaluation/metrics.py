"""Aggregate deterministic metrics for golden-task replay."""

from __future__ import annotations

from statistics import mean
from typing import Any


def aggregate_replay_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {
            "task_count": 0,
            "evaluated_count": 0,
            "skipped_count": 0,
            "passed_count": 0,
            "pass_rate": 0.0,
            "intent_coverage": 0.0,
            "capability_coverage": 0.0,
            "output_compliance": 0.0,
            "result_semantics": 0.0,
            "average_duration_ms": 0.0,
            "p95_duration_ms": 0.0,
            "scenario_count": 0,
            "tag_coverage": [],
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "llm_latency_ms": 0.0,
        }
    evaluated = [item for item in results if not item.get("skipped")]
    if not evaluated:
        return {
            "task_count": len(results),
            "evaluated_count": 0,
            "skipped_count": len(results),
            "passed_count": 0,
            "pass_rate": 0.0,
            "intent_coverage": 0.0,
            "capability_coverage": 0.0,
            "output_compliance": 0.0,
            "result_semantics": 0.0,
            "average_duration_ms": 0.0,
            "p95_duration_ms": 0.0,
            "scenario_count": len(
                {str(item.get("scenario_id") or item.get("task_id")) for item in results}
            ),
            "tag_coverage": sorted(
                {str(tag) for item in results for tag in item.get("tags", [])}
            ),
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "llm_latency_ms": 0.0,
        }
    return {
        "task_count": len(results),
        "evaluated_count": len(evaluated),
        "skipped_count": len(results) - len(evaluated),
        "passed_count": sum(bool(item.get("passed")) for item in evaluated),
        "pass_rate": _rate(
            sum(bool(item.get("passed")) for item in evaluated),
            len(evaluated),
        ),
        "intent_coverage": mean(
            float(item.get("metrics", {}).get("intent_coverage", 0.0))
            for item in evaluated
        ),
        "capability_coverage": mean(
            float(item.get("metrics", {}).get("capability_coverage", 0.0))
            for item in evaluated
        ),
        "output_compliance": mean(
            float(item.get("metrics", {}).get("output_compliance", 0.0))
            for item in evaluated
        ),
        "result_semantics": mean(
            float(item.get("metrics", {}).get("result_semantics", 0.0))
            for item in evaluated
        ),
        "average_duration_ms": round(
            mean(float(item.get("duration_ms", 0.0)) for item in evaluated),
            2,
        ),
        "p95_duration_ms": _percentile(
            [float(item.get("duration_ms", 0.0)) for item in evaluated],
            0.95,
        ),
        "scenario_count": len(
            {str(item.get("scenario_id") or item.get("task_id")) for item in results}
        ),
        "tag_coverage": sorted(
            {
                str(tag)
                for item in results
                for tag in item.get("tags", [])
            }
        ),
        "llm_calls": sum(len(item.get("llm_usage", [])) for item in evaluated),
        "prompt_tokens": sum(
            int(usage.get("prompt_tokens", 0))
            for item in evaluated
            for usage in item.get("llm_usage", [])
        ),
        "completion_tokens": sum(
            int(usage.get("completion_tokens", 0))
            for item in evaluated
            for usage in item.get("llm_usage", [])
        ),
        "llm_latency_ms": round(
            sum(
                float(usage.get("latency_ms", 0.0))
                for item in evaluated
                for usage in item.get("llm_usage", [])
            ),
            2,
        ),
    }


def coverage(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 1.0
    return _rate(len(expected & actual), len(expected))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 2)
