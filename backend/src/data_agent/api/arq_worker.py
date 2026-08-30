"""ARQ worker entry point for durable cross-process job execution."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from data_agent.api.artifact_store import (
    close_configured_artifact_store,
    configured_artifact_store,
    validate_artifact_configuration,
)
from data_agent.api.job_manager import get_job_manager
from data_agent.api.job_queue import QueueMessage, validate_queue_configuration
from data_agent.api.metadata_repository import (
    close_configured_metadata_repository,
    configured_metadata_repository,
)

try:
    from arq.connections import RedisSettings
except ImportError:  # pragma: no cover - production extra owns this dependency
    RedisSettings = None  # type: ignore[assignment,misc]


async def execute_job_message(_ctx: dict[str, Any], message: QueueMessage) -> None:
    """Run blocking pandas/openpyxl work outside the worker event loop."""

    manager = get_job_manager()
    try:
        await asyncio.to_thread(manager.execute_queue_message, message)
    except Exception as exc:
        attempt = max(1, int(_ctx.get("job_try") or 1))
        max_tries = max(1, int(os.environ.get("DATA_AGENT_ARQ_MAX_TRIES", "3")))
        if attempt >= max_tries:
            await asyncio.to_thread(manager.fail_queue_message, message, exc)
        raise


async def startup(_ctx: dict[str, Any]) -> None:
    validate_queue_configuration()
    validate_artifact_configuration()
    repository = configured_metadata_repository()
    if repository is None:
        raise RuntimeError("ARQ worker requires PostgreSQL metadata")
    repository.initialize()
    artifact_store = configured_artifact_store()
    if artifact_store is not None:
        artifact_store.initialize()


async def shutdown(_ctx: dict[str, Any]) -> None:
    get_job_manager().shutdown()
    close_configured_artifact_store()
    close_configured_metadata_repository()


class WorkerSettings:
    functions = [execute_job_message]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = os.environ.get("DATA_AGENT_ARQ_QUEUE", "data-agent")
    max_jobs = max(1, int(os.environ.get("DATA_AGENT_ARQ_MAX_JOBS", "2")))
    job_timeout = max(60, int(os.environ.get("DATA_AGENT_ARQ_JOB_TIMEOUT", "3600")))
    max_tries = max(1, int(os.environ.get("DATA_AGENT_ARQ_MAX_TRIES", "3")))
    redis_settings = (
        RedisSettings.from_dsn(os.environ.get("DATA_AGENT_REDIS_URL", "redis://localhost:6379/0"))
        if RedisSettings is not None
        else None
    )
