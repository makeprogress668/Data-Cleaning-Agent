from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from data_agent.api.metadata_repository import configured_metadata_repository
from data_agent.schemas.session import ConversationTurn, TaskSession
from data_agent.tenancy import current_tenant_id, default_tenant_id, tenant_from_config

PROJECT_ROOT = Path(__file__).resolve().parents[4]
logger = logging.getLogger(__name__)

# Job status vocabulary shared by the whole API. The console maps the legacy
# "completed"/"success" onto "succeeded", but new code should emit these directly.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
# needs_clarification and awaiting_confirmation are deliberately NOT terminal: the
# job pauses for user input (answers / plan approval) and resumes, so pollers keep
# polling and the console can render the questions or the plan-confirmation gate.
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})

# Per-process locks keyed by job_id so concurrent writers (executor threads,
# review submits) never interleave a read-modify-write on the same job files.
# A single process is the common single-worker deployment; multi-worker setups
# additionally get atomicity from the temp-file + os.replace write below.
_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_JOB_INDEX_LOCK = threading.Lock()
_TENANT_ADMISSION_LOCKS: dict[str, threading.Lock] = {}
_TENANT_ADMISSION_LOCKS_GUARD = threading.Lock()
_JOB_INDEX_VERSION = 1
_JOB_INDEX_LIMIT = 1000

_STATUS_DOCUMENT = "status"
_TASK_SESSION_DOCUMENT = "task_session"
_REVIEW_DOCUMENT = "review_state"
_PLAN_STATE_DOCUMENT = "plan_state"
_PLAN_SNAPSHOT_DOCUMENT = "plan_snapshot"
_CLARIFICATION_ANSWERS_DOCUMENT = "clarification_answers"


def _job_lock(job_id: str) -> threading.Lock:
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[job_id] = lock
        return lock


def _tenant_admission_lock(tenant_id: str) -> threading.Lock:
    with _TENANT_ADMISSION_LOCKS_GUARD:
        lock = _TENANT_ADMISSION_LOCKS.get(tenant_id)
        if lock is None:
            lock = threading.Lock()
            _TENANT_ADMISSION_LOCKS[tenant_id] = lock
        return lock


def api_work_dir() -> Path:
    return Path(os.environ.get("DATA_AGENT_API_WORK_DIR", PROJECT_ROOT / "data" / "api_jobs"))


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return api_work_dir() / job_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_index_file() -> Path:
    return api_work_dir() / "job_index.json"


# Windows opens files without FILE_SHARE_DELETE, so ``os.replace`` onto a path a
# reader currently holds fails with a sharing violation (WinError 5/32 -> Python
# PermissionError), and a read issued mid-replace fails the same way. POSIX renames
# have no such window, so these retries never trigger there. Bounded so a genuine
# permission fault still surfaces instead of hanging.
_SHARING_RETRY_ATTEMPTS = 40
_SHARING_RETRY_DELAY = 0.005


def _retry_on_sharing_violation(operation, description: str):
    """Run ``operation``, retrying the transient Windows sharing violation."""
    last_error: Optional[PermissionError] = None
    for attempt in range(_SHARING_RETRY_ATTEMPTS):
        try:
            return operation()
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(_SHARING_RETRY_DELAY * (attempt + 1), 0.05))
    assert last_error is not None
    logger.warning(
        "%s: gave up after %d sharing-violation retries",
        description,
        _SHARING_RETRY_ATTEMPTS,
    )
    raise last_error


def _path_lock(path: Path) -> threading.Lock:
    """Serialise same-process readers and writers of one store file.

    The retry above alone still loses races when a file is polled hard enough to be
    open almost continuously. Taking this lock on both sides removes the window for
    the single-worker deployment; the retry remains the fallback for multi-process
    setups, which share no lock. Always acquired *after* :func:`_job_lock`, and never
    around code that takes another lock, so the ordering stays deadlock-free.
    """
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def read_store_text(path: Path) -> str:
    """Read a job-store file, tolerating a concurrent atomic replace on Windows."""
    with _path_lock(path):
        return _retry_on_sharing_violation(
            lambda: path.read_text(encoding="utf-8"),
            f"read {path.name}",
        )


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically so a crash or concurrent read never sees half a file.

    Writes to a unique temp file in the same directory, flushes to disk, then
    ``os.replace`` (atomic on POSIX/Windows for same-filesystem moves).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Only the swap needs the lock; the temp name is already thread-unique.
        with _path_lock(path):
            _retry_on_sharing_violation(lambda: os.replace(tmp, path), f"replace {path.name}")
    finally:
        # A failed replace would otherwise leak the temp file into the job directory.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def job_meta_from_config(config: Optional[dict]) -> dict[str, str]:
    config = config or {}
    overrides = config.get("job_overrides") if isinstance(config, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}
    return {
        "goal": str(overrides.get("goal") or config.get("goal") or "未提供业务目标"),
        "mode": str(overrides.get("mode") or config.get("mode") or "answer"),
        "tenant_id": tenant_from_config(config),
    }


def build_status_payload(
    job_id: str,
    status: str,
    *,
    payload: Optional[dict] = None,
    config: Optional[dict] = None,
    error: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    """Assemble the ``job_status.json`` document for any lifecycle state.

    Pending/running states carry only lifecycle metadata; terminal success also
    carries the full result projection the console reads back (counts, discovery,
    plan, summary, files ...).
    """
    payload = payload or {}
    meta = job_meta_from_config(config)
    timestamp = now_iso()
    stable_created_at = created_at or timestamp
    queued_at = timestamp if status == STATUS_PENDING else payload.get("queued_at")
    started_at = timestamp if status == STATUS_RUNNING else payload.get("started_at")
    completed_at = timestamp if status in TERMINAL_STATUSES else None
    queue_wait_ms = (
        _iso_duration_ms(str(queued_at), timestamp)
        if status == STATUS_RUNNING and queued_at
        else payload.get("queue_wait_ms")
    )
    dispatch_attempts = int(payload.get("dispatch_attempts") or 0)
    if status == STATUS_RUNNING:
        dispatch_attempts += 1
    sla_seconds = max(0, int(os.environ.get("DATA_AGENT_JOB_SLA_SECONDS", "3600")))
    elapsed_ms = (
        _iso_duration_ms(stable_created_at, timestamp)
        if completed_at
        else None
    )
    payload_files = payload.get("files", {})
    persisted_files = {
        "download_url": f"/api/v1/cleaning/jobs/{job_id}/result.xlsx",
        "excel_path": payload_files.get("excel_path", ""),
        "job_config_path": payload_files.get("job_config_path", ""),
    }
    persisted_files.update(
        {
            key: value
            for key, value in payload_files.items()
            if isinstance(value, str)
            and (key.startswith(("chart_", "report_")) or key == "result_v2_path")
        }
    )
    document = {
        "job_id": job_id,
        "tenant_id": str(payload.get("tenant_id") or meta["tenant_id"]),
        "status": status,
        "mode": meta["mode"],
        "goal": meta["goal"],
        "created_at": stable_created_at,
        "updated_at": timestamp,
        "queued_at": queued_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "queue_wait_ms": queue_wait_ms,
        "dispatch_attempts": dispatch_attempts,
        "sla_seconds": sla_seconds,
        "sla_breached": bool(
            elapsed_ms is not None
            and sla_seconds > 0
            and elapsed_ms > sla_seconds * 1000
        ),
        "input": payload.get("input", {}),
        "counts": payload.get("counts", {}),
        "quality_score": payload.get("quality_score"),
        "processing": payload.get("processing", {}),
        "goal_plan": payload.get("goal_plan", {}),
        "execution_plan": payload.get("execution_plan", {}),
        "output_spec": payload.get("output_spec", {}),
        "plan_id": payload.get("plan_id", ""),
        "plan_hash": payload.get("plan_hash", ""),
        "queue_message": payload.get("queue_message"),
        "input_artifacts": payload.get("input_artifacts", []),
        "artifact_manifest": payload.get("artifact_manifest", []),
        "clarification_questions": payload.get("clarification_questions", []),
        "discovery": payload.get("discovery", {}),
        "plan": payload.get("plan", {}),
        "summary": payload.get("summary", []),
        "result_rows": payload.get("result_rows", []),
        "review_items": payload.get("review_items", []),
        "review_item_count": payload.get("review_item_count", 0),
        "review_items_truncated": bool(payload.get("review_items_truncated", False)),
        "review_catalog_path": payload.get("review_catalog_path", ""),
        "result_row_count": payload.get("result_row_count", 0),
        "charts": payload.get("charts", []),
        "execution_events": payload.get("execution_events", []),
        "execution_duration_ms": payload.get("execution_duration_ms"),
        "llm_usage": payload.get("llm_usage", []),
        "retryable": bool(payload.get("retryable", False)),
        "failure_stage": payload.get("failure_stage"),
        "files": persisted_files,
    }
    if error:
        document["error"] = error
        document["error_message"] = error
    return document


def write_job_status(job_id: str, status: str, **kwargs: Any) -> dict:
    """Persist a job status document atomically, preserving created_at across writes."""
    repository = configured_metadata_repository()
    if repository is not None:
        job_dir(job_id)  # Validate the external identifier before using it as a key.
        requested_created_at = kwargs.pop("created_at", None)

        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            created_at = requested_created_at or (
                prior.get("created_at") if prior else None
            )
            call_kwargs = dict(kwargs)
            if prior is not None:
                call_kwargs["payload"] = _preserve_artifact_references(
                    prior,
                    dict(call_kwargs.get("payload") or {}),
                )
            return build_status_payload(
                job_id,
                status,
                created_at=created_at,
                **call_kwargs,
            )

        document = repository.mutate_document(
            job_id,
            _STATUS_DOCUMENT,
            update,
            create=True,
        )
        assert document is not None
        return document

    directory = api_work_dir() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    status_file = directory / "job_status.json"
    with _job_lock(job_id):
        created_at = kwargs.pop("created_at", None)
        prior_document: dict[str, Any] | None = None
        if created_at is None and status_file.exists():
            try:
                prior_document = json.loads(read_store_text(status_file))
                created_at = prior_document.get("created_at")
            except (json.JSONDecodeError, OSError):
                created_at = None
        if prior_document is not None:
            kwargs["payload"] = _preserve_artifact_references(
                prior_document,
                dict(kwargs.get("payload") or {}),
            )
        document = build_status_payload(job_id, status, created_at=created_at, **kwargs)
        atomic_write_json(status_file, document)
        _upsert_job_index(document)
    return document


def transition_job_status(
    job_id: str,
    expected: str | set[str] | frozenset[str],
    status: str,
    *,
    expected_plan_id: str | None = None,
    expected_plan_hash: str | None = None,
    **kwargs: Any,
) -> tuple[bool, dict]:
    """Atomically compare-and-set a job status.

    Clarification, confirmation and cancellation use this operation so concurrent
    requests cannot schedule the same job twice. When no payload/config is supplied,
    the previous document is projected into the new state to preserve user-visible
    context.
    """

    allowed = {expected} if isinstance(expected, str) else set(expected)
    repository = configured_metadata_repository()
    if repository is not None:
        job_dir(job_id)
        legacy_snapshot = (
            repository.read_document(job_id, _PLAN_SNAPSHOT_DOCUMENT)
            if expected_plan_id is not None or expected_plan_hash is not None
            else None
        )
        changed = False

        def transition(prior: dict[str, Any] | None) -> dict[str, Any]:
            nonlocal changed
            assert prior is not None
            if prior.get("status") not in allowed:
                return prior
            if expected_plan_id is not None or expected_plan_hash is not None:
                frozen = prior if prior.get("plan_hash") else (legacy_snapshot or {})
                actual_hash = str(frozen.get("plan_hash") or "")
                actual_id = str(frozen.get("plan_id") or actual_hash[:32])
                id_matches = expected_plan_id is None or actual_id == expected_plan_id
                hash_matches = expected_plan_hash is None or hmac.compare_digest(
                    actual_hash,
                    expected_plan_hash,
                )
                if not actual_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="处理计划快照不存在，请重新生成处理计划。",
                    )
                if not id_matches or not hash_matches:
                    raise HTTPException(
                        status_code=409,
                        detail="处理计划已经更新，请刷新页面后重新确认。",
                    )
            payload = _preserve_artifact_references(
                prior,
                kwargs.pop("payload", prior),
            )
            config = kwargs.pop(
                "config",
                {
                    "goal": prior.get("goal", ""),
                    "mode": prior.get("mode", "answer"),
                },
            )
            changed = True
            return build_status_payload(
                job_id,
                status,
                payload=payload,
                config=config,
                created_at=prior.get("created_at"),
                **kwargs,
            )

        document = repository.mutate_document(
            job_id,
            _STATUS_DOCUMENT,
            transition,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return changed, document

    directory = api_work_dir() / job_id
    status_file = directory / "job_status.json"
    with _job_lock(job_id):
        if not status_file.exists():
            raise HTTPException(status_code=404, detail="任务不存在")
        try:
            prior = json.loads(read_store_text(status_file))
        except (json.JSONDecodeError, OSError) as exc:
            raise HTTPException(status_code=500, detail="任务状态读取失败，请稍后重试") from exc
        if prior.get("status") not in allowed:
            return False, prior

        if expected_plan_id is not None or expected_plan_hash is not None:
            snapshot_file = _plan_snapshot_file(job_id)
            if not snapshot_file.exists():
                raise HTTPException(
                    status_code=409,
                    detail="处理计划快照不存在，请重新生成处理计划。",
                )
            try:
                snapshot = json.loads(read_store_text(snapshot_file))
            except (json.JSONDecodeError, OSError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail="处理计划快照无法读取，请重新生成处理计划。",
                ) from exc
            actual_hash = str(snapshot.get("plan_hash") or "")
            # Version 3 snapshots predate plan_id. Their content hash is the stable
            # confirmation identity during the compatibility window.
            actual_id = str(snapshot.get("plan_id") or actual_hash[:32])
            id_matches = expected_plan_id is None or actual_id == expected_plan_id
            hash_matches = expected_plan_hash is None or hmac.compare_digest(
                actual_hash,
                expected_plan_hash,
            )
            if not id_matches or not hash_matches:
                raise HTTPException(
                    status_code=409,
                    detail="处理计划已经更新，请刷新页面后重新确认。",
                )

        payload = _preserve_artifact_references(
            prior,
            kwargs.pop("payload", prior),
        )
        config = kwargs.pop(
            "config",
            {
                "goal": prior.get("goal", ""),
                "mode": prior.get("mode", "answer"),
            },
        )
        document = build_status_payload(
            job_id,
            status,
            payload=payload,
            config=config,
            created_at=prior.get("created_at"),
            **kwargs,
        )
        atomic_write_json(status_file, document)
        _upsert_job_index(document)
    return True, document


def _preserve_artifact_references(prior: dict, payload: dict) -> dict:
    """Keep durable lifecycle anchors when a transition narrows its payload."""

    merged = dict(payload)
    for field in (
        "tenant_id",
        "input_artifacts",
        "artifact_manifest",
        "queued_at",
        "started_at",
        "queue_wait_ms",
        "dispatch_attempts",
    ):
        if field not in merged and prior.get(field):
            merged[field] = prior[field]
    return merged


def _iso_duration_ms(start: str, end: str) -> float | None:
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (finished - started).total_seconds() * 1000), 3)


def read_job_status(job_id: str) -> dict:
    repository = configured_metadata_repository()
    if repository is not None:
        job_dir(job_id)
        document = repository.read_document(job_id, _STATUS_DOCUMENT)
        if document is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return document

    status_file = job_dir(job_id) / "job_status.json"
    if not status_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        return json.loads(read_store_text(status_file))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail="任务状态读取失败，请稍后重试") from exc


def public_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove queue and storage internals from HTTP-facing status documents."""

    return {
        key: value
        for key, value in payload.items()
        if key not in {"queue_message", "input_artifacts", "artifact_manifest"}
    }


def list_job_statuses(
    limit: int = 20,
    *,
    tenant_id: str | None = None,
) -> list[dict]:
    """Return recent task summaries from the bounded lightweight index.

    Existing deployments rebuild the derived index once from status files. Normal
    reads then parse one small file instead of every historical job document.
    """

    repository = configured_metadata_repository()
    if repository is not None:
        bounded_limit = max(1, min(limit, 100))
        return [
            _job_list_projection(document)
            for document in repository.list_documents(
                _STATUS_DOCUMENT,
                limit=bounded_limit,
                tenant_id=tenant_id,
            )
        ]

    root = api_work_dir()
    if not root.exists():
        return []

    bounded_limit = max(1, min(limit, 100))
    with _JOB_INDEX_LOCK:
        jobs = _read_job_index_unlocked()
        if jobs is None:
            jobs = _rebuild_job_index_unlocked()
        existing = [
            item
            for item in jobs
            if (root / str(item.get("job_id")) / "job_status.json").is_file()
        ]
        if len(existing) != len(jobs):
            _write_job_index_unlocked(existing)
        if tenant_id is not None:
            existing = [
                item
                for item in existing
                if str(item.get("tenant_id") or default_tenant_id()) == tenant_id
            ]
        return existing[:bounded_limit]


def tenant_resource_usage(tenant_id: str) -> dict[str, int]:
    """Return authoritative active-job and stored-byte usage for one tenant."""

    repository = configured_metadata_repository()
    if repository is not None:
        documents = repository.list_documents(
            _STATUS_DOCUMENT,
            tenant_id=tenant_id,
        )
    else:
        documents = []
        root = api_work_dir()
        if root.exists():
            for entry in root.iterdir():
                if not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", entry.name):
                    continue
                status_file = entry / "job_status.json"
                try:
                    document = json.loads(read_store_text(status_file))
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    continue
                if str(document.get("tenant_id") or default_tenant_id()) == tenant_id:
                    documents.append(document)

    active_statuses = {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_NEEDS_CLARIFICATION,
        STATUS_AWAITING_CONFIRMATION,
    }
    active_jobs = sum(
        1 for document in documents if document.get("status") in active_statuses
    )
    stored_objects: dict[str, int] = {}
    for document in documents:
        for field in ("input_artifacts", "artifact_manifest"):
            entries = document.get(field)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("key") or "")
                if not key:
                    continue
                try:
                    stored_objects[key] = max(0, int(entry.get("size_bytes") or 0))
                except (TypeError, ValueError):
                    continue

    if repository is None:
        # Local artifacts have no object manifest. Count the actual tenant-owned
        # workspaces so the same quota semantics hold in a single-node deployment.
        stored_bytes = 0
        for document in documents:
            job_id = str(document.get("job_id") or "")
            try:
                directory = job_dir(job_id)
            except HTTPException:
                continue
            for path in directory.rglob("*") if directory.exists() else []:
                try:
                    if path.is_file():
                        stored_bytes += path.stat().st_size
                except OSError:
                    continue
    else:
        stored_bytes = sum(stored_objects.values())
    return {
        "active_jobs": active_jobs,
        "stored_bytes": stored_bytes,
    }


def list_pending_queue_messages(limit: int = 1000) -> list[dict[str, Any]]:
    """Return durable outbox messages that have not yet been claimed by a worker."""

    repository = configured_metadata_repository()
    if repository is None:
        return []
    messages: list[dict[str, Any]] = []
    for document in repository.list_documents(
        _STATUS_DOCUMENT,
        limit=max(1, min(limit, 10_000)),
    ):
        message = document.get("queue_message")
        if document.get("status") == STATUS_PENDING and isinstance(message, dict):
            messages.append(dict(message))
    return messages


def _job_list_projection(payload: dict[str, Any]) -> dict[str, Any]:
    discovery = payload.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    counts = payload.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    files = payload.get("files")
    files = files if isinstance(files, dict) else {}
    return {
        "job_id": str(payload.get("job_id") or ""),
        "tenant_id": str(payload.get("tenant_id") or default_tenant_id()),
        "status": payload.get("status", STATUS_PENDING),
        "mode": payload.get("mode", "answer"),
        "goal": payload.get("goal", ""),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "queued_at": payload.get("queued_at"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "queue_wait_ms": payload.get("queue_wait_ms"),
        "dispatch_attempts": payload.get("dispatch_attempts", 0),
        "sla_seconds": payload.get("sla_seconds"),
        "sla_breached": bool(payload.get("sla_breached", False)),
        "error": payload.get("error", ""),
        "error_message": payload.get("error_message", payload.get("error", "")),
        "failure_stage": payload.get("failure_stage"),
        "clarification_questions": payload.get("clarification_questions", []),
        "counts": {
            "total": counts.get("total", 0),
            "valid": counts.get("valid", 0),
            "abnormal": counts.get("abnormal", 0),
        },
        "quality_score": payload.get("quality_score"),
        "review_item_count": payload.get("review_item_count", 0),
        "result_row_count": payload.get("result_row_count", 0),
        "discovery": {"files": list(discovery.get("files") or [])}
        if discovery
        else {},
        "plan": {"available": True} if payload.get("plan") else {},
        "files": {"excel_path": files.get("excel_path", "")},
    }


def _job_sort_key(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(payload.get("created_at") or ""),
        str(payload.get("updated_at") or ""),
        str(payload.get("job_id") or ""),
    )


def _read_job_index_unlocked() -> list[dict[str, Any]] | None:
    path = _job_index_file()
    if not path.exists():
        return None
    try:
        document = json.loads(read_store_text(path))
    except (json.JSONDecodeError, OSError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("version") != _JOB_INDEX_VERSION
        or not isinstance(document.get("jobs"), list)
    ):
        return None
    return [
        item
        for item in document["jobs"]
        if isinstance(item, dict)
        and re.fullmatch(r"[a-f0-9]{32}", str(item.get("job_id") or ""))
    ]


def _write_job_index_unlocked(jobs: list[dict[str, Any]]) -> None:
    ordered = sorted(jobs, key=_job_sort_key, reverse=True)[:_JOB_INDEX_LIMIT]
    try:
        atomic_write_json(
            _job_index_file(),
            {"version": _JOB_INDEX_VERSION, "jobs": ordered},
        )
    except OSError:
        logger.warning("failed to update derived job index", exc_info=True)


def _rebuild_job_index_unlocked() -> list[dict[str, Any]]:
    root = api_work_dir()
    jobs: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", entry.name):
            continue
        status_file = entry / "job_status.json"
        if not status_file.exists():
            continue
        try:
            payload = json.loads(read_store_text(status_file))
        except (json.JSONDecodeError, OSError):
            continue
        if payload.get("job_id") == entry.name:
            jobs.append(_job_list_projection(payload))
    _write_job_index_unlocked(jobs)
    return sorted(jobs, key=_job_sort_key, reverse=True)[:_JOB_INDEX_LIMIT]


def _upsert_job_index(payload: dict[str, Any]) -> None:
    projection = _job_list_projection(payload)
    job_id = projection["job_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return
    with _JOB_INDEX_LOCK:
        jobs = _read_job_index_unlocked()
        if jobs is None:
            jobs = _rebuild_job_index_unlocked()
        jobs = [item for item in jobs if item.get("job_id") != job_id]
        jobs.append(projection)
        _write_job_index_unlocked(jobs)


def _remove_jobs_from_index(job_ids: set[str]) -> None:
    if not job_ids:
        return
    with _JOB_INDEX_LOCK:
        jobs = _read_job_index_unlocked()
        if jobs is None:
            jobs = _rebuild_job_index_unlocked()
        _write_job_index_unlocked(
            [item for item in jobs if item.get("job_id") not in job_ids]
        )


def update_job_observability(
    job_id: str,
    *,
    llm_usage: list[dict[str, Any]],
) -> dict:
    """Merge task-local observability after a background worker finishes."""

    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            if prior is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            return {
                **prior,
                "llm_usage": llm_usage,
                "updated_at": now_iso(),
            }

        document = repository.mutate_document(
            job_id,
            _STATUS_DOCUMENT,
            update,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return document

    status_file = job_dir(job_id) / "job_status.json"
    with _job_lock(job_id):
        try:
            document = json.loads(read_store_text(status_file))
        except (json.JSONDecodeError, OSError) as exc:
            raise HTTPException(
                status_code=500,
                detail="任务观测信息写入失败，请稍后重试",
            ) from exc
        document["llm_usage"] = llm_usage
        document["updated_at"] = now_iso()
        atomic_write_json(status_file, document)
        _upsert_job_index(document)
    return document


def record_rematerialized_result(
    job_id: str,
    *,
    result_path: Path,
    summary: dict[str, Any],
    version_number: int = 2,
    parent_version_id: str | None = None,
    decisions: dict[str, str] | None = None,
    artifact_ref: dict[str, Any] | None = None,
) -> dict:
    """Attach the review-applied workbook to a finished job document.

    Kept separate from :func:`write_job_status` because the job is already terminal:
    re-materialisation adds a deliverable without changing the run's outcome.
    """

    version_id = f"v{version_number}"

    def updated_document(prior: dict[str, Any] | None) -> dict[str, Any]:
        if prior is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        versions = _result_versions_from_payload(job_id, prior)
        if any(item["version_id"] == version_id for item in versions):
            raise HTTPException(status_code=409, detail="结果版本号冲突，请重试。")
        selected_id = str(prior.get("selected_result_version_id") or versions[-1]["version_id"])
        parent_id = parent_version_id or selected_id
        if parent_id not in {item["version_id"] for item in versions}:
            raise HTTPException(status_code=404, detail="父结果版本不存在")
        version = {
            "version_id": version_id,
            "version_number": version_number,
            "parent_version_id": parent_id,
            "kind": "review",
            "file_name": result_path.name,
            "path": str(result_path),
            "summary": copy_json(summary),
            "decisions": _normalize_review_decisions(decisions or {}),
            "created_at": now_iso(),
        }
        versions.append(version)
        files = prior.get("files")
        persisted_files = (
            {
                **files,
                f"result_v{version_number}_path": str(result_path),
                "selected_result_path": str(result_path),
            }
            if isinstance(files, dict)
            else {
                f"result_v{version_number}_path": str(result_path),
                "selected_result_path": str(result_path),
            }
        )
        manifest = _merge_artifact_manifest(
            prior.get("artifact_manifest"),
            artifact_ref,
        )
        return {
            **prior,
            "files": persisted_files,
            "artifact_manifest": manifest,
            "result_versions": versions,
            "selected_result_version_id": version_id,
            "review_applied": {
                **summary,
                "version_id": version_id,
                "parent_version_id": parent_id,
                "updated_at": now_iso(),
            },
            "updated_at": now_iso(),
        }

    repository = configured_metadata_repository()
    if repository is not None:
        document = repository.mutate_document(
            job_id,
            _STATUS_DOCUMENT,
            updated_document,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return document

    status_file = job_dir(job_id) / "job_status.json"
    with _job_lock(job_id):
        try:
            document = json.loads(read_store_text(status_file))
        except (json.JSONDecodeError, OSError) as exc:
            raise HTTPException(
                status_code=500,
                detail="复核结果写入失败，请稍后重试",
            ) from exc
        document = updated_document(document)
        atomic_write_json(status_file, document)
        _upsert_job_index(document)
    return document


def list_result_versions(job_id: str) -> dict[str, Any]:
    payload = read_job_status(job_id)
    versions = _result_versions_from_payload(job_id, payload)
    return {
        "job_id": job_id,
        "selected_version_id": str(
            payload.get("selected_result_version_id") or versions[-1]["version_id"]
        ),
        "versions": versions,
    }


def next_result_version_number(job_id: str) -> int:
    versions = list_result_versions(job_id)["versions"]
    return max(int(item["version_number"]) for item in versions) + 1


def select_result_version(job_id: str, version_id: str) -> dict[str, Any]:
    """Move the selected pointer without deleting any deliverable."""

    def update(prior: dict[str, Any] | None) -> dict[str, Any]:
        if prior is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        versions = _result_versions_from_payload(job_id, prior)
        selected = next(
            (item for item in versions if item["version_id"] == version_id),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="结果版本不存在")
        files = dict(prior.get("files") or {})
        files["selected_result_path"] = str(selected.get("path") or "")
        return {
            **prior,
            "files": files,
            "result_versions": versions,
            "selected_result_version_id": version_id,
            "review_applied": (
                {
                    **dict(selected.get("summary") or {}),
                    "version_id": version_id,
                    "parent_version_id": selected.get("parent_version_id"),
                    "updated_at": now_iso(),
                }
                if int(selected.get("version_number") or 1) > 1
                else None
            ),
            "updated_at": now_iso(),
        }

    repository = configured_metadata_repository()
    if repository is not None:
        document = repository.mutate_document(job_id, _STATUS_DOCUMENT, update)
        if document is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return document
    status_file = job_dir(job_id) / "job_status.json"
    with _job_lock(job_id):
        document = update(json.loads(read_store_text(status_file)))
        atomic_write_json(status_file, document)
        _upsert_job_index(document)
    return document


def undo_result_version(job_id: str) -> dict[str, Any]:
    state = list_result_versions(job_id)
    selected = next(
        item
        for item in state["versions"]
        if item["version_id"] == state["selected_version_id"]
    )
    parent_id = selected.get("parent_version_id")
    if not parent_id:
        raise HTTPException(status_code=409, detail="当前已经是最初结果，无法继续撤销。")
    return select_result_version(job_id, str(parent_id))


def result_version_decisions(job_id: str, version_id: str) -> dict[str, str]:
    state = list_result_versions(job_id)
    version = next(
        (item for item in state["versions"] if item["version_id"] == version_id),
        None,
    )
    if version is None:
        raise HTTPException(status_code=404, detail="结果版本不存在")
    return _normalize_review_decisions(version.get("decisions") or {})


def _result_versions_from_payload(job_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("result_versions")
    versions = (
        [copy_json(item) for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )
    if versions:
        return versions
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    original_path = str(files.get("excel_path") or job_dir(job_id) / "output" / "final_result.xlsx")
    return [
        {
            "version_id": "v1",
            "version_number": 1,
            "parent_version_id": None,
            "kind": "original",
            "file_name": Path(original_path).name or "final_result.xlsx",
            "path": original_path,
            "summary": {
                "result_row_count": int(payload.get("result_row_count") or 0),
            },
            "decisions": {},
            "created_at": str(payload.get("completed_at") or payload.get("updated_at") or ""),
        }
    ]


def copy_json(value: Any) -> Any:
    """Copy JSON-compatible metadata without sharing nested mutable state."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def admit_job_status(
    job_id: str,
    status: str,
    *,
    config: Optional[dict] = None,
    max_active_jobs: int = 0,
) -> dict:
    """Atomically create the first status document within a tenant job quota."""

    job_dir(job_id)
    tenant_id = job_meta_from_config(config)["tenant_id"]
    document = build_status_payload(job_id, status, config=config)
    active_statuses = {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_NEEDS_CLARIFICATION,
        STATUS_AWAITING_CONFIRMATION,
    }
    repository = configured_metadata_repository()
    if repository is not None:
        admitted = repository.create_status_with_quota(
            job_id,
            document,
            tenant_id=tenant_id,
            max_active_jobs=max_active_jobs,
            active_statuses=active_statuses,
        )
    else:
        with _tenant_admission_lock(tenant_id):
            usage = tenant_resource_usage(tenant_id)
            admitted = not max_active_jobs or usage["active_jobs"] < max_active_jobs
            if admitted:
                status_file = job_dir(job_id) / "job_status.json"
                if status_file.exists():
                    admitted = False
                else:
                    atomic_write_json(status_file, document)
                    _upsert_job_index(document)
    if not admitted:
        raise HTTPException(
            status_code=429,
            detail=(
                f"当前租户同时进行的任务已达到 {max_active_jobs} 个上限，"
                "请等待任务完成或取消后重试。"
            ),
        )
    return document


def _merge_artifact_manifest(
    existing: object,
    reference: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    manifest = [item for item in existing if isinstance(item, dict)] if isinstance(
        existing,
        list,
    ) else []
    if reference is None:
        return manifest
    relative = str(reference.get("relative_path") or "")
    return [
        *[
            item
            for item in manifest
            if str(item.get("relative_path") or "") != relative
        ],
        reference,
    ]


def _review_state_file(job_id: str) -> Path:
    return job_dir(job_id) / "review_state.json"


def _task_session_file(job_id: str) -> Path:
    return job_dir(job_id) / "task_session.json"


def create_task_session(job_id: str, instruction: str) -> dict:
    """Create one durable task session with the initial user instruction."""

    max_rounds = _max_clarification_rounds()
    turns = (
        [
            ConversationTurn(
                role="user",
                kind="instruction",
                content=instruction,
            )
        ]
        if instruction
        else []
    )
    session = TaskSession(
        job_id=job_id,
        original_instruction=instruction,
        max_clarification_rounds=max_rounds,
        turns=turns,
    )
    repository = configured_metadata_repository()
    if repository is not None:
        payload = session.model_dump(mode="json")
        document = repository.mutate_document(
            job_id,
            _TASK_SESSION_DOCUMENT,
            lambda prior: prior or payload,
            create=True,
        )
        assert document is not None
        return TaskSession.model_validate(document).model_dump(mode="json")

    with _job_lock(job_id):
        state_file = _task_session_file(job_id)
        if state_file.exists():
            return _read_task_session_unlocked(state_file).model_dump(mode="json")
        payload = session.model_dump(mode="json")
        atomic_write_json(state_file, payload)
    return payload


def read_task_session(job_id: str) -> dict:
    repository = configured_metadata_repository()
    if repository is not None:
        document = repository.read_document(job_id, _TASK_SESSION_DOCUMENT)
        if document is None:
            raise HTTPException(status_code=404, detail="任务会话不存在")
        try:
            return TaskSession.model_validate(document).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail="任务会话读取失败，请稍后重试",
            ) from exc

    state_file = _task_session_file(job_id)
    if not state_file.exists():
        raise HTTPException(status_code=404, detail="任务会话不存在")
    with _job_lock(job_id):
        return _read_task_session_unlocked(state_file).model_dump(mode="json")


def update_task_session(job_id: str, **updates: Any) -> dict:
    """Atomically update session metadata without replacing its turn history."""

    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            if prior is None:
                raise HTTPException(status_code=404, detail="任务会话不存在")
            payload = TaskSession.model_validate(prior).model_dump(mode="python")
            payload.update(updates)
            payload["updated_at"] = datetime.now(timezone.utc)
            return TaskSession.model_validate(payload).model_dump(mode="json")

        document = repository.mutate_document(
            job_id,
            _TASK_SESSION_DOCUMENT,
            update,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务会话不存在")
        return document

    with _job_lock(job_id):
        state_file = _task_session_file(job_id)
        session = _read_task_session_unlocked(state_file)
        payload = session.model_dump(mode="python")
        payload.update(updates)
        payload["updated_at"] = datetime.now(timezone.utc)
        updated = TaskSession.model_validate(payload)
        document = updated.model_dump(mode="json")
        atomic_write_json(state_file, document)
    return document


def record_session_plan(
    job_id: str,
    *,
    task_spec: dict[str, Any],
    plan_hash: str,
) -> dict:
    """Record a new TaskSpec/PlanSnapshot version and its audit turn."""

    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            if prior is None:
                raise HTTPException(status_code=404, detail="任务会话不存在")
            session = TaskSession.model_validate(prior)
            turn = ConversationTurn(
                role="assistant",
                kind="plan_created",
                content="已生成可执行处理计划。",
                structured_payload={
                    "plan_hash": plan_hash,
                    "task_spec_version": session.current_task_spec_version + 1,
                    "plan_version": session.current_plan_version + 1,
                },
            )
            return session.model_copy(
                update={
                    "status": "planned",
                    "current_task_spec_version": session.current_task_spec_version + 1,
                    "current_plan_version": session.current_plan_version + 1,
                    "current_plan_hash": plan_hash,
                    "task_spec": task_spec,
                    "turns": [*session.turns, turn],
                    "updated_at": datetime.now(timezone.utc),
                }
            ).model_dump(mode="json")

        document = repository.mutate_document(
            job_id,
            _TASK_SESSION_DOCUMENT,
            update,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务会话不存在")
        return document

    with _job_lock(job_id):
        state_file = _task_session_file(job_id)
        session = _read_task_session_unlocked(state_file)
        turn = ConversationTurn(
            role="assistant",
            kind="plan_created",
            content="已生成可执行处理计划。",
            structured_payload={
                "plan_hash": plan_hash,
                "task_spec_version": session.current_task_spec_version + 1,
                "plan_version": session.current_plan_version + 1,
            },
        )
        updated = session.model_copy(
            update={
                "status": "planned",
                "current_task_spec_version": session.current_task_spec_version + 1,
                "current_plan_version": session.current_plan_version + 1,
                "current_plan_hash": plan_hash,
                "task_spec": task_spec,
                "turns": [*session.turns, turn],
                "updated_at": datetime.now(timezone.utc),
            }
        )
        document = updated.model_dump(mode="json")
        atomic_write_json(state_file, document)
    return document


def record_clarification_question(
    job_id: str,
    *,
    question: str,
    structured_payload: dict[str, Any],
) -> dict:
    """Append one assistant question and atomically advance the round counter."""

    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            if prior is None:
                raise HTTPException(status_code=404, detail="任务会话不存在")
            session = TaskSession.model_validate(prior)
            next_round = session.clarification_round + 1
            if next_round > session.max_clarification_rounds:
                return session.model_dump(mode="json")
            turn = ConversationTurn(
                role="assistant",
                kind="clarification_question",
                content=question,
                structured_payload={
                    **structured_payload,
                    "clarification_round": next_round,
                },
            )
            return session.model_copy(
                update={
                    "status": STATUS_NEEDS_CLARIFICATION,
                    "clarification_round": next_round,
                    "turns": [*session.turns, turn],
                    "updated_at": datetime.now(timezone.utc),
                }
            ).model_dump(mode="json")

        document = repository.mutate_document(
            job_id,
            _TASK_SESSION_DOCUMENT,
            update,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务会话不存在")
        return document

    with _job_lock(job_id):
        state_file = _task_session_file(job_id)
        session = _read_task_session_unlocked(state_file)
        next_round = session.clarification_round + 1
        if next_round > session.max_clarification_rounds:
            return session.model_dump(mode="json")
        turn = ConversationTurn(
            role="assistant",
            kind="clarification_question",
            content=question,
            structured_payload={
                **structured_payload,
                "clarification_round": next_round,
            },
        )
        updated = session.model_copy(
            update={
                "status": STATUS_NEEDS_CLARIFICATION,
                "clarification_round": next_round,
                "turns": [*session.turns, turn],
                "updated_at": datetime.now(timezone.utc),
            }
        )
        document = updated.model_dump(mode="json")
        atomic_write_json(state_file, document)
    return document


def answered_session_slots(job_id: str) -> set[str]:
    """Return question/slot ids already answered in this task session."""

    session = TaskSession.model_validate(read_task_session(job_id))
    answered: set[str] = set()
    for turn in session.turns:
        if turn.kind != "clarification_answer":
            continue
        for item in turn.structured_payload.get("answers", []):
            if isinstance(item, dict) and item.get("question_id"):
                answered.add(str(item["question_id"]))
    return answered


def append_session_turn(
    job_id: str,
    *,
    role: str,
    kind: str,
    content: str,
    structured_payload: Optional[dict[str, Any]] = None,
    status: Optional[str] = None,
) -> dict:
    """Append one validated turn and optionally update the session status."""

    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            if prior is None:
                raise HTTPException(status_code=404, detail="任务会话不存在")
            session = TaskSession.model_validate(prior)
            turn = ConversationTurn.model_validate(
                {
                    "role": role,
                    "kind": kind,
                    "content": content,
                    "structured_payload": structured_payload or {},
                }
            )
            changes: dict[str, Any] = {
                "turns": [*session.turns, turn],
                "updated_at": datetime.now(timezone.utc),
            }
            if status:
                changes["status"] = status
            return session.model_copy(update=changes).model_dump(mode="json")

        document = repository.mutate_document(
            job_id,
            _TASK_SESSION_DOCUMENT,
            update,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="任务会话不存在")
        return document

    with _job_lock(job_id):
        state_file = _task_session_file(job_id)
        session = _read_task_session_unlocked(state_file)
        turn = ConversationTurn.model_validate(
            {
                "role": role,
                "kind": kind,
                "content": content,
                "structured_payload": structured_payload or {},
            }
        )
        update = {
            "turns": [*session.turns, turn],
            "updated_at": datetime.now(timezone.utc),
        }
        if status:
            update["status"] = status
        updated = session.model_copy(update=update)
        document = updated.model_dump(mode="json")
        atomic_write_json(state_file, document)
    return document


def _read_task_session_unlocked(path: Path) -> TaskSession:
    if not path.exists():
        raise HTTPException(status_code=404, detail="任务会话不存在")
    try:
        return TaskSession.model_validate_json(read_store_text(path))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail="任务会话读取失败，请稍后重试") from exc


def _max_clarification_rounds() -> int:
    raw = os.environ.get("DATA_AGENT_MAX_CLARIFICATION_ROUNDS", "3")
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 3


def read_review_state(job_id: str) -> dict:
    """Return persisted human-review decisions for a job (empty if none yet)."""
    read_job_status(job_id)
    repository = configured_metadata_repository()
    if repository is not None:
        payload = repository.read_document(job_id, _REVIEW_DOCUMENT)
        if payload is None:
            return {"job_id": job_id, "decisions": {}, "updated_at": None}
        payload["decisions"] = _normalize_review_decisions(
            payload.get("decisions", {})
        )
        return payload

    state_file = _review_state_file(job_id)
    if not state_file.exists():
        return {"job_id": job_id, "decisions": {}, "updated_at": None}
    try:
        payload = json.loads(read_store_text(state_file))
        payload["decisions"] = _normalize_review_decisions(
            payload.get("decisions", {})
        )
        return payload
    except (json.JSONDecodeError, OSError):
        return {"job_id": job_id, "decisions": {}, "updated_at": None}


def write_review_decisions(job_id: str, decisions: dict[str, str]) -> dict:
    """Merge and persist review decisions ({row_id: status}); lock-guarded + atomic.

    The lock closes the read-modify-write race that previously let two concurrent
    submits clobber each other's decisions (lost update).
    """
    read_job_status(job_id)
    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            existing = prior.get("decisions", {}) if prior else {}
            merged = {
                **_normalize_review_decisions(existing),
                **_normalize_review_decisions(decisions),
            }
            return {
                "job_id": job_id,
                "decisions": merged,
                "updated_at": now_iso(),
            }

        document = repository.mutate_document(
            job_id,
            _REVIEW_DOCUMENT,
            update,
            create=True,
        )
        assert document is not None
        return document

    with _job_lock(job_id):
        state_file = _review_state_file(job_id)
        existing: dict[str, str] = {}
        if state_file.exists():
            try:
                existing = json.loads(read_store_text(state_file)).get("decisions", {})
            except (json.JSONDecodeError, OSError):
                existing = {}
        merged = {
            **_normalize_review_decisions(existing),
            **_normalize_review_decisions(decisions),
        }
        payload = {"job_id": job_id, "decisions": merged, "updated_at": now_iso()}
        atomic_write_json(state_file, payload)
    return payload


def _normalize_review_decisions(decisions: Any) -> dict[str, str]:
    if not isinstance(decisions, dict):
        return {}
    allowed = {"pending", "accepted", "excluded"}
    return {
        str(row_id): status if status in allowed else "pending"
        for row_id, raw_status in decisions.items()
        if (status := str(raw_status))
    }


def _plan_state_file(job_id: str) -> Path:
    return job_dir(job_id) / "plan_state.json"


def _plan_snapshot_file(job_id: str) -> Path:
    return job_dir(job_id) / "plan_snapshot.json"


def _clarification_answers_file(job_id: str) -> Path:
    return job_dir(job_id) / "clarification_answers.json"


def write_plan_state(job_id: str, plan_state: dict) -> dict:
    """Persist the resume anchor for a paused (needs_clarification) job.

    Holds the input paths, prepared config path, goal, and the questions asked, so
    JobManager.resume can reload and continue execution after the user answers.
    """
    repository = configured_metadata_repository()
    if repository is not None:
        return repository.write_document(
            job_id,
            _PLAN_STATE_DOCUMENT,
            plan_state,
        )

    with _job_lock(job_id):
        atomic_write_json(_plan_state_file(job_id), plan_state)
    return plan_state


def read_plan_state(job_id: str) -> dict:
    """Return the persisted plan state for a paused job (empty dict if none)."""
    repository = configured_metadata_repository()
    if repository is not None:
        return repository.read_document(job_id, _PLAN_STATE_DOCUMENT) or {}

    state_file = _plan_state_file(job_id)
    if not state_file.exists():
        return {}
    try:
        return json.loads(read_store_text(state_file))
    except (json.JSONDecodeError, OSError):
        return {}


def write_plan_snapshot(job_id: str, snapshot: dict) -> dict:
    """Persist the immutable, validated plan used by confirmation and execution."""

    repository = configured_metadata_repository()
    if repository is not None:
        return repository.write_document(
            job_id,
            _PLAN_SNAPSHOT_DOCUMENT,
            snapshot,
        )

    with _job_lock(job_id):
        atomic_write_json(_plan_snapshot_file(job_id), snapshot)
    return snapshot


def read_plan_snapshot(job_id: str) -> dict:
    """Read a previously frozen plan snapshot."""

    repository = configured_metadata_repository()
    if repository is not None:
        return repository.read_document(job_id, _PLAN_SNAPSHOT_DOCUMENT) or {}

    state_file = _plan_snapshot_file(job_id)
    if not state_file.exists():
        return {}
    try:
        return json.loads(read_store_text(state_file))
    except (json.JSONDecodeError, OSError):
        return {}


def write_clarification_answers(job_id: str, answers: dict[str, str]) -> dict:
    """Merge and persist user answers to clarification questions ({question_id: answer})."""
    read_job_status(job_id)
    repository = configured_metadata_repository()
    if repository is not None:
        def update(prior: dict[str, Any] | None) -> dict[str, Any]:
            existing = prior.get("answers", {}) if prior else {}
            return {
                "job_id": job_id,
                "answers": {**existing, **answers},
                "updated_at": now_iso(),
            }

        document = repository.mutate_document(
            job_id,
            _CLARIFICATION_ANSWERS_DOCUMENT,
            update,
            create=True,
        )
        assert document is not None
        return document

    with _job_lock(job_id):
        state_file = _clarification_answers_file(job_id)
        existing: dict[str, str] = {}
        if state_file.exists():
            try:
                existing = json.loads(read_store_text(state_file)).get("answers", {})
            except (json.JSONDecodeError, OSError):
                existing = {}
        merged = {**existing, **answers}
        payload = {"job_id": job_id, "answers": merged, "updated_at": now_iso()}
        atomic_write_json(state_file, payload)
    return payload


def delete_job(job_id: str) -> bool:
    """Remove one job's metadata, local cache and object-store namespace."""
    import shutil

    from data_agent.api.artifact_store import artifact_key, configured_artifact_store

    directory = job_dir(job_id)
    try:
        tenant_id = str(read_job_status(job_id).get("tenant_id") or default_tenant_id())
    except HTTPException:
        tenant_id = current_tenant_id()
    store = configured_artifact_store()
    objects_deleted = (
        store.delete_prefix(artifact_key(job_id, tenant_id=tenant_id))
        if store is not None
        else 0
    )
    repository = configured_metadata_repository()
    if repository is not None:
        metadata_deleted = repository.delete_job(job_id)
        artifact_deleted = directory.exists()
        if artifact_deleted:
            with _job_lock(job_id):
                shutil.rmtree(directory, ignore_errors=True)
        return metadata_deleted or artifact_deleted or bool(objects_deleted)

    if not directory.exists():
        return bool(objects_deleted)
    with _job_lock(job_id):
        shutil.rmtree(directory, ignore_errors=True)
    _remove_jobs_from_index({job_id})
    return True


def purge_expired_jobs(ttl_seconds: int, *, now: Optional[float] = None) -> int:
    """Delete job directories whose status file is older than ttl_seconds.

    Best-effort disk hygiene so long-running deployments don't accumulate jobs
    forever. Returns the number of jobs removed. ttl_seconds <= 0 disables it.
    """
    if ttl_seconds <= 0:
        return 0
    import shutil

    repository = configured_metadata_repository()
    if repository is not None:
        from data_agent.api.artifact_store import artifact_key, configured_artifact_store

        cutoff = (now if now is not None else time.time()) - ttl_seconds
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        tenant_by_job = {
            str(document.get("job_id") or ""): str(
                document.get("tenant_id") or default_tenant_id()
            )
            for document in repository.list_documents(_STATUS_DOCUMENT)
        }
        expired = repository.purge_jobs_before(cutoff_iso)
        store = configured_artifact_store()
        for job_id in expired:
            if store is not None:
                store.delete_prefix(
                    artifact_key(
                        job_id,
                        tenant_id=tenant_by_job.get(job_id, default_tenant_id()),
                    )
                )
            directory = job_dir(job_id)
            if directory.exists():
                with _job_lock(job_id):
                    shutil.rmtree(directory, ignore_errors=True)
        return len(expired)

    root = api_work_dir()
    if not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    removed = 0
    removed_job_ids: set[str] = set()
    for entry in root.iterdir():
        if not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", entry.name):
            continue
        status_file = entry / "job_status.json"
        reference = status_file if status_file.exists() else entry
        try:
            mtime = reference.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            removed_job_ids.add(entry.name)
    _remove_jobs_from_index(removed_job_ids)
    return removed


def reconcile_interrupted_jobs() -> int:
    """Mark orphaned pending/running jobs as explicitly retryable failures.

    Paused clarification/confirmation jobs remain valid because their resume anchors
    are durable. A local ThreadPoolExecutor cannot survive a process restart, so
    pretending a prior ``running`` state is still active would leave the UI polling
    forever.
    """

    # ARQ owns retry/recovery for queued and running work. Marking those jobs failed
    # merely because an API process restarted would race a healthy remote worker.
    from data_agent.api.job_queue import durable_queue_enabled

    if durable_queue_enabled():
        return 0

    repository = configured_metadata_repository()
    if repository is not None:
        reconciled = 0
        for prior in repository.list_documents(_STATUS_DOCUMENT):
            if prior.get("status") not in {STATUS_PENDING, STATUS_RUNNING}:
                continue
            job_id = str(prior.get("job_id") or "")
            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                continue
            payload = {**prior, "retryable": True}
            changed, _document = transition_job_status(
                job_id,
                {STATUS_PENDING, STATUS_RUNNING},
                STATUS_FAILED,
                payload=payload,
                error="服务重启中断了任务，请重新提交或重试。",
            )
            reconciled += int(changed)
        return reconciled

    root = api_work_dir()
    if not root.exists():
        return 0
    reconciled = 0
    for entry in root.iterdir():
        if not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", entry.name):
            continue
        status_file = entry / "job_status.json"
        if not status_file.exists():
            continue
        try:
            prior = json.loads(read_store_text(status_file))
        except (json.JSONDecodeError, OSError):
            continue
        if prior.get("status") not in {STATUS_PENDING, STATUS_RUNNING}:
            continue
        payload = {**prior, "retryable": True}
        changed, _document = transition_job_status(
            entry.name,
            {STATUS_PENDING, STATUS_RUNNING},
            STATUS_FAILED,
            payload=payload,
            error="服务重启中断了任务，请重新提交或重试。",
        )
        reconciled += int(changed)
    return reconciled
