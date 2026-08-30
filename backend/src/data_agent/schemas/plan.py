"""Versioned, immutable execution-plan snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from data_agent.schemas.output import OutputSpec
from data_agent.schemas.task import ActionAuthorization

ConfirmationReason = Literal[
    "missing_authorization",
    "impact_unknown",
    "high_removal_ratio",
    "fuzzy_matching",
]


class StepImpactEstimate(BaseModel):
    """Frozen evidence used by the confirmation policy for one plan step."""

    estimate_available: bool = False
    input_rows: int = Field(default=0, ge=0)
    estimated_removed_rows: int | None = Field(default=None, ge=0)
    removal_ratio: float | None = Field(default=None, ge=0, le=1)
    high_removal_ratio_threshold: float = Field(default=0.25, ge=0, le=1)


class PlannerMetadata(BaseModel):
    goal_prompt_version: str = Field(min_length=1)
    planner_prompt_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    capability_registry_hash: str = Field(min_length=64, max_length=64)


class InputFingerprint(BaseModel):
    """Content identity for one data or supporting-document input."""

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExecutionStep(BaseModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    requires_confirmation: bool = False
    confirmation_reasons: list[ConfirmationReason] = Field(default_factory=list)
    impact_estimate: StepImpactEstimate | None = None
    # Authorization and impact are separate evidence. A quoted instruction authorizes
    # the action; it does not waive confirmation when the estimated blast radius is
    # high or unknowable. Defaults to "implied" for infrastructure steps.
    authorization: ActionAuthorization = "implied"
    acceptance_rules: list[str] = Field(min_length=1)
    failure_policy: Literal["fail", "warn"] = "fail"


class ExecutionPlan(BaseModel):
    # v2 makes runtime-stage ordering part of the contract. v3 additionally seals
    # the compatibility JobConfig payload, so it cannot become a second business
    # truth beside this plan.
    version: Literal[1, 2, 3] = 3
    config_hash: str = ""
    steps: list[ExecutionStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dag(self) -> ExecutionPlan:
        if self.version == 3 and len(self.config_hash) != 64:
            raise ValueError("ExecutionPlan v3 requires a 64-character config_hash")
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate execution step: {step.step_id}")
            missing = set(step.depends_on) - seen
            if missing:
                raise ValueError(
                    f"{step.step_id} depends on missing or later steps: {sorted(missing)}"
                )
            seen.add(step.step_id)
        return self

    @property
    def requires_confirmation(self) -> bool:
        return any(step.requires_confirmation for step in self.steps)


class PlanSnapshot(BaseModel):
    """Serializable anchor for confirmation, retry and recovery.

    ``job_config`` is the exact validated configuration the user reviewed. Runtime
    code may retarget only its output path to the same job directory; semantic
    planning is never repeated after confirmation.
    """

    # v3-v6 remain readable so paused jobs can finish. v7 binds the compiler version
    # into the confirmation envelope, preventing a new compiler from executing an
    # older plan under changed semantics.
    version: Literal[3, 4, 5, 6, 7] = 7
    plan_id: str = ""
    plan_hash: str = Field(min_length=64, max_length=64)
    task_spec_hash: str = ""
    schema_fingerprint: str = Field(min_length=64, max_length=64)
    input_fingerprints: list[InputFingerprint] = Field(default_factory=list)
    compiler_version: str = ""
    input_paths: list[Path] = Field(min_length=1)
    processing_options: dict[str, Any]
    job_config: dict[str, Any]
    execution_plan: ExecutionPlan
    output_spec: OutputSpec
    planner_metadata: PlannerMetadata
    llm_usage: list[dict[str, Any]] = Field(default_factory=list)
    goal_plan: dict[str, Any] = Field(default_factory=dict)
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_confirmation_envelope(self) -> PlanSnapshot:
        if self.version in {4, 5, 6, 7}:
            if not self.plan_id or len(self.plan_id) != 32:
                raise ValueError("version 4+ snapshot requires a 32-character plan_id")
            if not self.task_spec_hash or len(self.task_spec_hash) != 64:
                raise ValueError("version 4+ snapshot requires a task_spec_hash")
            if not self.input_fingerprints:
                raise ValueError("version 4+ snapshot requires input fingerprints")
        if self.version == 7 and not self.compiler_version:
            raise ValueError("version 7 snapshot requires a compiler_version")
        return self
