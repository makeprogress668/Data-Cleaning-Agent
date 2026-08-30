"""Phase 3A metadata persistence and cross-process CAS contracts."""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import HTTPException

import data_agent.api.job_store as job_store
from data_agent.api.metadata_migration import migrate_file_metadata
from data_agent.api.metadata_repository import (
    InMemoryMetadataRepository,
    PostgresMetadataRepository,
    configured_metadata_repository,
    metadata_backend,
)

_JOB_ID = "9" * 32


def _use_repository(
    repository: InMemoryMetadataRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(
        job_store,
        "configured_metadata_repository",
        lambda: repository,
    )


def test_external_repository_runs_the_complete_metadata_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    _use_repository(repository, tmp_path, monkeypatch)

    job_store.create_task_session(_JOB_ID, "按客户统计金额")
    pending = job_store.write_job_status(
        _JOB_ID,
        job_store.STATUS_PENDING,
        config={"goal": "按客户统计金额"},
    )
    assert pending["status"] == job_store.STATUS_PENDING
    assert not (tmp_path / "artifacts" / _JOB_ID / "job_status.json").exists()

    claimed, running = job_store.transition_job_status(
        _JOB_ID,
        job_store.STATUS_PENDING,
        job_store.STATUS_RUNNING,
    )
    assert claimed is True
    assert running["status"] == job_store.STATUS_RUNNING

    job_store.write_plan_snapshot(
        _JOB_ID,
        {"plan_id": "a" * 32, "plan_hash": "b" * 64},
    )
    job_store.write_plan_state(_JOB_ID, {"input_paths": ["orders.csv"]})
    job_store.record_session_plan(
        _JOB_ID,
        task_spec={"version": 1, "objective": "统计", "actions": []},
        plan_hash="b" * 64,
    )
    job_store.write_review_decisions(_JOB_ID, {"ISSUE-1": "accepted"})
    job_store.write_clarification_answers(_JOB_ID, {"group_by": "客户"})

    assert job_store.read_plan_state(_JOB_ID)["input_paths"] == ["orders.csv"]
    assert job_store.read_plan_snapshot(_JOB_ID)["plan_id"] == "a" * 32
    assert job_store.read_task_session(_JOB_ID)["current_plan_version"] == 1
    assert job_store.read_review_state(_JOB_ID)["decisions"] == {
        "ISSUE-1": "accepted"
    }
    assert job_store.list_job_statuses()[0]["job_id"] == _JOB_ID


def test_external_repository_status_cas_has_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    _use_repository(repository, tmp_path, monkeypatch)
    job_store.write_job_status(_JOB_ID, job_store.STATUS_PENDING, config={})
    outcomes: list[bool] = []

    def claim() -> None:
        changed, _document = job_store.transition_job_status(
            _JOB_ID,
            job_store.STATUS_PENDING,
            job_store.STATUS_RUNNING,
        )
        outcomes.append(changed)

    threads = [threading.Thread(target=claim) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 11


def test_external_repository_serializes_review_merges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    _use_repository(repository, tmp_path, monkeypatch)
    job_store.write_job_status(_JOB_ID, job_store.STATUS_SUCCEEDED, config={})

    threads = [
        threading.Thread(
            target=job_store.write_review_decisions,
            args=(_JOB_ID, {f"ISSUE-{index}": "accepted"}),
        )
        for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    decisions = job_store.read_review_state(_JOB_ID)["decisions"]
    assert len(decisions) == 20


def test_confirmation_identity_is_compared_inside_status_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    _use_repository(repository, tmp_path, monkeypatch)
    plan_id = "a" * 32
    plan_hash = "b" * 64
    job_store.write_job_status(_JOB_ID, job_store.STATUS_RUNNING, config={})
    job_store.write_plan_snapshot(
        _JOB_ID,
        {"plan_id": plan_id, "plan_hash": plan_hash},
    )
    job_store.transition_job_status(
        _JOB_ID,
        job_store.STATUS_RUNNING,
        job_store.STATUS_AWAITING_CONFIRMATION,
        payload={"plan_id": plan_id, "plan_hash": plan_hash},
    )

    with pytest.raises(HTTPException, match="处理计划已经更新"):
        job_store.transition_job_status(
            _JOB_ID,
            job_store.STATUS_AWAITING_CONFIRMATION,
            job_store.STATUS_RUNNING,
            expected_plan_id=plan_id,
            expected_plan_hash="c" * 64,
        )

    changed, _document = job_store.transition_job_status(
        _JOB_ID,
        job_store.STATUS_AWAITING_CONFIRMATION,
        job_store.STATUS_RUNNING,
        expected_plan_id=plan_id,
        expected_plan_hash=plan_hash,
    )
    assert changed is True


def test_metadata_backend_selection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "file")
    assert metadata_backend() == "file"
    assert configured_metadata_repository() is None

    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "postgres")
    monkeypatch.delenv("DATA_AGENT_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATA_AGENT_DATABASE_URL"):
        configured_metadata_repository()

    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "mongodb")
    with pytest.raises(RuntimeError, match="file 或 postgres"):
        metadata_backend()


def test_file_metadata_migration_is_idempotent_and_never_overwrites(
    tmp_path: Path,
) -> None:
    repository = InMemoryMetadataRepository()
    directory = tmp_path / _JOB_ID
    directory.mkdir()
    (directory / "job_status.json").write_text(
        json.dumps({"job_id": _JOB_ID, "status": "succeeded"}),
        encoding="utf-8",
    )
    (directory / "task_session.json").write_text(
        json.dumps({"job_id": _JOB_ID, "original_instruction": "旧任务"}),
        encoding="utf-8",
    )
    (directory / "review_state.json").write_text("{bad json", encoding="utf-8")
    repository.write_document(
        _JOB_ID,
        "task_session",
        {"job_id": _JOB_ID, "original_instruction": "数据库中的新任务"},
    )

    first = migrate_file_metadata(tmp_path, repository)
    second = migrate_file_metadata(tmp_path, repository)

    assert first.jobs_seen == 1
    assert first.documents_migrated == 1
    assert first.documents_skipped == 1
    assert first.documents_invalid == 1
    assert second.documents_migrated == 0
    assert repository.read_document(_JOB_ID, "task_session") == {
        "job_id": _JOB_ID,
        "original_instruction": "数据库中的新任务",
    }


class _FakeCursor:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool
        self.rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, parameters=()) -> None:  # noqa: ANN001
        normalized = " ".join(query.split()).upper()
        self.pool.queries.append(normalized)
        self.rows = []
        self.rowcount = 0
        if normalized.startswith("SELECT PG_ADVISORY_XACT_LOCK"):
            return
        if normalized.startswith("CREATE"):
            return
        if normalized.startswith("INSERT INTO DATA_AGENT_SCHEMA_MIGRATIONS"):
            return
        if normalized.startswith("INSERT INTO DATA_AGENT_UNDERSTANDING_CACHE"):
            key, payload = str(parameters[0]), str(parameters[1])
            self.pool.understanding_cache[key] = json.loads(payload)
            self.rowcount = 1
            return
        if normalized.startswith("SELECT PAYLOAD FROM DATA_AGENT_UNDERSTANDING_CACHE"):
            payload = self.pool.understanding_cache.get(str(parameters[0]))
            self.rows = [(payload,)] if payload is not None else []
            return
        if normalized.startswith("SELECT COUNT(*)"):
            count = len(self.pool.understanding_cache)
            timestamp = "2026-08-24T00:00:00+00:00" if count else None
            self.rows = [(count, timestamp, timestamp)]
            return
        if normalized.startswith("DELETE FROM DATA_AGENT_UNDERSTANDING_CACHE"):
            if "WHERE CACHE_KEY IN" not in normalized:
                self.rowcount = len(self.pool.understanding_cache)
                self.pool.understanding_cache.clear()
            return
        if normalized.startswith("INSERT"):
            job_id, kind = str(parameters[0]), str(parameters[1])
            key = (job_id, kind)
            if "'{}'::JSONB" in normalized:
                self.pool.documents.setdefault(key, {})
            else:
                self.pool.documents[key] = json.loads(parameters[2])
            self.rowcount = 1
            return
        if normalized.startswith("SELECT PAYLOAD") and "WHERE JOB_ID" in normalized:
            payload = self.pool.documents.get((str(parameters[0]), str(parameters[1])))
            self.rows = [(payload,)] if payload is not None else []
            return
        if normalized.startswith("SELECT PAYLOAD"):
            kind = str(parameters[0])
            documents = [
                payload
                for (_job_id, document_kind), payload in self.pool.documents.items()
                if document_kind == kind
            ]
            self.rows = [(payload,) for payload in documents]
            return
        if normalized.startswith("UPDATE"):
            payload, job_id, kind = parameters
            self.pool.documents[(str(job_id), str(kind))] = json.loads(payload)
            self.rowcount = 1
            return
        if normalized.startswith("DELETE"):
            job_id = str(parameters[0])
            keys = [key for key in self.pool.documents if key[0] == job_id]
            for key in keys:
                self.pool.documents.pop(key, None)
            self.rowcount = len(keys)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.pool)

    def transaction(self):
        return self.pool.lock

    def commit(self) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict] = {}
        self.understanding_cache: dict[str, dict] = {}
        self.queries: list[str] = []
        self.lock = threading.RLock()
        self.closed = False

    def connection(self):
        return nullcontext(_FakeConnection(self))

    def close(self) -> None:
        self.closed = True


def test_postgres_repository_uses_transactional_row_lock() -> None:
    pool = _FakePool()
    repository = PostgresMetadataRepository("postgresql://test", pool=pool)

    repository.initialize()
    repository.write_document(_JOB_ID, "status", {"status": "pending"})
    updated = repository.mutate_document(
        _JOB_ID,
        "status",
        lambda prior: {**(prior or {}), "status": "running"},
    )

    assert updated == {"status": "running"}
    assert repository.read_document(_JOB_ID, "status") == {"status": "running"}
    assert any("FOR UPDATE" in query for query in pool.queries)
    assert repository.delete_job(_JOB_ID) is True
    repository.close()
    assert pool.closed is True


def test_postgres_initialization_serializes_schema_ddl_across_processes() -> None:
    pool = _FakePool()
    repository = PostgresMetadataRepository("postgresql://test", pool=pool)

    repository.initialize()

    assert pool.queries[0].startswith("SELECT PG_ADVISORY_XACT_LOCK")
    assert any(
        "CREATE TABLE IF NOT EXISTS DATA_AGENT_UNDERSTANDING_CACHE" in query
        for query in pool.queries
    )
    assert any("IDX_DATA_AGENT_TENANT_JOB_DOCUMENT" in query for query in pool.queries)


def test_postgres_repository_persists_shared_understanding_cache() -> None:
    pool = _FakePool()
    repository = PostgresMetadataRepository("postgresql://test", pool=pool)
    key = "a" * 64

    repository.write_understanding_cache(key, {"suggested_base_table": "orders"}, 200)

    assert repository.read_understanding_cache(key) == {"suggested_base_table": "orders"}
    assert repository.understanding_cache_stats()["entry_count"] == 1
    assert repository.clear_understanding_cache() == 1
    assert repository.read_understanding_cache(key) is None


def test_postgres_understanding_cache_enforces_ttl_in_sql() -> None:
    pool = _FakePool()
    repository = PostgresMetadataRepository("postgresql://test", pool=pool)
    key = "b" * 64
    repository.write_understanding_cache(key, {"ok": True}, 200)

    assert repository.read_understanding_cache(key, max_age_seconds=3600) == {"ok": True}
    assert any("STORED_AT >= NOW()" in query for query in pool.queries)
