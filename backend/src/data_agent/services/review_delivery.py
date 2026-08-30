"""Close the human-review loop by re-materialising a result from decisions.

Review used to be write-only: the console persisted 确认可用 / 确认不采用 per row, but
nothing ever consumed those decisions, so the downloadable workbook still reflected
the machine's original split. This module turns the saved judgements into a real
second deliverable.

The run persists its full pre-split ``business_result`` once (only when the run
actually produced review rows), so re-materialisation is a pure, deterministic
projection: no re-planning, no LLM call, no re-execution. The same OutputSpec that
governed the first workbook still governs this one, so a review pass can never widen
the delivery contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

from data_agent.schemas.output import OutputSpec
from data_agent.tools import export_workbook, lint_output
from data_agent.tools.delivery_view import (
    DELIVERED_STATUS,
    REPORTED_PROBLEM_COLUMN,
    REVIEW_COLUMNS,
    is_technical_column,
    review_rows,
)

# Internal, non-advertised location: this workbook is execution state, not a product.
_SNAPSHOT_DIR = "_internal"
_SNAPSHOT_NAME = "result_snapshot.xlsx"
_SNAPSHOT_SHEET = "business_result"
# Positional key that ties a review item back to its row in the snapshot. Review ids
# ("ROW-12", "ROW-12-2") are not unique per source row, so they cannot be the key.
RESULT_INDEX_COLUMN = "_result_index"
REMATERIALIZED_RESULT_NAME = "final_result_v2.xlsx"
REVIEW_DECISION_COLUMN = "复核结论"

_DELIVERED_STATUS = DELIVERED_STATUS


@dataclass(frozen=True)
class RematerializeResult:
    """What one review-driven re-materialisation produced."""

    path: Path
    accepted_rows: int
    excluded_rows: int
    pending_rows: int
    result_row_count: int


def snapshot_path(work_dir: Union[str, Path]) -> Path:
    return Path(work_dir) / _SNAPSHOT_DIR / _SNAPSHOT_NAME


def snapshot_relative_path() -> Path:
    """Location relative to a job directory, for promoting a reflection winner."""

    return Path(_SNAPSHOT_DIR) / _SNAPSHOT_NAME


def write_result_snapshot(
    work_dir: Union[str, Path],
    business_result: pd.DataFrame,
) -> Optional[Path]:
    """Persist the full pre-split result so decisions can be applied afterwards.

    Skipped when the run has nothing to re-materialise (no review rows), which keeps
    the common clean run free of an extra workbook write.
    """

    path = snapshot_path(work_dir)
    if (
        business_result.empty
        or "处理状态" not in business_result.columns
        or review_rows(business_result).empty
    ):
        # A resumed/confirmed run reuses the job directory, so a snapshot left by an
        # earlier round must not survive a round that has nothing to review.
        path.unlink(missing_ok=True)
        return None
    frame = business_result.copy()
    frame.insert(0, RESULT_INDEX_COLUMN, [int(label) for label in frame.index])
    return export_workbook(path, {_SNAPSHOT_SHEET: frame})


def read_result_snapshot(work_dir: Union[str, Path]) -> Optional[pd.DataFrame]:
    path = snapshot_path(work_dir)
    if not path.exists():
        return None
    frame = pd.read_excel(path, sheet_name=_SNAPSHOT_SHEET)
    if RESULT_INDEX_COLUMN not in frame.columns:
        return None
    return frame


def rematerialize_result(
    job_dir: Union[str, Path],
    *,
    output_spec: OutputSpec | dict[str, Any],
    decisions: dict[str, str],
    review_items: Iterable[dict[str, Any]],
    file_name: str = REMATERIALIZED_RESULT_NAME,
) -> RematerializeResult:
    """Rebuild the deliverable with the user's review judgements applied.

    ``accepted`` rows re-enter the primary result, ``excluded`` rows leave it, and
    everything still pending keeps the machine's original placement. Raises
    ``ValueError`` with a user-facing message when the job cannot be re-materialised.
    """

    root = Path(job_dir)
    spec = (
        output_spec
        if isinstance(output_spec, OutputSpec)
        else OutputSpec.model_validate(output_spec)
    )
    snapshot = read_result_snapshot(root)
    if snapshot is None:
        raise ValueError("该任务没有可用于复核回写的结果快照，请重新提交任务。")

    index_by_row_id = {
        str(item.get("id")): int(item["result_index"])
        for item in review_items
        if isinstance(item, dict)
        and item.get("id")
        and _is_int(item.get("result_index"))
    }
    if not index_by_row_id:
        raise ValueError("该任务的复核清单缺少行定位信息，无法回写结果。")

    accepted = _decided_indexes(decisions, index_by_row_id, "accepted")
    excluded = _decided_indexes(decisions, index_by_row_id, "excluded")
    if not accepted and not excluded:
        raise ValueError("还没有已确认的复核结论，无需重新生成结果。")

    positions = snapshot[RESULT_INDEX_COLUMN]
    was_delivered = snapshot["处理状态"].eq(_DELIVERED_STATUS)
    delivered = (was_delivered | positions.isin(accepted)) & ~positions.isin(excluded)

    primary = snapshot[delivered].drop(columns=[RESULT_INDEX_COLUMN])
    primary = primary.drop(
        columns=[
            column
            for column in primary.columns
            if column in REVIEW_COLUMNS or is_technical_column(str(column))
        ]
    ).reset_index(drop=True)

    sheets: dict[str, pd.DataFrame] = {spec.primary_artifact.name: primary}
    if spec.include_review_view:
        sheets["问题说明"] = _problem_sheet(snapshot, delivered, accepted, excluded)

    allowed_fields = {str(column) for column in snapshot.columns}
    allowed_fields.discard(RESULT_INDEX_COLUMN)
    allowed_fields.add(REVIEW_DECISION_COLUMN)
    lint_output(
        spec,
        sheets={
            sheet_name: [str(column) for column in frame.columns]
            for sheet_name, frame in sheets.items()
        },
        row_counts={sheet_name: len(frame) for sheet_name, frame in sheets.items()},
        allowed_fields=allowed_fields,
    )

    safe_name = Path(file_name).name
    if safe_name != file_name or not safe_name.endswith(".xlsx"):
        raise ValueError("结果版本文件名无效。")
    path = export_workbook(root / "output" / safe_name, sheets)
    was_reported = snapshot.get(
        REPORTED_PROBLEM_COLUMN,
        pd.Series(False, index=snapshot.index),
    ).fillna(False).astype(bool)
    pending = int((was_reported & ~positions.isin(accepted | excluded)).sum())
    return RematerializeResult(
        path=path,
        accepted_rows=len(accepted),
        excluded_rows=len(excluded),
        pending_rows=pending,
        result_row_count=len(primary),
    )


def _problem_sheet(
    snapshot: pd.DataFrame,
    delivered: pd.Series,
    accepted: set[int],
    excluded: set[int],
) -> pd.DataFrame:
    """List every row kept out of the delivery, annotated with its final judgement."""

    positions = snapshot[RESULT_INDEX_COLUMN]
    reported = snapshot.get(
        REPORTED_PROBLEM_COLUMN,
        pd.Series(False, index=snapshot.index),
    ).fillna(False).astype(bool)
    problem_mask = (~delivered) | (
        reported & ~positions.isin(accepted | excluded)
    )
    problems = snapshot[problem_mask].drop(columns=[RESULT_INDEX_COLUMN]).copy()
    problems = problems.drop(
        columns=[
            column
            for column in problems.columns
            if is_technical_column(str(column))
        ]
    )
    verdict = positions[problem_mask].map(
        lambda position: "确认不采用" if position in excluded else "待复核"
    )
    problems[REVIEW_DECISION_COLUMN] = verdict.to_numpy()
    return problems.reset_index(drop=True)


def _decided_indexes(
    decisions: dict[str, str],
    index_by_row_id: dict[str, int],
    status: str,
) -> set[int]:
    return {
        index_by_row_id[row_id]
        for row_id, value in decisions.items()
        if value == status and row_id in index_by_row_id
    }


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True
