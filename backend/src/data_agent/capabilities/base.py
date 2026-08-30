"""Capability metadata for deterministic data operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]
FailurePolicy = Literal["fail", "warn"]
FieldEffect = Literal["none", "add", "overwrite", "delete"]


@dataclass(frozen=True)
class CapabilitySpec:
    """A registered deterministic operation available to planning."""

    capability_id: str
    version: str
    input_schema: Mapping[str, str]
    output_schema: Mapping[str, str]
    parameter_schema: Mapping[str, Any]
    required_parameters: tuple[str, ...]
    supported_engines: tuple[str, ...]
    risk_level: RiskLevel
    requires_confirmation: bool
    field_effects: tuple[FieldEffect, ...]
    executor: Callable[..., Any] = field(repr=False, compare=False)
    # Formula operations handled by this capability. The registry owns this binding;
    # Planner and Runner must never maintain parallel operator maps again.
    formula_operators: tuple[str, ...] = ()
    formula_fallback: bool = False
    acceptance_rules: tuple[str, ...] = ()
    failure_policy: FailurePolicy = "fail"
    max_rows: int | None = None

    def __post_init__(self) -> None:
        if not self.capability_id or "." in self.capability_id:
            raise ValueError("capability_id must be a non-empty stable identifier")
        if not self.version:
            raise ValueError("capability version is required")
        if not callable(self.executor):
            raise TypeError(f"{self.capability_id} executor must be callable")
        unknown_required = set(self.required_parameters) - set(self.parameter_schema)
        if unknown_required:
            raise ValueError(
                f"{self.capability_id} has undeclared required parameters: "
                f"{sorted(unknown_required)}"
            )
        if not self.supported_engines:
            raise ValueError(f"{self.capability_id} must declare an execution engine")
        if not self.acceptance_rules:
            raise ValueError(f"{self.capability_id} must declare acceptance rules")
        if len(self.formula_operators) != len(set(self.formula_operators)):
            raise ValueError(
                f"{self.capability_id} declares duplicate formula operators"
            )

    def describe(self) -> dict[str, Any]:
        """Return serializable metadata safe to expose to a planner."""

        return {
            "capability_id": self.capability_id,
            "version": self.version,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "parameter_schema": dict(self.parameter_schema),
            "required_parameters": list(self.required_parameters),
            "supported_engines": list(self.supported_engines),
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "field_effects": list(self.field_effects),
            "formula_operators": list(self.formula_operators),
            "formula_fallback": self.formula_fallback,
            "acceptance_rules": list(self.acceptance_rules),
            "failure_policy": self.failure_policy,
            "max_rows": self.max_rows,
        }

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        """Validate the concrete step payload against registry-owned metadata."""

        unknown = set(parameters) - set(self.parameter_schema)
        if unknown:
            raise ValueError(
                f"{self.capability_id} has undeclared parameters: {sorted(unknown)}"
            )
        missing = set(self.required_parameters) - set(parameters)
        if missing:
            raise ValueError(
                f"{self.capability_id} missing required parameters: {sorted(missing)}"
            )
        for name, value in parameters.items():
            expected = str(self.parameter_schema[name])
            if not _matches_schema(value, expected):
                raise ValueError(
                    f"{self.capability_id}.{name} must match {expected}"
                )


def _matches_schema(value: Any, expected: str) -> bool:
    if expected.startswith("optional "):
        return value is None or _matches_schema(value, expected.removeprefix("optional "))
    if expected.startswith("list["):
        return isinstance(value, list)
    if expected.startswith("dict["):
        return isinstance(value, dict)
    if expected in {"string", "path", "FormulaOperator", "LookupMatchMode", "PivotAggregation"}:
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected in {"any", "ConditionConfig"}:
        return True
    # Named structured contracts are represented by JSON objects at plan boundaries.
    return isinstance(value, dict)
