"""Strict registry for planner-visible deterministic capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from data_agent.capabilities.base import CapabilitySpec


class UnknownCapabilityError(ValueError):
    """Raised when a plan references an operation outside the registry."""


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilitySpec] = {}
        self._formula_capabilities: dict[str, str] = {}
        self._formula_fallback: str | None = None

    def register(self, capability: CapabilitySpec) -> None:
        if capability.capability_id in self._capabilities:
            raise ValueError(
                f"capability already registered: {capability.capability_id}"
            )
        duplicated_operators = set(capability.formula_operators) & set(
            self._formula_capabilities
        )
        if duplicated_operators:
            raise ValueError(
                "formula operators already registered: "
                f"{sorted(duplicated_operators)}"
            )
        if capability.formula_fallback and self._formula_fallback is not None:
            raise ValueError(
                "formula fallback already registered: "
                f"{self._formula_fallback}"
            )
        self._capabilities[capability.capability_id] = capability
        for operator in capability.formula_operators:
            self._formula_capabilities[operator] = capability.capability_id
        if capability.formula_fallback:
            self._formula_fallback = capability.capability_id

    def register_many(self, capabilities: Iterable[CapabilitySpec]) -> None:
        for capability in capabilities:
            self.register(capability)

    def resolve(self, capability_id: str) -> CapabilitySpec:
        try:
            return self._capabilities[capability_id]
        except KeyError as exc:
            raise UnknownCapabilityError(
                f"unregistered capability: {capability_id}"
            ) from exc

    def resolve_formula_operator(self, operator: str) -> CapabilitySpec:
        """Resolve one FormulaConfig operator through the registry-owned binding."""

        capability_id = (
            self._formula_capabilities.get(operator) or self._formula_fallback
        )
        if capability_id is None:
            raise UnknownCapabilityError(
                f"unregistered formula operator: {operator}"
            )
        return self.resolve(capability_id)

    def descriptors(self) -> list[dict]:
        return [
            self._capabilities[key].describe()
            for key in sorted(self._capabilities)
        ]

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.descriptors(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __contains__(self, capability_id: object) -> bool:
        return capability_id in self._capabilities

    def __len__(self) -> int:
        return len(self._capabilities)
