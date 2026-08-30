"""Persistent queue adapter for API-to-worker task dispatch."""

from __future__ import annotations

import inspect
import os
from typing import Any, Literal, TypedDict

QueueAction = Literal["run", "resume", "confirm"]


class QueueMessage(TypedDict):
    dispatch_id: str
    action: QueueAction
    job_id: str
    payload: dict[str, Any]


def queue_backend() -> str:
    backend = os.environ.get("DATA_AGENT_JOB_QUEUE_BACKEND", "thread").strip().lower()
    if backend not in {"thread", "arq"}:
        raise RuntimeError("DATA_AGENT_JOB_QUEUE_BACKEND 仅支持 thread 或 arq")
    return backend


def durable_queue_enabled() -> bool:
    return queue_backend() == "arq"


def validate_queue_configuration() -> None:
    if not durable_queue_enabled():
        return
    if os.environ.get("DATA_AGENT_METADATA_BACKEND", "file").strip().lower() != "postgres":
        raise RuntimeError("ARQ 持久队列必须与 PostgreSQL 元数据存储同时启用")
    if not os.environ.get("DATA_AGENT_REDIS_URL", "").strip():
        raise RuntimeError("启用 ARQ 持久队列时必须设置 DATA_AGENT_REDIS_URL")


async def enqueue_message(message: QueueMessage) -> None:
    """Enqueue one idempotently identified message in ARQ."""

    validate_queue_configuration()
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
    except ImportError as exc:
        raise RuntimeError("ARQ 持久队列未安装，请安装 production 依赖。") from exc

    redis = await create_pool(
        RedisSettings.from_dsn(os.environ["DATA_AGENT_REDIS_URL"]),
    )
    try:
        # A status document carries one stable dispatch id. Retrying an API request or
        # startup reconciliation therefore cannot enqueue two logical copies.
        await redis.enqueue_job(
            "execute_job_message",
            dict(message),
            _job_id=message["dispatch_id"],
            _queue_name=os.environ.get("DATA_AGENT_ARQ_QUEUE", "data-agent"),
        )
    finally:
        close = getattr(redis, "aclose", None) or getattr(redis, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
