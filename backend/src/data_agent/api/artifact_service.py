"""Map durable object references onto the local paths required by executors."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from data_agent.api.artifact_store import artifact_key, configured_artifact_store
from data_agent.api.job_store import job_dir, read_job_status

_METADATA_FILES = frozenset(
    {
        "job_status.json",
        "task_session.json",
        "plan_state.json",
        "plan_snapshot.json",
        "review_state.json",
        "clarification_answers.json",
    }
)


def persist_job_inputs(
    job_id: str,
    paths: list[Path],
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Upload input files before queue dispatch and return immutable references."""

    store = configured_artifact_store()
    if store is None:
        return []
    references = []
    for path in paths:
        relative = Path("uploads") / path.name
        reference = _reference(job_id, path, relative, tenant_id=tenant_id)
        store.put_file(
            reference["key"],
            path,
            content_type=reference["content_type"],
        )
        references.append(reference)
    return references


def materialize_job_inputs(
    job_id: str,
    references: list[dict[str, Any]] | None = None,
) -> list[Path]:
    """Download inputs into this process' private job workspace when necessary."""

    if references is None:
        payload = read_job_status(job_id)
        raw = payload.get("input_artifacts")
        references = list(raw) if isinstance(raw, list) else []
    store = configured_artifact_store()
    paths = []
    for reference in references:
        relative = _safe_relative(reference.get("relative_path"))
        destination = job_dir(job_id) / relative
        if not destination.exists() and store is not None:
            store.get_file(str(reference.get("key") or ""), destination)
        if destination.exists():
            paths.append(destination)
    return paths


def publish_job_artifacts(job_id: str) -> list[dict[str, Any]]:
    """Upload every execution artifact needed for delivery or later review."""

    store = configured_artifact_store()
    root = job_dir(job_id)
    if store is None or not root.exists():
        return []
    payload = read_job_status(job_id)
    tenant_id = str(payload.get("tenant_id") or "") or None
    manifest = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] == "uploads" or (
            len(relative.parts) == 1 and relative.name in _METADATA_FILES
        ):
            continue
        reference = _reference(job_id, path, relative, tenant_id=tenant_id)
        store.put_file(
            reference["key"],
            path,
            content_type=reference["content_type"],
        )
        manifest.append(reference)
    return manifest


def publish_artifact(job_id: str, path: Path) -> dict[str, Any] | None:
    store = configured_artifact_store()
    if store is None:
        return None
    root = job_dir(job_id).resolve()
    resolved = path.resolve()
    relative = resolved.relative_to(root)
    payload = read_job_status(job_id)
    reference = _reference(
        job_id,
        resolved,
        relative,
        tenant_id=str(payload.get("tenant_id") or "") or None,
    )
    store.put_file(reference["key"], resolved, content_type=reference["content_type"])
    return reference


def materialize_relative_artifact(
    job_id: str,
    relative_path: str | Path,
    *,
    payload: dict[str, Any] | None = None,
) -> Path:
    relative = _safe_relative(relative_path)
    destination = job_dir(job_id) / relative
    if destination.exists():
        return destination
    store = configured_artifact_store()
    if store is None:
        return destination
    document = payload or read_job_status(job_id)
    reference = _manifest_by_relative(document).get(relative.as_posix())
    if reference is not None:
        store.get_file(str(reference.get("key") or ""), destination)
    return destination


def materialize_named_artifact(
    job_id: str,
    file_name: str,
    *,
    payload: dict[str, Any] | None = None,
) -> Path | None:
    safe_name = Path(file_name).name
    if safe_name != file_name:
        return None
    document = payload or read_job_status(job_id)
    matches = [
        item
        for item in _manifest(document)
        if Path(str(item.get("relative_path") or "")).name == safe_name
    ]
    if len(matches) != 1:
        return None
    return materialize_relative_artifact(
        job_id,
        str(matches[0]["relative_path"]),
        payload=document,
    )


def has_relative_artifact(
    job_id: str,
    relative_path: str | Path,
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    relative = _safe_relative(relative_path)
    if (job_dir(job_id) / relative).exists():
        return True
    document = payload or read_job_status(job_id)
    return relative.as_posix() in _manifest_by_relative(document)


def _manifest(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("artifact_manifest")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _manifest_by_relative(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("relative_path") or ""): item
        for item in _manifest(payload)
        if item.get("relative_path")
    }


def _reference(
    job_id: str,
    path: Path,
    relative: Path,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    return {
        "key": artifact_key(job_id, relative, tenant_id=tenant_id),
        "relative_path": relative.as_posix(),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: object) -> Path:
    path = Path(str(value or ""))
    if not str(value or "") or path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact path must be relative to the job directory")
    return path
