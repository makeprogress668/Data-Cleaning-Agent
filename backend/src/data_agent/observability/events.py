"""Real per-stage execution telemetry.

Events used to be *projected* after the fact: every plan step was stamped
``succeeded`` with a row count guessed from the capability id, and no timing at all.
That reads like telemetry but reports nothing that was observed — a stage that did
nothing still showed up as a successful step processing N rows.

The deterministic executor now records each stage as it runs (real duration, real row
counts in and out, real failures), and :func:`bind_execution_events` attaches those
records to the plan steps they correspond to. A plan step with no recorded stage is
reported as ``skipped`` with empty counters rather than an invented success.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from data_agent.schemas.plan import ExecutionPlan

EventStatus = Literal["started", "succeeded", "skipped", "failed"]


class ExecutionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    step_id: str
    capability_id: str
    capability_version: str
    status: EventStatus
    input_rows: Optional[int] = Field(default=None, ge=0)
    output_rows: Optional[int] = Field(default=None, ge=0)
    duration_ms: Optional[float] = Field(default=None, ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class StageRecord:
    """One observed execution stage, independent of any plan numbering."""

    capability_id: str
    status: EventStatus = "succeeded"
    input_rows: Optional[int] = None
    output_rows: Optional[int] = None
    duration_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None


class ExecutionRecorder:
    """Collects observed stage telemetry while the executor runs.

    Deliberately dumb: it knows nothing about plan step ids, so the executor can
    record what it actually did without having to mirror the plan's numbering.
    """

    def __init__(self, execution_plan: ExecutionPlan | None = None) -> None:
        self._stages: list[StageRecord] = []
        self._execution_plan = execution_plan
        self._plan_cursor = 0

    @property
    def stages(self) -> list[StageRecord]:
        return list(self._stages)

    @contextmanager
    def stage(
        self,
        capability_id: str,
        *,
        input_rows: Optional[int] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> Iterator[StageRecord]:
        """Time one stage and record its real outcome, including failures."""

        capability = self._validate_stage(capability_id, input_rows=input_rows)
        record = StageRecord(
            capability_id=capability_id,
            input_rows=input_rows,
            output_rows=input_rows,
            metrics=dict(metrics or {}),
        )
        started = perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "failed"
            record.error_code = type(exc).__name__
            record.duration_ms = round((perf_counter() - started) * 1000, 3)
            self._stages.append(record)
            raise
        record.duration_ms = round((perf_counter() - started) * 1000, 3)
        self._validate_observation(capability, record)
        self._stages.append(record)

    def record(self, stage: StageRecord) -> None:
        """Append a stage observed outside a ``with`` block (e.g. a later phase)."""

        capability = self._validate_stage(
            stage.capability_id,
            input_rows=stage.input_rows,
        )
        self._validate_observation(capability, stage)
        self._stages.append(stage)

    def _validate_stage(
        self,
        capability_id: str,
        *,
        input_rows: int | None,
    ):
        """Gate a v2 stage before its implementation is allowed to run."""

        if self._execution_plan is None or self._execution_plan.version < 2:
            return None
        from data_agent.capabilities.builtins import get_capability_registry

        capability = get_capability_registry().resolve(capability_id)
        _enforce_row_budget(capability_id, capability.max_rows, input_rows)
        self._plan_cursor = _advance_plan_cursor(
            self._execution_plan,
            capability_id,
            self._plan_cursor,
        )
        return capability

    @staticmethod
    def _validate_observation(capability, record: StageRecord) -> None:  # noqa: ANN001
        if capability is None or record.status != "succeeded":
            return
        _enforce_row_budget(
            capability.capability_id,
            capability.max_rows,
            record.output_rows,
        )
        engine = str(
            record.metrics.get("engine") or capability.supported_engines[0]
        )
        if engine not in capability.supported_engines:
            raise ValueError(
                f"{capability.capability_id} used undeclared engine: {engine}"
            )
        record.metrics["engine"] = engine
        if (
            "row count is unchanged" in capability.acceptance_rules
            and record.input_rows is not None
            and record.output_rows is not None
            and record.input_rows != record.output_rows
        ):
            raise ValueError(
                f"{capability.capability_id} violated acceptance rule: row count is unchanged"
            )
        record.metrics["acceptance_status"] = "passed"
        record.metrics["acceptance_rules"] = list(capability.acceptance_rules)


def _enforce_row_budget(
    capability_id: str,
    max_rows: int | None,
    observed_rows: int | None,
) -> None:
    if max_rows is None or observed_rows is None or observed_rows <= max_rows:
        return
    raise ValueError(
        f"{capability_id} exceeded row budget: {observed_rows} > {max_rows}"
    )


def _advance_plan_cursor(
    execution_plan: ExecutionPlan,
    capability_id: str,
    plan_cursor: int,
) -> int:
    """Validate one observed stage and return the next allowed plan position."""

    from data_agent.capabilities.builtins import get_capability_registry

    if capability_id not in get_capability_registry():
        raise ValueError(
            "执行器记录了能力注册表之外的阶段："
            f"{capability_id}"
        )
    match_index = next(
        (
            index
            for index in range(plan_cursor, len(execution_plan.steps))
            if execution_plan.steps[index].capability_id == capability_id
        ),
        None,
    )
    if match_index is None:
        raise ValueError(
            "执行阶段未在计划中声明，或执行顺序与计划不一致："
            f"{capability_id}"
        )
    return match_index + 1


def bind_execution_events(
    execution_plan: ExecutionPlan,
    stages: list[StageRecord],
) -> list[ExecutionEvent]:
    """Attach recorded stages to the plan steps they correspond to, in order.

    Matching is by capability id, scanning forward, because the executor runs the
    plan's capabilities in plan order. Anything the executor did not run is reported
    as ``skipped`` — never as a success with fabricated counters.
    """

    if execution_plan.version >= 2:
        # New plans promise that the registry is the complete runtime allowlist and
        # that step order is executable order. Validate before projecting events so
        # an extra or reordered stage cannot disappear from telemetry unnoticed.
        plan_cursor = 0
        for record in stages:
            plan_cursor = _advance_plan_cursor(
                execution_plan,
                record.capability_id,
                plan_cursor,
            )

    remaining = list(stages)
    events: list[ExecutionEvent] = []
    for step in execution_plan.steps:
        match_index = next(
            (
                index
                for index, record in enumerate(remaining)
                if record.capability_id == step.capability_id
            ),
            None,
        )
        if match_index is None:
            events.append(
                ExecutionEvent(
                    step_id=step.step_id,
                    capability_id=step.capability_id,
                    capability_version=step.capability_version,
                    status="skipped",
                    metrics={
                        "risk_level": step.risk_level,
                        "requires_confirmation": step.requires_confirmation,
                        "reason": "no execution stage recorded for this step",
                    },
                )
            )
            continue
        record = remaining.pop(match_index)
        events.append(
            ExecutionEvent(
                step_id=step.step_id,
                capability_id=step.capability_id,
                capability_version=step.capability_version,
                status=record.status,
                input_rows=record.input_rows,
                output_rows=record.output_rows,
                duration_ms=record.duration_ms,
                error_code=record.error_code,
                metrics={
                    "risk_level": step.risk_level,
                    "requires_confirmation": step.requires_confirmation,
                    **record.metrics,
                },
            )
        )
    return events
