"""Phase 3C object-store durability and local materialisation contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

import data_agent.api.artifact_service as artifact_service
import data_agent.api.artifact_store as artifact_store_module
import data_agent.api.job_store as job_store
from data_agent.api.artifact_service import (
    materialize_job_inputs,
    materialize_named_artifact,
    persist_job_inputs,
    publish_job_artifacts,
)
from data_agent.api.artifact_store import S3ArtifactStore, artifact_key

_JOB_ID = "8" * 32


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def initialize(self) -> None:
        return None

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        self.objects[key] = source.read_bytes()

    def get_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[key])

    def delete_prefix(self, prefix: str) -> int:
        matches = [key for key in self.objects if key.startswith(prefix.rstrip("/") + "/")]
        for key in matches:
            self.objects.pop(key)
        return len(matches)

    def close(self) -> None:
        return None


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MemoryArtifactStore:
    store = MemoryArtifactStore()
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(artifact_service, "configured_artifact_store", lambda: store)
    return store


def test_inputs_survive_cross_process_local_cache_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _runtime(tmp_path, monkeypatch)
    upload = job_store.job_dir(_JOB_ID) / "uploads" / "订单.csv"
    upload.parent.mkdir(parents=True)
    upload.write_text("订单号,金额\nA1,10\n", encoding="utf-8")

    references = persist_job_inputs(_JOB_ID, [upload])
    upload.unlink()
    restored = materialize_job_inputs(_JOB_ID, references)

    assert restored == [upload]
    assert upload.read_text(encoding="utf-8") == "订单号,金额\nA1,10\n"
    assert references[0]["key"] in store.objects
    assert len(references[0]["sha256"]) == 64


def test_output_manifest_excludes_uploads_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _runtime(tmp_path, monkeypatch)
    root = job_store.job_dir(_JOB_ID)
    output = root / "output" / "final_result.xlsx"
    snapshot = root / "_internal" / "result_snapshot.xlsx"
    upload = root / "uploads" / "source.csv"
    for path, content in (
        (output, b"result"),
        (snapshot, b"snapshot"),
        (upload, b"source"),
        (root / "job_status.json", b"{}"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    manifest = publish_job_artifacts(_JOB_ID)
    relative_paths = {item["relative_path"] for item in manifest}

    assert relative_paths == {
        "_internal/result_snapshot.xlsx",
        "output/final_result.xlsx",
    }
    output.unlink()
    restored = materialize_named_artifact(
        _JOB_ID,
        "final_result.xlsx",
        payload={"artifact_manifest": manifest},
    )
    assert restored == output
    assert restored.read_bytes() == b"result"
    assert len(store.objects) == 2


def test_status_transitions_preserve_storage_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "jobs"))
    reference = {
        "key": artifact_key(_JOB_ID, "uploads/a.csv"),
        "relative_path": "uploads/a.csv",
    }
    job_store.write_job_status(
        _JOB_ID,
        job_store.STATUS_PENDING,
        payload={"input_artifacts": [reference]},
        config={},
    )

    changed, status = job_store.transition_job_status(
        _JOB_ID,
        job_store.STATUS_PENDING,
        job_store.STATUS_RUNNING,
        payload={"goal_plan": {"summary": "plan"}},
        config={},
    )

    assert changed is True
    assert status["input_artifacts"] == [reference]


def test_s3_adapter_checks_bucket_and_deletes_every_page(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def head_bucket(self, **kwargs):  # noqa: ANN003
            calls.append(("head", kwargs))

        def upload_file(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls.append(("upload", {"args": args, **kwargs}))

        def download_file(self, bucket, key, destination):  # noqa: ANN001
            calls.append(("download", {"bucket": bucket, "key": key}))
            Path(destination).write_bytes(b"downloaded")

        def list_objects_v2(self, **kwargs):  # noqa: ANN003
            calls.append(("list", kwargs))
            if "ContinuationToken" not in kwargs:
                return {
                    "Contents": [{"Key": "jobs/id/a"}],
                    "IsTruncated": True,
                    "NextContinuationToken": "next",
                }
            return {"Contents": [{"Key": "jobs/id/b"}], "IsTruncated": False}

        def delete_objects(self, **kwargs):  # noqa: ANN003
            calls.append(("delete", kwargs))

        def close(self):
            calls.append(("close", {}))

    source = tmp_path / "source.txt"
    destination = tmp_path / "download.txt"
    source.write_text("hello", encoding="utf-8")
    store = S3ArtifactStore(bucket="agent", client=FakeClient())

    store.initialize()
    store.put_file("jobs/id/source.txt", source, content_type="text/plain")
    store.get_file("jobs/id/source.txt", destination)
    assert store.delete_prefix("jobs/id") == 2
    store.close()

    assert destination.read_bytes() == b"downloaded"
    assert [name for name, _kwargs in calls].count("list") == 2
    assert [name for name, _kwargs in calls].count("delete") == 2


def test_artifact_configuration_and_key_namespace_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_ARTIFACT_BACKEND", "s3")
    monkeypatch.delenv("DATA_AGENT_ARTIFACT_BUCKET", raising=False)
    artifact_store_module.configured_artifact_store.cache_clear()
    with pytest.raises(RuntimeError, match="DATA_AGENT_ARTIFACT_BUCKET"):
        artifact_store_module.configured_artifact_store()

    monkeypatch.setenv("DATA_AGENT_ARTIFACT_PREFIX", "tenant-a/jobs")
    assert artifact_key(_JOB_ID, "output/final.xlsx").startswith("tenant-a/jobs/")
    with pytest.raises(ValueError, match="inside its job namespace"):
        artifact_key(_JOB_ID, "../other/file")
    artifact_store_module.configured_artifact_store.cache_clear()
