"""S3-compatible storage boundary for uploaded files and generated artifacts."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol

from data_agent.tenancy import current_tenant_id, validate_tenant_id


class ArtifactStore(Protocol):
    """Minimal object-store operations required by the job lifecycle."""

    def initialize(self) -> None: ...

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None: ...

    def get_file(self, key: str, destination: Path) -> None: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def close(self) -> None: ...


class S3ArtifactStore:
    """Boto3-backed store compatible with AWS S3, MinIO and similar services."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        client=None,
    ) -> None:
        if not bucket.strip():
            raise RuntimeError("S3 产物存储必须设置 DATA_AGENT_ARTIFACT_BUCKET")
        self.bucket = bucket.strip()
        if client is not None:
            self._client = client
            return
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("S3 产物存储未安装，请安装 production 依赖。") from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def initialize(self) -> None:
        self._client.head_bucket(Bucket=self.bucket)

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        kwargs = {"ExtraArgs": extra} if extra else {}
        self._client.upload_file(str(source), self.bucket, _validated_key(key), **kwargs)

    def get_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.downloading")
        try:
            self._client.download_file(self.bucket, _validated_key(key), str(temporary))
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        safe_prefix = _validated_key(prefix).rstrip("/") + "/"
        deleted = 0
        continuation: str | None = None
        while True:
            request = {"Bucket": self.bucket, "Prefix": safe_prefix, "MaxKeys": 1000}
            if continuation:
                request["ContinuationToken"] = continuation
            page = self._client.list_objects_v2(**request)
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                deleted += len(objects)
            if not page.get("IsTruncated"):
                break
            continuation = str(page.get("NextContinuationToken") or "")
            if not continuation:
                break
        return deleted

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def artifact_backend() -> str:
    backend = os.environ.get("DATA_AGENT_ARTIFACT_BACKEND", "local").strip().lower()
    if backend not in {"local", "s3"}:
        raise RuntimeError("DATA_AGENT_ARTIFACT_BACKEND 仅支持 local 或 s3")
    return backend


def object_storage_enabled() -> bool:
    return artifact_backend() == "s3"


def validate_artifact_configuration() -> None:
    if not object_storage_enabled():
        return
    if not os.environ.get("DATA_AGENT_ARTIFACT_BUCKET", "").strip():
        raise RuntimeError("启用 S3 产物存储时必须设置 DATA_AGENT_ARTIFACT_BUCKET")


@lru_cache(maxsize=1)
def configured_artifact_store() -> ArtifactStore | None:
    validate_artifact_configuration()
    if not object_storage_enabled():
        return None
    return S3ArtifactStore(
        bucket=os.environ["DATA_AGENT_ARTIFACT_BUCKET"],
        endpoint_url=os.environ.get("DATA_AGENT_S3_ENDPOINT_URL"),
        region=os.environ.get("DATA_AGENT_S3_REGION"),
        access_key=os.environ.get("DATA_AGENT_S3_ACCESS_KEY"),
        secret_key=os.environ.get("DATA_AGENT_S3_SECRET_KEY"),
    )


def close_configured_artifact_store() -> None:
    store = configured_artifact_store()
    if store is not None:
        store.close()
    configured_artifact_store.cache_clear()


def artifact_key(
    job_id: str,
    relative_path: str | Path = "",
    *,
    tenant_id: str | None = None,
) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("invalid job id for artifact key")
    prefix = os.environ.get("DATA_AGENT_ARTIFACT_PREFIX", "jobs").strip().strip("/")
    relative = PurePosixPath(str(relative_path).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path must stay inside its job namespace")
    tenant = validate_tenant_id(tenant_id or current_tenant_id())
    parts = [
        part
        for part in (prefix, "tenants", tenant, job_id, relative.as_posix())
        if part and part != "."
    ]
    return _validated_key("/".join(parts))


def _validated_key(key: str) -> str:
    normalized = key.strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid object-store key")
    return path.as_posix()
