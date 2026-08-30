"""Deterministic, auditable confirmation policy for execution-plan steps."""

from __future__ import annotations

from typing import Any

from data_agent.schemas.plan import (
    ConfirmationReason,
    StepImpactEstimate,
)
from data_agent.schemas.task import ActionAuthorization

# A quarter of the base table is large enough that a wrong rule is costly, while
# keeping ordinary removal of a few known bad rows on the direct path. This value is
# deliberately code-versioned rather than environment-driven: a frozen plan must not
# change its confirmation result because a worker has different process settings.
HIGH_REMOVAL_RATIO_THRESHOLD = 0.25


def build_step_impact_estimate(
    impact_preview: dict[str, Any] | None,
) -> StepImpactEstimate:
    """Normalize a plan-wide destructive preview into frozen policy evidence."""

    preview = impact_preview or {}
    input_rows = _non_negative_int(preview.get("input_rows")) or 0
    removed_rows = _non_negative_int(preview.get("estimated_removed_rows"))
    estimate_available = bool(preview.get("estimate_available"))
    if not estimate_available or removed_rows is None:
        return StepImpactEstimate(
            estimate_available=False,
            input_rows=input_rows,
        )
    ratio = removed_rows / input_rows if input_rows else 0.0
    return StepImpactEstimate(
        estimate_available=True,
        input_rows=input_rows,
        estimated_removed_rows=removed_rows,
        removal_ratio=ratio,
        high_removal_ratio_threshold=HIGH_REMOVAL_RATIO_THRESHOLD,
    )


def confirmation_reasons(
    *,
    capability_id: str,
    capability_requires_confirmation: bool,
    authorization: ActionAuthorization,
    parameters: dict[str, Any],
    impact_estimate: StepImpactEstimate | None,
) -> list[ConfirmationReason]:
    """Return stable reason codes; a non-empty result means the step must pause."""

    reasons: list[ConfirmationReason] = []
    if capability_requires_confirmation:
        if authorization != "stated":
            reasons.append("missing_authorization")
        if impact_estimate is None or not impact_estimate.estimate_available:
            reasons.append("impact_unknown")
        elif (
            impact_estimate.removal_ratio is not None
            and impact_estimate.removal_ratio
            >= impact_estimate.high_removal_ratio_threshold
        ):
            reasons.append("high_removal_ratio")

    if (
        capability_id in {"lookup_fields", "join_tables"}
        and str(parameters.get("match_mode") or "") == "fuzzy"
    ):
        reasons.append("fuzzy_matching")
    return reasons


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
