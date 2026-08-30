"""Phase 3B durable queue, outbox replay and worker-claim contracts."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

import data_agent.api.job_manager as job_manager_module
import data_agent.api.job_store as job_store
from data_agent.api.job_manager import JobManager, redispatch_pending_messages
from data_agent.api.job_queue import (
    QueueMessage,
    enqueue_message,
    validate_queue_configuration,
)
from data_agent.api.metadata_repository import InMemoryMetadataRepository

_JOB_ID = "7" * 32


def _enable_durable_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> InMemoryMetadataRepository:
    repository = InMemoryMetadataRepository()
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DATA_AGENT_JOB_QUEUE_BACKEND", "arq")
    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "postgres")
    monkeypatch.setenv("DATA_AGENT_REDIS_URL", "redis://queue.test:6379/0")
    monkeypatch.setattr(
        job_store,
        "configured_metadata_repository",
        lambda: repository,
    )
    return repository


def test_durable_submit_persists_outbox_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_durable_runtime(tmp_path, monkeypatch)
    enqueued: list[QueueMessage] = []

    async def capture(message: QueueMessage) -> None:
        enqueued.append(message)

    monkeypatch.setattr(job_manager_module, "enqueue_message", capture)
    manager = JobManager()
    try:
        asyncio.run(
            manager.submit(
                _JOB_ID,
                [tmp_path / "orders.csv"],
                {"goal": "清洗订单"},
            )
        )
        status = job_store.read_job_status(_JOB_ID)
        message = status["queue_message"]

        assert status["status"] == job_store.STATUS_PENDING
        assert message["action"] == "run"
        assert message["payload"]["saved_paths"] == [str(tmp_path / "orders.csv")]
        assert enqueued == [message]
    finally:
        manager.shutdown()


def test_failed_enqueue_is_replayed_from_postgres_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_durable_runtime(tmp_path, monkeypatch)
    attempts: list[str] = []

    async def unavailable(message: QueueMessage) -> None:
        attempts.append(str(message["dispatch_id"]))
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(job_manager_module, "enqueue_message", unavailable)
    manager = JobManager()
    try:
        asyncio.run(manager.submit(_JOB_ID, [], {"goal": "清洗"}))
        pending = job_store.read_job_status(_JOB_ID)
        assert pending["status"] == job_store.STATUS_PENDING

        recovered: list[QueueMessage] = []

        async def available(message: QueueMessage) -> None:
            recovered.append(message)

        monkeypatch.setattr(job_manager_module, "enqueue_message", available)
        assert asyncio.run(redispatch_pending_messages()) == 1
        assert recovered[0]["dispatch_id"] == pending["queue_message"]["dispatch_id"]
        assert attempts == [pending["queue_message"]["dispatch_id"]]
    finally:
        manager.shutdown()


def test_worker_claim_rejects_stale_dispatch_and_cancelled_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_durable_runtime(tmp_path, monkeypatch)
    enqueued: list[QueueMessage] = []

    async def capture(message: QueueMessage) -> None:
        enqueued.append(message)

    monkeypatch.setattr(job_manager_module, "enqueue_message", capture)
    manager = JobManager()
    try:
        asyncio.run(manager.submit(_JOB_ID, [], {"goal": "清洗"}))
        message = enqueued[0]
        stale = QueueMessage(
            dispatch_id="stale",
            action="run",
            job_id=_JOB_ID,
            payload=message["payload"],
        )
        assert manager._claim_queue_message(stale) is False
        assert manager._claim_queue_message(message) is True
        assert job_store.read_job_status(_JOB_ID)["status"] == job_store.STATUS_RUNNING

        assert asyncio.run(manager.cancel(_JOB_ID)) is True
        assert manager._claim_queue_message(message) is False
        assert job_store.read_job_status(_JOB_ID)["status"] == job_store.STATUS_CANCELLED
    finally:
        manager.shutdown()


def test_durable_runtime_does_not_fail_jobs_when_api_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_durable_runtime(tmp_path, monkeypatch)
    job_store.write_job_status(
        _JOB_ID,
        job_store.STATUS_RUNNING,
        payload={"queue_message": {"dispatch_id": "active"}},
        config={},
    )

    assert job_store.reconcile_interrupted_jobs() == 0
    assert job_store.read_job_status(_JOB_ID)["status"] == job_store.STATUS_RUNNING


def test_exhausted_worker_retry_closes_the_current_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_durable_runtime(tmp_path, monkeypatch)
    enqueued: list[QueueMessage] = []

    async def capture(message: QueueMessage) -> None:
        enqueued.append(message)

    monkeypatch.setattr(job_manager_module, "enqueue_message", capture)
    manager = JobManager()
    try:
        asyncio.run(manager.submit(_JOB_ID, [], {"goal": "清洗"}))
        message = enqueued[0]
        assert manager._claim_queue_message(message) is True

        assert manager.fail_queue_message(message, ConnectionError("redis lost")) is True
        status = job_store.read_job_status(_JOB_ID)
        assert status["status"] == job_store.STATUS_FAILED
        assert status["retryable"] is True
        assert status["failure_stage"] == "execution"
    finally:
        manager.shutdown()


def test_arq_adapter_enqueues_with_stable_dispatch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_JOB_QUEUE_BACKEND", "arq")
    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "postgres")
    monkeypatch.setenv("DATA_AGENT_REDIS_URL", "redis://queue.test:6379/0")
    monkeypatch.setenv("DATA_AGENT_ARQ_QUEUE", "agent-tests")
    calls: list[tuple] = []

    class FakeRedis:
        async def enqueue_job(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls.append((args, kwargs))

        async def aclose(self) -> None:
            calls.append((("closed",), {}))

    async def create_pool(settings):  # noqa: ANN001
        assert settings == "redis://queue.test:6379/0"
        return FakeRedis()

    arq_module = ModuleType("arq")
    arq_module.create_pool = create_pool
    connections_module = ModuleType("arq.connections")
    connections_module.RedisSettings = SimpleNamespace(from_dsn=lambda dsn: dsn)
    monkeypatch.setitem(sys.modules, "arq", arq_module)
    monkeypatch.setitem(sys.modules, "arq.connections", connections_module)
    message = QueueMessage(
        dispatch_id="dispatch-1",
        action="run",
        job_id=_JOB_ID,
        payload={"saved_paths": []},
    )

    asyncio.run(enqueue_message(message))

    args, kwargs = calls[0]
    assert args == ("execute_job_message", message)
    assert kwargs == {"_job_id": "dispatch-1", "_queue_name": "agent-tests"}
    assert calls[-1][0] == ("closed",)


def test_arq_requires_postgres_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_JOB_QUEUE_BACKEND", "arq")
    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "file")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        validate_queue_configuration()

    monkeypatch.setenv("DATA_AGENT_METADATA_BACKEND", "postgres")
    monkeypatch.delenv("DATA_AGENT_REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATA_AGENT_REDIS_URL"):
        validate_queue_configuration()


def test_compose_worker_healthcheck_targets_redis_instead_of_api_http() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    healthcheck = compose["services"]["worker"]["healthcheck"]
    command = " ".join(healthcheck["test"])

    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    assert "DATA_AGENT_REDIS_URL" in command
    assert "127.0.0.1:8000" not in command
