from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from data_agent.api.artifact_service import (
    has_relative_artifact,
    materialize_named_artifact,
    materialize_relative_artifact,
    publish_artifact,
)
from data_agent.api.job_manager import get_job_manager
from data_agent.api.job_store import (
    STATUS_AWAITING_CONFIRMATION,
    STATUS_NEEDS_CLARIFICATION,
    TERMINAL_STATUSES,
    job_dir,
    list_job_statuses,
    list_result_versions,
    next_result_version_number,
    public_job_payload,
    read_job_status,
    read_plan_snapshot,
    read_review_state,
    read_store_text,
    read_task_session,
    record_rematerialized_result,
    result_version_decisions,
    select_result_version,
    undo_result_version,
    write_clarification_answers,
    write_review_decisions,
)
from data_agent.api.job_store import (
    delete_job as delete_job_dir,
)
from data_agent.services.review_delivery import rematerialize_result
from data_agent.tenancy import request_tenant_id
from data_agent.tools import OutputContractError
from data_agent.tools.quality_score import ACTION_LABELS

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

_ALLOWED_REVIEW_STATUSES = {"pending", "accepted", "excluded"}


class ReviewDecision(BaseModel):
    row_id: str = Field(min_length=1)
    status: str


class ReviewRequest(BaseModel):
    decisions: list[ReviewDecision] = Field(default_factory=list)


class RematerializeRequest(BaseModel):
    parent_version_id: str | None = Field(default=None, pattern=r"^v[1-9][0-9]*$")
    decisions: list[ReviewDecision] = Field(default_factory=list)


class ClarificationAnswer(BaseModel):
    question_id: str = Field(min_length=1)
    answer: str


class ClarificationRequest(BaseModel):
    answers: list[ClarificationAnswer] = Field(min_length=1)


class PlanConfirmationRequest(BaseModel):
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("")
def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return recent real tasks for navigation and task recovery."""

    return {
        "jobs": [
            _build_job_summary(payload)
            for payload in list_job_statuses(
                limit,
                tenant_id=request_tenant_id(request),
            )
        ],
    }


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    return public_job_payload(read_job_status(job_id))


@router.get("/{job_id}/status")
def get_job_status(job_id: str) -> dict:
    payload = read_job_status(job_id)
    try:
        session = read_task_session(job_id)
    except HTTPException:
        session = {}
    return _build_job_summary(payload, session=session)


def _build_job_summary(
    payload: dict,
    *,
    session: dict | None = None,
) -> dict:
    job_id = str(payload.get("job_id") or "")
    if session is None:
        try:
            session = read_task_session(job_id)
        except HTTPException:
            session = {}
    review_state = read_review_state(job_id)
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    discovery = (
        payload.get("discovery")
        if isinstance(payload.get("discovery"), dict)
        else {}
    )
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    return {
        "job_id": payload.get("job_id", job_id),
        "status": payload.get("status", "pending"),
        "mode": payload.get("mode", "answer"),
        "goal": payload.get("goal"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "queued_at": payload.get("queued_at"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "queue_wait_ms": payload.get("queue_wait_ms"),
        "dispatch_attempts": _int_value(payload.get("dispatch_attempts")),
        "sla_seconds": payload.get("sla_seconds"),
        "sla_breached": bool(payload.get("sla_breached", False)),
        "error": payload.get("error", ""),
        "error_message": payload.get("error_message", payload.get("error", "")),
        "failure_stage": payload.get("failure_stage"),
        # Present (non-empty) only when the job paused for clarification, so the
        # console can render a "回答澄清问题" prompt while continuing to poll.
        "clarification_questions": payload.get("clarification_questions", []),
        "session_id": session.get("session_id"),
        "clarification_round": session.get("clarification_round", 0),
        "max_clarification_rounds": session.get("max_clarification_rounds", 3),
        "task_spec_version": session.get("current_task_spec_version", 0),
        "plan_version": session.get("current_plan_version", 0),
        "review_count": _review_item_count(payload),
        "pending_review_count": _pending_review_item_count(
            payload,
            review_state.get("decisions", {}),
        ),
        "counts": {
            "total": _int_value(counts.get("total")),
            "valid": _int_value(counts.get("valid")),
            "abnormal": _int_value(counts.get("abnormal")),
        },
        "quality_score": payload.get("quality_score"),
        "result_row_count": _int_value(payload.get("result_row_count")),
        "input_files": [
            str(file_name)
            for file_name in discovery.get("files", [])
            if file_name
        ],
        "has_discovery": bool(discovery),
        "has_plan": bool(payload.get("plan")),
        "has_result": bool(files.get("excel_path")),
    }


@router.get("/{job_id}/files")
def get_job_files(job_id: str) -> dict:
    payload = read_job_status(job_id)
    files = payload.get("files", {})
    return {"job_id": job_id, "files": files}


@router.get("/{job_id}/business-answer")
def get_business_answer(job_id: str) -> dict:
    payload = read_job_status(job_id)
    status = payload.get("status")
    if status in {"failed", "cancelled"}:
        raise HTTPException(status_code=400, detail=payload.get("error", "任务未成功完成。"))
    if status not in {"succeeded", "completed", "success"}:
        # Still pending/running: tell the console to keep polling rather than 500.
        raise HTTPException(status_code=404, detail="业务结果尚未生成，请稍候。")
    if not payload.get("files", {}).get("excel_path"):
        raise HTTPException(status_code=404, detail="该任务模式不生成业务结果文件。")
    return _build_business_answer(payload, job_id)


@router.get("/{job_id}/discovery")
def get_job_discovery(job_id: str) -> dict:
    """Return the deterministic data-discovery view captured when the job ran."""
    payload = read_job_status(job_id)
    discovery = payload.get("discovery") or {}
    _require_ready(payload, discovery, "数据概览")
    return {
        "job_id": payload.get("job_id", job_id),
        "goal": payload.get("goal", ""),
        "files": discovery.get("files", []),
        "tables": discovery.get("tables", []),
        "keys": discovery.get("keys", []),
        "relationships": discovery.get("relationships", []),
        "issues": discovery.get("issues", []),
    }


@router.get("/{job_id}/plan")
def get_job_plan(job_id: str) -> dict:
    """Return the executed processing plan (base table, lookups, formulas, rules)."""
    payload = read_job_status(job_id)
    plan = payload.get("plan") or {}
    _require_ready(payload, plan, "处理方案")
    snapshot = read_plan_snapshot(job_id)
    impact = payload.get("goal_plan", {}).get("impact_preview", {})
    if not isinstance(impact, dict):
        impact = {}
    input_rows = (
        _int_value(impact.get("input_rows"))
        if "input_rows" in impact
        else _int_value(payload.get("counts", {}).get("total"))
    )
    return {
        "job_id": payload.get("job_id", job_id),
        "goal": payload.get("goal", ""),
        "main_table": plan.get("main_table", ""),
        "lookups": plan.get("lookups", []),
        "formulas": plan.get("formulas", []),
        "anomaly_rules": plan.get("anomaly_rules", []),
        "document_rules": plan.get("document_rules", []),
        "risks": plan.get("risks", []),
        "execution_steps": payload.get("execution_plan", {}).get("steps", []),
        "output_spec": payload.get("output_spec", {}),
        "planner_metadata": snapshot.get("planner_metadata", {}),
        "impact_preview": {
            "base_table": str(impact.get("base_table") or plan.get("main_table") or ""),
            "input_rows": input_rows,
            "estimated_output_rows": impact.get("estimated_output_rows"),
            "estimated_removed_rows": impact.get("estimated_removed_rows"),
            "estimated_removal_ratio": impact.get("estimated_removal_ratio"),
            "estimate_available": bool(impact.get("estimate_available", False)),
            "affected_fields": list(impact.get("affected_fields") or []),
            "added_fields": list(impact.get("added_fields") or []),
        },
    }


@router.get("/{job_id}/events")
def get_job_events(job_id: str) -> dict:
    """Return persisted execution events and aggregate LLM usage for one job."""

    payload = read_job_status(job_id)
    snapshot = read_plan_snapshot(job_id)
    usage = payload.get("llm_usage") or snapshot.get("llm_usage") or []
    return {
        "job_id": payload.get("job_id", job_id),
        "status": payload.get("status"),
        "plan_hash": snapshot.get("plan_hash", ""),
        "planner_metadata": snapshot.get("planner_metadata", {}),
        "execution_duration_ms": payload.get("execution_duration_ms"),
        "execution_events": payload.get("execution_events", []),
        "llm_usage": usage,
        "llm_usage_summary": {
            "call_count": len(usage),
            "prompt_tokens": sum(_int_value(item.get("prompt_tokens")) for item in usage),
            "completion_tokens": sum(
                _int_value(item.get("completion_tokens")) for item in usage
            ),
            "total_tokens": sum(_int_value(item.get("total_tokens")) for item in usage),
            "latency_ms": round(
                sum(float(item.get("latency_ms") or 0) for item in usage),
                2,
            ),
        },
    }


@router.get("/{job_id}/files/{file_name}")
def download_job_file(job_id: str, file_name: str) -> FileResponse:
    root = job_dir(job_id)
    safe_name = Path(file_name).name
    if safe_name != file_name:
        raise HTTPException(status_code=400, detail="Invalid file name")

    candidates = [
        root / "output" / safe_name,
        root / safe_name,
    ]
    payload = read_job_status(job_id)
    stored = materialize_named_artifact(job_id, safe_name, payload=payload)
    if stored is not None and stored.exists() and stored.is_file():
        return FileResponse(
            stored,
            filename=safe_name,
            media_type=_delivery_media_type(safe_name),
        )
    for value in payload.get("files", {}).values():
        if isinstance(value, str) and value:
            path = Path(value)
            if path.name == safe_name:
                candidates.append(path)

    for candidate in candidates:
        resolved = candidate.resolve()
        if _is_inside(resolved, root.resolve()) and resolved.exists() and resolved.is_file():
            return FileResponse(
                resolved,
                filename=safe_name,
                media_type=_delivery_media_type(safe_name),
            )

    raise HTTPException(status_code=404, detail="File not found")


def _delivery_media_type(file_name: str) -> str:
    """Return stable delivery types independent of the host MIME registry."""

    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".html": "text/html",
        ".md": "text/markdown",
        ".json": "application/json",
    }.get(Path(file_name).suffix.lower(), "application/octet-stream")


@router.get("/{job_id}/review")
def get_review(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    """Return real review rows plus persisted human-review decisions."""
    payload = read_job_status(job_id)
    if payload.get("status") not in {"succeeded", "completed", "success"}:
        _require_ready(payload, {}, "复核清单")
    review_state = read_review_state(job_id)
    catalog = _review_catalog_items(payload, job_id)
    page = catalog[offset : offset + limit]
    return {
        **review_state,
        "items": page,
        "total": _review_item_count(payload),
        "pending": _pending_review_item_count(
            payload,
            review_state.get("decisions", {}),
        ),
        "truncated": offset + len(page) < len(catalog),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(catalog),
    }


@router.get("/{job_id}/session")
def get_task_session(job_id: str) -> dict:
    """Return task-scoped turns, TaskSpec version and clarification progress."""

    read_job_status(job_id)
    return read_task_session(job_id)


@router.get("/{job_id}/clarification")
def get_clarification(job_id: str) -> dict:
    """Return the clarification questions a paused job is waiting on."""
    payload = read_job_status(job_id)
    if payload.get("status") != STATUS_NEEDS_CLARIFICATION:
        raise HTTPException(status_code=409, detail="该任务当前不在等待澄清状态。")
    session = read_task_session(job_id)
    return {
        "job_id": payload.get("job_id", job_id),
        "goal": payload.get("goal", ""),
        "status": payload.get("status"),
        "questions": payload.get("clarification_questions", []),
        "clarification_round": session.get("clarification_round", 0),
        "max_clarification_rounds": session.get("max_clarification_rounds", 3),
    }


@router.post("/{job_id}/clarification")
async def submit_clarification(job_id: str, request: ClarificationRequest) -> dict:
    """Persist clarification answers and resume the paused job.

    Only valid while the job is awaiting clarification; the answers are folded into
    the goal and the job re-plans + executes through the same reflection loop.
    """
    payload = read_job_status(job_id)
    if payload.get("status") != STATUS_NEEDS_CLARIFICATION:
        raise HTTPException(status_code=400, detail="该任务当前不在等待澄清状态。")
    questions = {
        str(item.get("id")): item
        for item in payload.get("clarification_questions", [])
        if isinstance(item, dict) and item.get("id")
    }
    answers = {
        answer.question_id: answer.answer.strip()
        for answer in request.answers
    }
    unknown = sorted(set(answers) - set(questions))
    missing = sorted(set(questions) - set(answers))
    blank = sorted(question_id for question_id, answer in answers.items() if not answer)
    invalid_options = sorted(
        question_id
        for question_id, answer in answers.items()
        if isinstance(questions.get(question_id, {}).get("options"), list)
        and questions[question_id]["options"]
        and answer not in questions[question_id]["options"]
    )
    if unknown or missing or blank or invalid_options:
        raise HTTPException(status_code=400, detail="请完整回答当前待确认问题。")
    config = {"job_overrides": {"goal": payload.get("goal", ""), "mode": "answer"}}
    resumed = await get_job_manager().resume(
        job_id,
        config,
        [
            {"question_id": question_id, "answer": answer}
            for question_id, answer in answers.items()
        ],
    )
    if not resumed:
        raise HTTPException(status_code=409, detail="该任务已被恢复，请勿重复提交。")
    write_clarification_answers(job_id, answers)
    return {"job_id": job_id, "status": "running"}


@router.get("/{job_id}/plan-confirmation")
def get_plan_confirmation(job_id: str) -> dict:
    """Return the plan understanding a job is awaiting confirmation on.

    Surfaces the pre-execution goal understanding (source / confidence /
    assumptions / located tables and fields) so the console can show it — and warn
    on a low-confidence or generic-fallback plan — before the user approves.
    """
    payload = read_job_status(job_id)
    if payload.get("status") != STATUS_AWAITING_CONFIRMATION:
        raise HTTPException(status_code=409, detail="该任务当前没有待确认的处理计划。")
    snapshot = read_plan_snapshot(job_id)
    snapshot_goal_plan = snapshot.get("goal_plan", {})
    processing_options = snapshot.get("processing_options", {})
    return {
        "job_id": payload.get("job_id", job_id),
        "goal": processing_options.get("goal", payload.get("goal", "")),
        "status": payload.get("status"),
        "plan_id": snapshot.get("plan_id") or str(snapshot.get("plan_hash", ""))[:32],
        "plan_hash": snapshot.get("plan_hash", ""),
        "goal_understanding": _business_goal_understanding(
            snapshot_goal_plan
        ),
        "planned_actions": _planned_actions(snapshot_goal_plan),
        "execution_plan": snapshot.get("execution_plan", {}),
        "output_spec": snapshot.get("output_spec", {}),
        "planner_metadata": snapshot.get("planner_metadata", {}),
    }


@router.post("/{job_id}/plan-confirmation")
async def submit_plan_confirmation(
    job_id: str,
    request: PlanConfirmationRequest,
) -> dict:
    """Approve a paused plan and resume execution unchanged.

    Only valid while the job is awaiting confirmation. This executes the immutable
    PlanSnapshot the user reviewed without re-planning or post-confirmation reflection.
    """
    payload = read_job_status(job_id)
    if payload.get("status") != STATUS_AWAITING_CONFIRMATION:
        raise HTTPException(status_code=400, detail="该任务当前不在等待确认状态。")
    config = {"job_overrides": {"goal": payload.get("goal", ""), "mode": "answer"}}
    confirmed = await get_job_manager().confirm(
        job_id,
        config,
        plan_id=request.plan_id,
        plan_hash=request.plan_hash,
    )
    if not confirmed:
        raise HTTPException(status_code=409, detail="该计划已确认，请勿重复提交。")
    return {"job_id": job_id, "status": "running"}


@router.post("/{job_id}/review")
def submit_review(job_id: str, request: ReviewRequest) -> dict:
    """Persist human-review decisions ({row_id: status}) so they survive reloads."""
    invalid = [
        decision.status
        for decision in request.decisions
        if decision.status not in _ALLOWED_REVIEW_STATUSES
    ]
    if invalid:
        allowed = ", ".join(sorted(_ALLOWED_REVIEW_STATUSES))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review status: {invalid}. Allowed: {allowed}",
        )
    decisions = {decision.row_id: decision.status for decision in request.decisions}
    payload = read_job_status(job_id)
    valid_ids = {
        str(item.get("id"))
        for item in _review_catalog_items(payload, job_id)
        if isinstance(item, dict) and item.get("id")
    }
    unknown_ids = sorted(set(decisions) - valid_ids)
    if unknown_ids:
        raise HTTPException(status_code=400, detail="复核记录不存在或已更新，请刷新后重试。")
    review_state = write_review_decisions(job_id, decisions)
    catalog = _review_catalog_items(payload, job_id)
    page = catalog[:200]
    return {
        **review_state,
        "items": page,
        "total": _review_item_count(payload),
        "pending": _pending_review_item_count(
            payload,
            review_state.get("decisions", {}),
        ),
        "truncated": len(page) < len(catalog),
        "offset": 0,
        "limit": 200,
        "has_more": len(page) < len(catalog),
    }


@router.post("/{job_id}/rematerialize")
def rematerialize_reviewed_result(
    job_id: str,
    request: RematerializeRequest | None = None,
) -> dict:
    """Apply saved review decisions and produce a second, review-aware workbook.

    This closes the human-review loop: 确认可用 rows re-enter the result and 确认不采用
    rows leave it. It is a pure projection over the run's persisted result snapshot —
    no re-planning, no re-execution — and it is linted against the *same* OutputSpec,
    so review can never widen the delivery contract.
    """

    return _create_review_result_version(job_id, request or RematerializeRequest())


@router.get("/{job_id}/versions")
def get_result_versions(job_id: str) -> dict:
    return _public_result_version_state(list_result_versions(job_id), job_id)


@router.post("/{job_id}/versions/{version_id}/select")
def select_version(job_id: str, version_id: str) -> dict:
    payload = select_result_version(job_id, version_id)
    return {
        "job_id": job_id,
        "selected_version_id": payload["selected_result_version_id"],
    }


@router.post("/{job_id}/versions/undo")
def undo_version(job_id: str) -> dict:
    payload = undo_result_version(job_id)
    return {
        "job_id": job_id,
        "selected_version_id": payload["selected_result_version_id"],
    }


@router.post("/{job_id}/versions/{version_id}/branch")
def branch_version(job_id: str, version_id: str, request: ReviewRequest) -> dict:
    return _create_review_result_version(
        job_id,
        RematerializeRequest(
            parent_version_id=version_id,
            decisions=request.decisions,
        ),
    )


def _create_review_result_version(
    job_id: str,
    request: RematerializeRequest,
) -> dict:
    payload = read_job_status(job_id)
    if payload.get("status") not in {"succeeded", "completed", "success"}:
        raise HTTPException(status_code=409, detail="任务尚未成功完成，无法回写复核结果。")
    state = list_result_versions(job_id)
    parent_id = request.parent_version_id or state["selected_version_id"]
    decisions = (
        result_version_decisions(job_id, parent_id)
        if request.parent_version_id
        else dict(read_review_state(job_id).get("decisions", {}))
    )
    overlay = {item.row_id: item.status for item in request.decisions}
    invalid = sorted(set(overlay.values()) - _ALLOWED_REVIEW_STATUSES)
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid review status: {invalid}")
    valid_ids = {
        str(item.get("id"))
        for item in _review_catalog_items(payload, job_id)
        if isinstance(item, dict) and item.get("id")
    }
    if set(overlay) - valid_ids:
        raise HTTPException(status_code=400, detail="复核记录不存在或已更新，请刷新后重试。")
    decisions.update(overlay)
    materialize_relative_artifact(
        job_id,
        Path("_internal") / "result_snapshot.xlsx",
        payload=payload,
    )
    version_number = next_result_version_number(job_id)
    file_name = f"final_result_v{version_number}.xlsx"
    try:
        result = rematerialize_result(
            job_dir(job_id),
            output_spec=payload.get("output_spec") or {},
            decisions=decisions,
            review_items=_review_catalog_items(payload, job_id),
            file_name=file_name,
        )
    except OutputContractError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = {
        "accepted_rows": result.accepted_rows,
        "excluded_rows": result.excluded_rows,
        "pending_rows": result.pending_rows,
        "result_row_count": result.result_row_count,
        "file_name": result.path.name,
    }
    artifact_ref = publish_artifact(job_id, result.path)
    recorded = record_rematerialized_result(
        job_id,
        result_path=result.path,
        summary=summary,
        version_number=version_number,
        parent_version_id=parent_id,
        decisions=decisions,
        artifact_ref=artifact_ref,
    )
    return {
        "job_id": job_id,
        **summary,
        "version_id": recorded["selected_result_version_id"],
        "parent_version_id": parent_id,
        "url": f"/api/v1/jobs/{job_id}/files/{result.path.name}",
    }


@router.delete("/{job_id}")
async def delete_job(job_id: str) -> dict:
    """Cancel (if running) and remove a job plus its durable artifacts."""
    payload = read_job_status(job_id)
    manager = get_job_manager()
    if payload.get("status") not in TERMINAL_STATUSES or manager.is_active(job_id):
        await manager.cancel(job_id)
        return {"job_id": job_id, "status": "cancelled"}
    delete_job_dir(job_id)
    return {"job_id": job_id, "status": "deleted"}


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _build_business_answer(payload: dict, job_id: str) -> dict:
    counts = payload.get("counts", {})
    processing = payload.get("processing", {})
    goal_plan = payload.get("goal_plan", {})
    total = _int_value(counts.get("total"))
    valid = _int_value(counts.get("valid"))
    abnormal = _int_value(counts.get("abnormal"))
    filtered_out = max(0, total - valid - abnormal)
    quality_score = _resolve_quality_score(payload, total, abnormal)
    goal = str(payload.get("goal") or "未提供业务目标")
    version_state = list_result_versions(job_id)
    public_version_state = _public_result_version_state(version_state, job_id)

    return {
        "job_id": payload.get("job_id", job_id),
        "goal": goal,
        "goal_understanding": _business_goal_understanding(goal_plan),
        "output_spec": payload.get("output_spec", {}),
        "final_conclusion": _final_conclusion(total, valid, abnormal, quality_score),
        "key_metrics": [
            {"label": "总记录数", "value": total, "description": "本次处理的主表记录数"},
            {"label": "可直接使用", "value": valid, "description": "通过规则校验的数据量"},
            {"label": "需要复核", "value": abnormal, "description": "存在异常或不确定的数据量"},
            *(
                [
                    {
                        "label": "按目标排除",
                        "value": filtered_out,
                        "description": "按明确筛选或去重规则移出的记录数",
                    }
                ]
                if filtered_out
                else []
            ),
            {
                "label": "数据质量评分",
                "value": f"{quality_score}/100",
                "description": "综合完整性、规则校验和问题影响得出的结果质量",
            },
        ],
        "key_findings": _key_findings(processing, payload),
        "data_quality_score": quality_score,
        "result_preview": payload.get("result_rows", []),
        "exception_impact": _exception_impact(payload),
        "review_items": payload.get("review_items", []),
        "review_item_count": _review_item_count(payload),
        "review_items_truncated": bool(payload.get("review_items_truncated", False)),
        # Present once the user has re-materialised the result from their review
        # decisions, so the console can point at the review-aware workbook.
        "review_applied": payload.get("review_applied") or None,
        "can_rematerialize": _can_rematerialize(payload, job_id),
        "selected_result_version_id": public_version_state["selected_version_id"],
        "result_versions": public_version_state["versions"],
        "recommended_actions": _recommended_actions(abnormal, processing),
        "charts": [
            {
                "title": chart.get("title", ""),
                "url": (
                    f"/api/v1/jobs/{job_id}/files/{chart.get('file_name', '')}"
                ),
                "type": chart.get("chart_type", "html"),
            }
            for chart in payload.get("charts", [])
            if chart.get("file_name")
        ],
        "output_files": _output_files(payload, job_id),
    }


def _final_conclusion(total: int, valid: int, abnormal: int, quality_score: int) -> str:
    if total == 0:
        return "本次任务没有识别到可处理的数据记录，请检查上传文件是否包含有效表格。"
    filtered_out = max(0, total - valid - abnormal)
    filtered_text = f"，按目标排除 {filtered_out} 行" if filtered_out else ""
    return (
        f"本次共处理 {total} 行数据，其中 {valid} 行可直接使用，"
        f"{abnormal} 行需要业务复核{filtered_text}。"
        f"数据质量评分为 {quality_score}/100。"
    )


def _planned_actions(goal_plan: object) -> list[dict]:
    """One line per thing this run will do, in the user's own terms.

    A paragraph usually carries several asks ("补齐客户名称…打上大额标记…按城市汇总").
    The confirmation page showed capability ids, so a user could not tell whether all
    of their requests had been understood — the most common way a run silently
    delivers two of three things.
    """

    if not isinstance(goal_plan, dict):
        return []
    spec = goal_plan.get("task_spec")
    if not isinstance(spec, dict):
        return []

    planned: list[dict] = []
    for lookup in spec.get("join_requirements") or []:
        if isinstance(lookup, dict) and lookup.get("required"):
            planned.append({"kind": "lookup", "summary": "从关联表补齐字段", "source": "goal_text"})
    for item in spec.get("derivations") or []:
        if not isinstance(item, dict):
            continue
        labels = "/".join(
            str(branch.get("then")) for branch in item.get("branches") or []
        )
        planned.append(
            {
                "kind": "derive",
                "summary": f"新增列「{item.get('output')}」：{labels}" if labels else "新增标记列",
                "source": str(item.get("source") or "goal_text"),
            }
        )
    for item in spec.get("filters") or []:
        if not isinstance(item, dict):
            continue
        action = "排除" if item.get("mode") == "exclude" else "保留"
        rule = f"{item.get('field')} {item.get('op')} {item.get('value')}"
        planned.append(
            {
                "kind": "filter",
                "summary": f"{action} {rule} 的记录",
                "source": str(item.get("source") or "goal_text"),
                "destructive": True,
            }
        )
    for item in spec.get("deduplication") or []:
        if not isinstance(item, dict):
            continue
        planned.append(
            {
                "kind": "deduplicate",
                "summary": "按 " + "+".join(item.get("fields") or []) + " 去重",
                "source": str(item.get("source") or "goal_text"),
                "destructive": True,
            }
        )
    aggregation = spec.get("aggregation") or {}
    if aggregation.get("group_by"):
        metrics = "、".join(
            str(metric.get("output_name"))
            for metric in aggregation.get("metrics") or []
            if metric.get("output_name")
        )
        planned.append(
            {
                "kind": "aggregate",
                "summary": "按 " + "、".join(aggregation["group_by"]) + f" 汇总：{metrics}",
                "source": "goal_text",
            }
        )
    for action in spec.get("unmet_actions") or []:
        planned.append(
            {
                "kind": str(action),
                "summary": f"你要求的「{ACTION_LABELS.get(str(action), action)}」本次无法执行",
                "unmet": True,
            }
        )
    return planned


def _business_goal_understanding(goal_plan: object) -> dict:
    """Return only the goal fields needed for business review and confirmation."""

    if not isinstance(goal_plan, dict):
        return {}
    allowed = (
        # How the goal was read, and — when a model was configured but unreachable —
        # why it was read the lesser way. Without these the console could not tell the
        # user that today's run understood less than yesterday's, which is exactly the
        # question a differing result raises.
        "understanding_source",
        "understanding_fallback_reason",
        "understanding_confidence",
        "is_generic_fallback",
        "assumptions",
        "focus",
        "requested_outputs",
        "task_spec",
        "impact_preview",
    )
    return {key: goal_plan[key] for key in allowed if key in goal_plan}


def _key_findings(processing: dict, payload: dict) -> list[str]:
    findings: list[str] = []
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    total = _int_value(counts.get("total"))
    valid = _int_value(counts.get("valid"))
    abnormal = _int_value(counts.get("abnormal"))
    filtered_out = max(0, total - valid - abnormal)
    result_row_count = _int_value(payload.get("result_row_count"))

    if result_row_count:
        findings.append(f"结果文件已生成 {result_row_count} 行业务数据。")
    if filtered_out:
        findings.append(f"已按明确的筛选或去重要求移出 {filtered_out} 行记录。")
    if abnormal:
        findings.append(f"有 {abnormal} 行记录需要业务人员确认后再使用。")
    lookup_count = _int_value(processing.get("lookup_count"))
    if lookup_count:
        findings.append(f"已完成 {lookup_count} 项跨表信息补齐。")
    document_rule_count = _int_value(processing.get("document_rule_count"))
    if document_rule_count:
        findings.append(f"已按说明文档中的 {document_rule_count} 条要求完成核对。")
    return findings or ["任务已完成，可以预览并下载结果。"]


def _exception_impact(payload: dict) -> list[dict]:
    counts = payload.get("counts", {})
    abnormal = _int_value(counts.get("abnormal"))
    if abnormal:
        return [
            {
                "issue_type": "needs_review",
                "count": abnormal,
                "impact": "这些记录存在异常或不确定匹配，需要人工复核后再使用。",
            }
        ]
    return []


def _recommended_actions(abnormal: int, processing: dict) -> list[str]:
    actions = []
    if abnormal:
        actions.append("先下载结果 Excel，重点查看异常或需复核记录。")
        actions.append("查看问题类型、当前值和建议处理方式，再做业务判断。")
    else:
        actions.append("可下载结果 Excel，继续进行业务抽查或导入前校验。")
    if _int_value(processing.get("missing_import_field_count")):
        actions.append("导入前需要补齐模板要求但当前结果缺失的字段。")
    return actions


def _output_files(payload: dict, job_id: str) -> list[dict]:
    # Only advertise the deliverable. The job_status.json is process/observability
    # information reachable at GET /{job_id}; it is not a user-facing product file.
    output_files = [
        {
            "name": "final_result.xlsx",
            "url": f"/api/v1/jobs/{job_id}/files/final_result.xlsx",
            "type": "excel",
        },
    ]
    files = payload.get("files", {})
    version_names = {
        str(item.get("file_name") or "")
        for item in list_result_versions(job_id)["versions"]
        if int(item.get("version_number") or 1) > 1
    }
    for file_name in sorted(name for name in version_names if name):
        output_files.append(
            {
                "name": file_name,
                "url": f"/api/v1/jobs/{job_id}/files/{file_name}",
                "type": "excel",
            }
        )
    for key, file_type in (
        ("report_html", "html"),
        ("report_markdown", "markdown"),
    ):
        path = str(files.get(key) or "")
        if path:
            file_name = Path(path).name
            output_files.append(
                {
                    "name": file_name,
                    "url": f"/api/v1/jobs/{job_id}/files/{file_name}",
                    "type": file_type,
                }
            )
    return output_files


def _public_result_version_state(state: dict, job_id: str) -> dict:
    """Remove local/object-storage internals from the public version graph."""

    versions = []
    for raw in state.get("versions", []):
        file_name = str(raw.get("file_name") or "final_result.xlsx")
        versions.append(
            {
                "version_id": str(raw.get("version_id") or ""),
                "version_number": _int_value(raw.get("version_number")),
                "parent_version_id": raw.get("parent_version_id"),
                "kind": str(raw.get("kind") or "original"),
                "file_name": file_name,
                "url": f"/api/v1/jobs/{job_id}/files/{file_name}",
                "summary": raw.get("summary") or {},
                "created_at": raw.get("created_at"),
            }
        )
    return {
        "job_id": job_id,
        "selected_version_id": str(state.get("selected_version_id") or "v1"),
        "versions": versions,
    }


def _resolve_quality_score(payload: dict, total: int, abnormal: int) -> int:
    """Prefer the unified score persisted at run time; fall back for legacy jobs.

    Jobs run before scoring was unified have no ``quality_score`` field, so we keep
    the simplified exception-ratio estimate as a backward-compatible fallback only.
    """
    persisted = payload.get("quality_score")
    if persisted is not None:
        try:
            return max(0, min(100, int(persisted)))
        except (TypeError, ValueError):
            pass
    return _quality_score(total, abnormal)


def _quality_score(total: int, abnormal: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round((1 - abnormal / total) * 100)))


def _review_item_count(payload: dict) -> int:
    persisted = payload.get("review_item_count")
    return (
        _int_value(persisted)
        if persisted is not None
        else len(payload.get("review_items", []))
    )


def _pending_review_item_count(payload: dict, decisions: dict) -> int:
    total = _review_item_count(payload)
    resolved = sum(
        1
        for status in decisions.values()
        if status in {"accepted", "excluded"}
    )
    return max(0, total - resolved)


def _review_catalog_items(payload: dict, job_id: str) -> list[dict]:
    raw_path = str(payload.get("review_catalog_path") or "")
    if raw_path:
        path = Path(raw_path).resolve()
        root = job_dir(job_id).resolve()
        if _is_inside(path, root) and not path.exists():
            relative = path.relative_to(root)
            path = materialize_relative_artifact(
                job_id,
                relative,
                payload=payload,
            ).resolve()
        if _is_inside(path, root) and path.exists() and path.is_file():
            try:
                data = json.loads(read_store_text(path))
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, dict)]
            except (json.JSONDecodeError, OSError):
                pass
    return [
        item
        for item in payload.get("review_items", [])
        if isinstance(item, dict)
    ]


def _can_rematerialize(payload: dict, job_id: str) -> bool:
    """Whether at least one decided review row can be written back into a result."""

    if not has_relative_artifact(
        job_id,
        Path("_internal") / "result_snapshot.xlsx",
        payload=payload,
    ):
        return False
    decided = {
        row_id
        for row_id, status in read_review_state(job_id).get("decisions", {}).items()
        if status in {"accepted", "excluded"}
    }
    if not decided:
        return False
    return any(
        str(item.get("id")) in decided and item.get("result_index") is not None
        for item in _review_catalog_items(payload, job_id)
    )


def _require_ready(payload: dict, content: dict, label: str) -> None:
    if content:
        return
    status = payload.get("status")
    if status in {"failed", "cancelled"}:
        raise HTTPException(
            status_code=400,
            detail=payload.get("error") or f"{label}未生成。",
        )
    raise HTTPException(status_code=404, detail=f"{label}尚未生成，请稍候。")


def _int_value(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
