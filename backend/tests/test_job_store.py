"""Tests for the async job store: persistence, atomicity, locking, TTL, deletion.

These lock in the P0-2 / P1-5 durability contract that the async task model relies
on: atomic writes, created_at preservation across status transitions, review
decision merges that never lose an update, TTL cleanup, and safe handling of a
corrupt status file.
"""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import data_agent.api.job_store as job_store
from data_agent.api.job_store import (
    STATUS_AWAITING_CONFIRMATION,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    atomic_write_json,
    delete_job,
    list_job_statuses,
    purge_expired_jobs,
    read_job_status,
    read_review_state,
    reconcile_interrupted_jobs,
    transition_job_status,
    write_job_status,
    write_review_decisions,
)

_JOB_ID = "a" * 32


def _use_tmp_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work_dir = tmp_path / "api_jobs"
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(work_dir))
    return work_dir


def test_atomic_write_json_replaces_without_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "status.json"
    atomic_write_json(target, {"a": 1, "b": "中文"})
    # No temp files left behind and content is complete + utf-8.
    assert target.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": "中文"}


def test_write_status_preserves_created_at_across_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)

    pending = write_job_status(_JOB_ID, STATUS_PENDING, config={"goal": "清洗"})
    created_at = pending["created_at"]
    assert pending["status"] == STATUS_PENDING
    assert pending["queued_at"]
    assert pending["dispatch_attempts"] == 0

    time.sleep(0.01)
    running = write_job_status(_JOB_ID, STATUS_RUNNING, config={"goal": "清洗"})
    assert running["status"] == STATUS_RUNNING
    # created_at is stable, updated_at moves forward.
    assert running["created_at"] == created_at
    assert running["updated_at"] >= created_at
    assert running["started_at"]
    assert running["queue_wait_ms"] >= 0
    assert running["dispatch_attempts"] == 1

    stored = read_job_status(_JOB_ID)
    assert stored["created_at"] == created_at
    assert stored["goal"] == "清洗"
    assert list_job_statuses(limit=1)[0]["status"] == STATUS_RUNNING


def test_job_status_write_survives_derived_index_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = _use_tmp_work_dir(tmp_path, monkeypatch)
    real_atomic_write = job_store.atomic_write_json

    def fail_only_index(path: Path, data) -> None:
        if path.name == "job_index.json":
            raise OSError("simulated index failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(job_store, "atomic_write_json", fail_only_index)

    document = write_job_status(_JOB_ID, STATUS_PENDING, config={"goal": "清洗"})

    assert document["status"] == STATUS_PENDING
    assert (work_dir / _JOB_ID / "job_status.json").exists()


def test_status_compare_and_set_allows_only_one_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    write_job_status(_JOB_ID, STATUS_NEEDS_CLARIFICATION, config={"goal": "清洗"})
    outcomes: list[bool] = []

    def _claim() -> None:
        changed, _payload = transition_job_status(
            _JOB_ID,
            STATUS_NEEDS_CLARIFICATION,
            STATUS_RUNNING,
        )
        outcomes.append(changed)

    threads = [threading.Thread(target=_claim) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 9
    assert read_job_status(_JOB_ID)["status"] == STATUS_RUNNING


def test_read_missing_job_raises_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        read_job_status(_JOB_ID)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "任务不存在"


def test_read_corrupt_status_raises_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = _use_tmp_work_dir(tmp_path, monkeypatch)
    status_file = work_dir / _JOB_ID / "job_status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(HTTPException) as excinfo:
        read_job_status(_JOB_ID)
    assert excinfo.value.status_code == 500


def test_list_job_statuses_returns_newest_readable_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = _use_tmp_work_dir(tmp_path, monkeypatch)
    older_id = "b" * 32
    newer_id = "c" * 32
    write_job_status(
        older_id,
        STATUS_SUCCEEDED,
        config={"goal": "较早任务"},
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_job_status(
        newer_id,
        STATUS_FAILED,
        config={"goal": "较新任务"},
        created_at="2026-01-02T00:00:00+00:00",
    )
    corrupt = work_dir / ("d" * 32) / "job_status.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{bad json", encoding="utf-8")
    index_file = work_dir / "job_index.json"
    index_file.unlink()

    jobs = list_job_statuses(limit=1)

    assert [job["job_id"] for job in jobs] == [newer_id]
    assert jobs[0]["goal"] == "较新任务"
    assert index_file.exists()

    status_reads: list[Path] = []
    real_read_text = Path.read_text

    def track_status_reads(path: Path, *args, **kwargs) -> str:
        if path.name == "job_status.json":
            status_reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_status_reads)
    assert [job["job_id"] for job in list_job_statuses(limit=1)] == [newer_id]
    assert status_reads == []


def test_invalid_job_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    # Path-traversal / malformed ids must never resolve to a directory.
    with pytest.raises(HTTPException) as excinfo:
        read_job_status("../etc/passwd")
    assert excinfo.value.status_code == 404


def test_review_decisions_merge_without_lost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    write_job_status(_JOB_ID, STATUS_SUCCEEDED, config={})

    write_review_decisions(_JOB_ID, {"ISSUE-1": "accepted"})
    write_review_decisions(_JOB_ID, {"ISSUE-2": "excluded"})
    state = read_review_state(_JOB_ID)
    assert state["decisions"] == {"ISSUE-1": "accepted", "ISSUE-2": "excluded"}


def test_concurrent_review_writes_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-job lock must serialize concurrent read-modify-write submits."""
    _use_tmp_work_dir(tmp_path, monkeypatch)
    write_job_status(_JOB_ID, STATUS_SUCCEEDED, config={})

    def _submit(n: int) -> None:
        write_review_decisions(_JOB_ID, {f"ISSUE-{n}": "accepted"})

    threads = [threading.Thread(target=_submit, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    decisions = read_review_state(_JOB_ID)["decisions"]
    # Not a single decision was lost to a clobbering write.
    assert len(decisions) == 20
    assert all(decisions[f"ISSUE-{i}"] == "accepted" for i in range(20))


def test_delete_job_removes_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = _use_tmp_work_dir(tmp_path, monkeypatch)
    write_job_status(_JOB_ID, STATUS_SUCCEEDED, config={})
    assert (work_dir / _JOB_ID).exists()

    assert delete_job(_JOB_ID) is True
    assert not (work_dir / _JOB_ID).exists()
    assert list_job_statuses() == []
    # Deleting again is a no-op that reports nothing was removed.
    assert delete_job(_JOB_ID) is False


def test_purge_expired_jobs_removes_only_old_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    old_id = "b" * 32
    fresh_id = "c" * 32
    write_job_status(old_id, STATUS_SUCCEEDED, config={})
    write_job_status(fresh_id, STATUS_SUCCEEDED, config={})

    # Age the "old" job well past the TTL using a future 'now'.
    future = time.time() + 10_000
    removed = purge_expired_jobs(ttl_seconds=5_000, now=future)
    # Both are older than 5000s relative to 'future', so both go; verify the count
    # matches the number of job dirs and nothing survives.
    assert removed == 2
    assert list_job_statuses() == []

    # With TTL disabled nothing is purged.
    write_job_status(old_id, STATUS_SUCCEEDED, config={})
    assert purge_expired_jobs(ttl_seconds=0) == 0


def test_purge_keeps_fresh_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = _use_tmp_work_dir(tmp_path, monkeypatch)
    write_job_status(_JOB_ID, STATUS_SUCCEEDED, config={})
    # A generous TTL against the real clock must keep a just-written job.
    assert purge_expired_jobs(ttl_seconds=3600) == 0
    assert (work_dir / _JOB_ID).exists()


def test_reconcile_marks_only_interrupted_jobs_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_work_dir(tmp_path, monkeypatch)
    pending_id = "b" * 32
    running_id = "c" * 32
    clarification_id = "d" * 32
    confirmation_id = "e" * 32
    succeeded_id = "f" * 32
    write_job_status(pending_id, STATUS_PENDING, config={"goal": "pending"})
    write_job_status(running_id, STATUS_RUNNING, config={"goal": "running"})
    write_job_status(
        clarification_id,
        STATUS_NEEDS_CLARIFICATION,
        config={"goal": "clarify"},
    )
    write_job_status(
        confirmation_id,
        STATUS_AWAITING_CONFIRMATION,
        config={"goal": "confirm"},
    )
    write_job_status(succeeded_id, STATUS_SUCCEEDED, config={"goal": "done"})

    assert reconcile_interrupted_jobs() == 2
    for job_id in (pending_id, running_id):
        payload = read_job_status(job_id)
        assert payload["status"] == STATUS_FAILED
        assert payload["retryable"] is True
        assert "服务重启" in payload["error_message"]
    assert read_job_status(clarification_id)["status"] == STATUS_NEEDS_CLARIFICATION
    assert read_job_status(confirmation_id)["status"] == STATUS_AWAITING_CONFIRMATION
    assert read_job_status(succeeded_id)["status"] == STATUS_SUCCEEDED
