"""One-way, idempotent migration from local JSON metadata to a repository."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from data_agent.api.metadata_repository import MetadataRepository

_DOCUMENT_FILES = {
    "job_status.json": "status",
    "task_session.json": "task_session",
    "review_state.json": "review_state",
    "plan_state.json": "plan_state",
    "plan_snapshot.json": "plan_snapshot",
    "clarification_answers.json": "clarification_answers",
}


@dataclass(frozen=True)
class MetadataMigrationResult:
    jobs_seen: int = 0
    documents_migrated: int = 0
    documents_skipped: int = 0
    documents_invalid: int = 0


def migrate_file_metadata(
    root: Path,
    repository: MetadataRepository,
) -> MetadataMigrationResult:
    """Copy valid legacy documents that do not already exist in the repository.

    Source files are never changed or deleted. Existing repository documents win so
    a later restart cannot overwrite newer PostgreSQL state with a stale local copy.
    """

    if not root.exists():
        return MetadataMigrationResult()

    jobs_seen = 0
    migrated = 0
    skipped = 0
    invalid = 0
    for directory in root.iterdir():
        if not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
            continue
        jobs_seen += 1
        for file_name, kind in _DOCUMENT_FILES.items():
            path = directory / file_name
            if not path.is_file():
                continue
            if repository.read_document(directory.name, kind) is not None:
                skipped += 1
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                invalid += 1
                continue
            if not isinstance(payload, dict):
                invalid += 1
                continue
            repository.write_document(directory.name, kind, payload)
            migrated += 1

    return MetadataMigrationResult(
        jobs_seen=jobs_seen,
        documents_migrated=migrated,
        documents_skipped=skipped,
        documents_invalid=invalid,
    )
