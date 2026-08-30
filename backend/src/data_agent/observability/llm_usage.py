"""Task-local LLM usage and latency capture without prompt/data logging."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("data_agent.observability")

_usage_buffer: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "data_agent_llm_usage",
    default=None,
)


@contextmanager
def capture_llm_usage() -> Iterator[list[dict[str, Any]]]:
    existing = _usage_buffer.get()
    if existing is not None:
        yield existing
        return
    records: list[dict[str, Any]] = []
    token = _usage_buffer.set(records)
    try:
        yield records
    finally:
        _usage_buffer.reset(token)


def current_llm_usage() -> list[dict[str, Any]]:
    records = _usage_buffer.get()
    return [dict(item) for item in records] if records is not None else []


def record_llm_usage(
    *,
    operation: str,
    model: str,
    latency_ms: float,
    attempts: int,
    status: str,
    usage: Optional[dict[str, Any]] = None,
    error_type: Optional[str] = None,
) -> dict[str, Any]:
    token_usage = usage or {}
    prompt_tokens = _integer(
        token_usage.get("prompt_tokens", token_usage.get("input_tokens"))
    )
    completion_tokens = _integer(
        token_usage.get("completion_tokens", token_usage.get("output_tokens"))
    )
    total_tokens = _integer(token_usage.get("total_tokens"))
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    record = {
        "operation": operation,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": round(latency_ms, 2),
        "attempts": attempts,
        "status": status,
        "error_type": error_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    records = _usage_buffer.get()
    if records is not None:
        records.append(record)
    logger.info("llm_usage %s", json.dumps(record, ensure_ascii=False))
    return record


def infer_llm_operation(messages: list[dict[str, str]]) -> str:
    system = str(messages[0].get("content", "") if messages else "").lower()
    if "goal-understanding" in system:
        return "goal_understanding"
    if "reflection" in system:
        return "reflection"
    return "planning"


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
