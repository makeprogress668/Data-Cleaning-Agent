"""Durable task-metadata repositories.

The default file store remains available for local and single-process deployments.
Production can select PostgreSQL, where every read-modify-write operation locks one
document row in a database transaction. Uploaded files and generated artifacts are
deliberately outside this repository; they move to object storage in Phase 3C.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Protocol

DocumentMutation = Callable[[dict[str, Any] | None], dict[str, Any]]
_ADVISORY_LOCK_NAMESPACE = 1_145_132_097  # ASCII "DATA", within PostgreSQL int4.
_SCHEMA_LOCK_KEY = 1
_UNDERSTANDING_CACHE_LOCK_KEY = 2


class MetadataRepository(Protocol):
    """Atomic JSON-document storage scoped by job id and document kind."""

    def initialize(self) -> None: ...

    def read_document(self, job_id: str, kind: str) -> dict[str, Any] | None: ...

    def write_document(
        self,
        job_id: str,
        kind: str,
        document: dict[str, Any],
    ) -> dict[str, Any]: ...

    def mutate_document(
        self,
        job_id: str,
        kind: str,
        mutation: DocumentMutation,
        *,
        create: bool = False,
    ) -> dict[str, Any] | None: ...

    def list_documents(
        self,
        kind: str,
        *,
        limit: int | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def create_status_with_quota(
        self,
        job_id: str,
        document: dict[str, Any],
        *,
        tenant_id: str,
        max_active_jobs: int,
        active_statuses: set[str],
    ) -> bool: ...

    def delete_job(self, job_id: str) -> bool: ...

    def purge_jobs_before(self, cutoff_iso: str) -> set[str]: ...

    def read_understanding_cache(
        self,
        key: str,
        *,
        max_age_seconds: int = 0,
    ) -> dict[str, Any] | None: ...

    def write_understanding_cache(
        self,
        key: str,
        document: dict[str, Any],
        max_entries: int,
    ) -> None: ...

    def clear_understanding_cache(self) -> int: ...

    def understanding_cache_stats(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class InMemoryMetadataRepository:
    """Repository contract implementation used by deterministic unit tests."""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._understanding_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def initialize(self) -> None:
        return None

    def read_document(self, job_id: str, kind: str) -> dict[str, Any] | None:
        _validate_key(job_id, kind)
        with self._lock:
            document = self._documents.get((job_id, kind))
            return copy.deepcopy(document) if document is not None else None

    def write_document(
        self,
        job_id: str,
        kind: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_key(job_id, kind)
        with self._lock:
            stored = copy.deepcopy(document)
            self._documents[(job_id, kind)] = stored
            return copy.deepcopy(stored)

    def mutate_document(
        self,
        job_id: str,
        kind: str,
        mutation: DocumentMutation,
        *,
        create: bool = False,
    ) -> dict[str, Any] | None:
        _validate_key(job_id, kind)
        with self._lock:
            prior = self._documents.get((job_id, kind))
            if prior is None and not create:
                return None
            updated = mutation(copy.deepcopy(prior) if prior is not None else None)
            stored = copy.deepcopy(updated)
            self._documents[(job_id, kind)] = stored
            return copy.deepcopy(stored)

    def list_documents(
        self,
        kind: str,
        *,
        limit: int | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _validate_kind(kind)
        with self._lock:
            documents = [
                copy.deepcopy(document)
                for (_job_id, document_kind), document in self._documents.items()
                if document_kind == kind
                and (
                    tenant_id is None
                    or str(document.get("tenant_id") or "default") == tenant_id
                )
            ]
        documents.sort(key=_document_sort_key, reverse=True)
        return documents if limit is None else documents[:limit]

    def delete_job(self, job_id: str) -> bool:
        _validate_job_id(job_id)
        with self._lock:
            keys = [key for key in self._documents if key[0] == job_id]
            for key in keys:
                self._documents.pop(key, None)
        return bool(keys)

    def create_status_with_quota(
        self,
        job_id: str,
        document: dict[str, Any],
        *,
        tenant_id: str,
        max_active_jobs: int,
        active_statuses: set[str],
    ) -> bool:
        _validate_job_id(job_id)
        with self._lock:
            if (job_id, "status") in self._documents:
                return False
            active = sum(
                1
                for (_stored_job_id, kind), payload in self._documents.items()
                if kind == "status"
                and str(payload.get("tenant_id") or "default") == tenant_id
                and str(payload.get("status") or "") in active_statuses
            )
            if max_active_jobs and active >= max_active_jobs:
                return False
            self._documents[(job_id, "status")] = copy.deepcopy(document)
        return True

    def purge_jobs_before(self, cutoff_iso: str) -> set[str]:
        with self._lock:
            expired = {
                job_id
                for (job_id, kind), document in self._documents.items()
                if kind == "status"
                and str(document.get("updated_at") or document.get("created_at") or "")
                < cutoff_iso
            }
            for key in [key for key in self._documents if key[0] in expired]:
                self._documents.pop(key, None)
        return expired

    def read_understanding_cache(
        self,
        key: str,
        *,
        max_age_seconds: int = 0,
    ) -> dict[str, Any] | None:
        _validate_cache_key(key)
        with self._lock:
            entry = self._understanding_cache.get(key)
            if entry is not None and _cache_timestamp_expired(
                entry[0],
                max_age_seconds,
            ):
                return None
            return copy.deepcopy(entry[1]) if entry is not None else None

    def write_understanding_cache(
        self,
        key: str,
        document: dict[str, Any],
        max_entries: int,
    ) -> None:
        _validate_cache_key(key)
        with self._lock:
            self._understanding_cache[key] = (_utc_now(), copy.deepcopy(document))
            excess = max(0, len(self._understanding_cache) - max(1, max_entries))
            oldest = sorted(
                self._understanding_cache,
                key=lambda item: (self._understanding_cache[item][0], item),
            )[:excess]
            for cache_key in oldest:
                self._understanding_cache.pop(cache_key, None)

    def clear_understanding_cache(self) -> int:
        with self._lock:
            count = len(self._understanding_cache)
            self._understanding_cache.clear()
        return count

    def understanding_cache_stats(self) -> dict[str, Any]:
        with self._lock:
            stored = sorted(timestamp for timestamp, _ in self._understanding_cache.values())
        return {
            "entry_count": len(stored),
            "newest": stored[-1] if stored else "",
            "oldest": stored[0] if stored else "",
        }

    def close(self) -> None:
        return None


class PostgresMetadataRepository:
    """PostgreSQL-backed task metadata with transactional row-level mutation."""

    def __init__(self, dsn: str, *, pool: Any | None = None) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL 元数据存储需要 DATA_AGENT_DATABASE_URL")
        self._dsn = dsn
        self._pool = pool
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def _get_pool(self):
        if self._pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL 元数据存储未安装，请安装 production 依赖。"
                ) from exc
            max_size = max(
                2,
                int(os.environ.get("DATA_AGENT_DATABASE_POOL_SIZE", "10")),
            )
            self._pool = ConnectionPool(
                conninfo=self._dsn,
                min_size=1,
                max_size=max_size,
                open=True,
            )
        return self._pool

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            with self._get_pool().connection() as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        # IF NOT EXISTS does not serialize PostgreSQL catalog writes.
                        # Separate uvicorn processes can still create the same row type
                        # concurrently, so schema setup needs a database-wide lock.
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(%s, %s)",
                            (_ADVISORY_LOCK_NAMESPACE, _SCHEMA_LOCK_KEY),
                        )
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS data_agent_job_documents (
                                job_id VARCHAR(32) NOT NULL,
                                document_kind VARCHAR(64) NOT NULL,
                                payload JSONB NOT NULL,
                                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                PRIMARY KEY (job_id, document_kind)
                            )
                            """
                        )
                        cursor.execute(
                            """
                            CREATE INDEX IF NOT EXISTS idx_data_agent_job_status_updated
                            ON data_agent_job_documents (
                                document_kind,
                                updated_at DESC
                            )
                            WHERE document_kind = 'status'
                            """
                        )
                        cursor.execute(
                            """
                            ALTER TABLE data_agent_job_documents
                            ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)
                            GENERATED ALWAYS AS (
                                COALESCE(payload ->> 'tenant_id', 'default')
                            ) STORED
                            """
                        )
                        cursor.execute(
                            """
                            CREATE UNIQUE INDEX IF NOT EXISTS
                                idx_data_agent_tenant_job_document
                            ON data_agent_job_documents (
                                tenant_id,
                                job_id,
                                document_kind
                            )
                            """
                        )
                        cursor.execute(
                            """
                            CREATE INDEX IF NOT EXISTS idx_data_agent_status_tenant_created
                            ON data_agent_job_documents (
                                (payload ->> 'tenant_id'),
                                ((payload ->> 'created_at')) DESC
                            )
                            WHERE document_kind = 'status'
                            """
                        )
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS data_agent_understanding_cache (
                                cache_key CHAR(64) PRIMARY KEY,
                                payload JSONB NOT NULL,
                                stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                            )
                            """
                        )
                        cursor.execute(
                            """
                            CREATE INDEX IF NOT EXISTS idx_data_agent_understanding_stored
                            ON data_agent_understanding_cache (stored_at DESC)
                            """
                        )
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS data_agent_schema_migrations (
                                version INTEGER PRIMARY KEY,
                                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                            )
                            """
                        )
                        cursor.execute(
                            """
                            INSERT INTO data_agent_schema_migrations (version)
                            VALUES (1), (2), (3), (4)
                            ON CONFLICT (version) DO NOTHING
                            """
                        )
            self._initialized = True

    def read_document(self, job_id: str, kind: str) -> dict[str, Any] | None:
        _validate_key(job_id, kind)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM data_agent_job_documents
                    WHERE job_id = %s AND document_kind = %s
                    """,
                    (job_id, kind),
                )
                row = cursor.fetchone()
        return _decode_payload(row[0]) if row else None

    def write_document(
        self,
        job_id: str,
        kind: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_key(job_id, kind)
        self.initialize()
        payload = _encode_payload(document)
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO data_agent_job_documents (
                        job_id, document_kind, payload
                    ) VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (job_id, document_kind)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    (job_id, kind, payload),
                )
            connection.commit()
        return copy.deepcopy(document)

    def mutate_document(
        self,
        job_id: str,
        kind: str,
        mutation: DocumentMutation,
        *,
        create: bool = False,
    ) -> dict[str, Any] | None:
        _validate_key(job_id, kind)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    if create:
                        cursor.execute(
                            """
                            INSERT INTO data_agent_job_documents (
                                job_id, document_kind, payload
                            ) VALUES (%s, %s, '{}'::jsonb)
                            ON CONFLICT (job_id, document_kind) DO NOTHING
                            """,
                            (job_id, kind),
                        )
                    cursor.execute(
                        """
                        SELECT payload
                        FROM data_agent_job_documents
                        WHERE job_id = %s AND document_kind = %s
                        FOR UPDATE
                        """,
                        (job_id, kind),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    prior = _decode_payload(row[0])
                    updated = mutation(prior if prior else None)
                    cursor.execute(
                        """
                        UPDATE data_agent_job_documents
                        SET payload = %s::jsonb, updated_at = NOW()
                        WHERE job_id = %s AND document_kind = %s
                        """,
                        (_encode_payload(updated), job_id, kind),
                    )
        return copy.deepcopy(updated)

    def list_documents(
        self,
        kind: str,
        *,
        limit: int | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _validate_kind(kind)
        self.initialize()
        query = """
            SELECT payload
            FROM data_agent_job_documents
            WHERE document_kind = %s
            ORDER BY COALESCE(payload ->> 'created_at', '') DESC,
            job_id DESC
        """
        parameters: list[Any] = [kind]
        if tenant_id is not None:
            query = query.replace(
                "WHERE document_kind = %s",
                "WHERE document_kind = %s "
                "AND COALESCE(payload ->> 'tenant_id', 'default') = %s",
            )
            parameters.append(tenant_id)
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(max(1, limit))
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
                rows = cursor.fetchall()
        return [_decode_payload(row[0]) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        _validate_job_id(job_id)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM data_agent_job_documents WHERE job_id = %s",
                    (job_id,),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
        return deleted

    def create_status_with_quota(
        self,
        job_id: str,
        document: dict[str, Any],
        *,
        tenant_id: str,
        max_active_jobs: int,
        active_statuses: set[str],
    ) -> bool:
        """Atomically reserve one tenant job slot across API processes."""

        _validate_job_id(job_id)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"tenant-job-quota:{tenant_id}",),
                    )
                    if max_active_jobs:
                        cursor.execute(
                            """
                            SELECT COUNT(*)
                            FROM data_agent_job_documents
                            WHERE document_kind = 'status'
                              AND COALESCE(payload ->> 'tenant_id', 'default') = %s
                              AND payload ->> 'status' = ANY(%s)
                            """,
                            (tenant_id, sorted(active_statuses)),
                        )
                        row = cursor.fetchone()
                        if row and int(row[0]) >= max_active_jobs:
                            return False
                    cursor.execute(
                        """
                        INSERT INTO data_agent_job_documents (
                            job_id, document_kind, payload
                        ) VALUES (%s, 'status', %s::jsonb)
                        ON CONFLICT (job_id, document_kind) DO NOTHING
                        """,
                        (job_id, _encode_payload(document)),
                    )
                    created = cursor.rowcount > 0
        return created

    def purge_jobs_before(self, cutoff_iso: str) -> set[str]:
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH expired AS (
                        SELECT job_id
                        FROM data_agent_job_documents
                        WHERE document_kind = 'status'
                          AND updated_at < %s::timestamptz
                    ), deleted AS (
                        DELETE FROM data_agent_job_documents
                        WHERE job_id IN (SELECT job_id FROM expired)
                        RETURNING job_id
                    )
                    SELECT DISTINCT job_id FROM deleted
                    """,
                    (cutoff_iso,),
                )
                expired = {str(row[0]) for row in cursor.fetchall()}
            connection.commit()
        return expired

    def read_understanding_cache(
        self,
        key: str,
        *,
        max_age_seconds: int = 0,
    ) -> dict[str, Any] | None:
        _validate_cache_key(key)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM data_agent_understanding_cache
                    WHERE cache_key = %s
                      AND (
                        %s <= 0
                        OR stored_at >= NOW() - (%s * INTERVAL '1 second')
                      )
                    """,
                    (key, max_age_seconds, max_age_seconds),
                )
                row = cursor.fetchone()
        return _decode_payload(row[0]) if row else None

    def write_understanding_cache(
        self,
        key: str,
        document: dict[str, Any],
        max_entries: int,
    ) -> None:
        _validate_cache_key(key)
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    # Insert + bounded eviction is one cross-process critical section;
                    # otherwise concurrent writers can both observe the old size.
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (_ADVISORY_LOCK_NAMESPACE, _UNDERSTANDING_CACHE_LOCK_KEY),
                    )
                    cursor.execute(
                        """
                        INSERT INTO data_agent_understanding_cache (
                            cache_key, payload
                        ) VALUES (%s, %s::jsonb)
                        ON CONFLICT (cache_key)
                        DO UPDATE SET payload = EXCLUDED.payload, stored_at = NOW()
                        """,
                        (key, _encode_payload(document)),
                    )
                    cursor.execute(
                        """
                        DELETE FROM data_agent_understanding_cache
                        WHERE cache_key IN (
                            SELECT cache_key
                            FROM data_agent_understanding_cache
                            ORDER BY stored_at DESC, cache_key DESC
                            OFFSET %s
                        )
                        """,
                        (max(1, max_entries),),
                    )

    def clear_understanding_cache(self) -> int:
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM data_agent_understanding_cache")
                deleted = cursor.rowcount
            connection.commit()
        return max(0, deleted)

    def understanding_cache_stats(self) -> dict[str, Any]:
        self.initialize()
        with self._get_pool().connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*), MAX(stored_at), MIN(stored_at)
                    FROM data_agent_understanding_cache
                    """
                )
                row = cursor.fetchone()
        count, newest, oldest = row if row else (0, None, None)
        return {
            "entry_count": int(count or 0),
            "newest": _timestamp_text(newest),
            "oldest": _timestamp_text(oldest),
        }

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


def metadata_backend() -> str:
    backend = os.environ.get("DATA_AGENT_METADATA_BACKEND", "file").strip().lower()
    if backend not in {"file", "postgres"}:
        raise RuntimeError(
            "DATA_AGENT_METADATA_BACKEND 仅支持 file 或 postgres"
        )
    return backend


@lru_cache(maxsize=8)
def _postgres_repository(dsn: str) -> PostgresMetadataRepository:
    return PostgresMetadataRepository(dsn)


def configured_metadata_repository() -> MetadataRepository | None:
    """Return the selected external repository; ``None`` means legacy file mode."""

    if metadata_backend() == "file":
        return None
    dsn = os.environ.get("DATA_AGENT_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "启用 PostgreSQL 元数据存储时必须设置 DATA_AGENT_DATABASE_URL"
        )
    return _postgres_repository(dsn)


def close_configured_metadata_repository() -> None:
    repository = configured_metadata_repository()
    if repository is not None:
        repository.close()
    _postgres_repository.cache_clear()


def _validate_key(job_id: str, kind: str) -> None:
    _validate_job_id(job_id)
    _validate_kind(kind)


def _validate_job_id(job_id: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("invalid job id")


def _validate_kind(kind: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
        raise ValueError("invalid metadata document kind")


def _validate_cache_key(key: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("invalid understanding cache key")


def _encode_payload(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _decode_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    if isinstance(payload, str):
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _cache_timestamp_expired(stored_at: str, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return False
    try:
        timestamp = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - timestamp).total_seconds() > max_age_seconds


def _document_sort_key(document: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(document.get("created_at") or ""),
        str(document.get("updated_at") or ""),
        str(document.get("job_id") or ""),
    )
