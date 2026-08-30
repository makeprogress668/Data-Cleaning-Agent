from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from data_agent.api.artifact_service import (
    materialize_relative_artifact,
    persist_job_inputs,
    publish_job_artifacts,
)
from data_agent.api.artifact_store import (
    close_configured_artifact_store,
    configured_artifact_store,
    validate_artifact_configuration,
)
from data_agent.api.connector_routes import router as connector_router
from data_agent.api.job_manager import get_job_manager, redispatch_pending_messages
from data_agent.api.job_queue import durable_queue_enabled, validate_queue_configuration
from data_agent.api.job_store import (
    STATUS_SUCCEEDED,
    admit_job_status,
    api_work_dir,
    delete_job,
    public_job_payload,
    purge_expired_jobs,
    read_job_status,
    reconcile_interrupted_jobs,
    write_job_status,
)
from data_agent.api.metadata_migration import migrate_file_metadata
from data_agent.api.metadata_repository import (
    close_configured_metadata_repository,
    configured_metadata_repository,
)
from data_agent.api.middleware import (
    allowed_origins,
    install_middlewares,
    validate_auth_configuration,
)
from data_agent.api.quotas import (
    enforce_batch_upload_bytes,
    enforce_tenant_admission,
    enforce_tenant_storage_growth,
    quota_projection,
)
from data_agent.api.recipe_routes import router as recipe_router
from data_agent.api.recipe_store import read_recipe, recipe_processing_config
from data_agent.api.routes import router as job_router
from data_agent.api.semantic_routes import router as semantic_router
from data_agent.observability import capture_llm_usage
from data_agent.semantic_layer import read_semantic_model
from data_agent.services import process_input_paths_with_reflection
from data_agent.tenancy import request_tenant_id, tenant_scope
from data_agent.tools.document_rules import DOCUMENT_EXTENSIONS
from data_agent.tools.excel_reader import SUPPORTED_EXTENSIONS
from data_agent.utils.errors import to_user_message

logger = logging.getLogger("data_agent.api")

PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Per-file upload size limit (default 100 MB) to avoid unbounded memory use / DoS.
MAX_UPLOAD_BYTES = int(os.environ.get("DATA_AGENT_MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
_UPLOAD_CHUNK_BYTES = 1024 * 1024
# Delete job directories older than this many seconds on startup (0 disables).
_JOB_TTL_SECONDS = int(os.environ.get("DATA_AGENT_JOB_TTL_SECONDS", str(7 * 24 * 3600)))
DEFAULT_MAX_UPLOAD_FILES = 50
_TENANT_ADMISSION_LOCKS: dict[str, asyncio.Lock] = {}

# Characters that are unsafe in a file name on Windows or POSIX. Everything else,
# including CJK, is preserved on purpose — see :func:`_safe_file_name`.
_UNSAFE_FILE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("DATA_AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_configure_logging()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    validate_auth_configuration()
    durable_queue = durable_queue_enabled()
    validate_queue_configuration()
    validate_artifact_configuration()
    if int(os.environ.get("DATA_AGENT_API_WORKERS", "1")) != 1 and not durable_queue:
        raise RuntimeError(
            "DATA_AGENT_API_WORKERS must remain 1 until the ARQ durable queue is enabled"
        )
    metadata_repository = configured_metadata_repository()
    if metadata_repository is not None:
        # Fail startup before accepting traffic if the configured durable store is
        # unreachable or its schema cannot be prepared.
        metadata_repository.initialize()
        auto_migrate = os.environ.get(
            "DATA_AGENT_METADATA_AUTO_MIGRATE",
            "1",
        ).strip().lower() not in {"0", "false", "no", "off"}
        if auto_migrate:
            migration = migrate_file_metadata(api_work_dir(), metadata_repository)
            if migration.documents_migrated or migration.documents_invalid:
                logger.info(
                    "metadata migration: jobs=%d migrated=%d skipped=%d invalid=%d",
                    migration.jobs_seen,
                    migration.documents_migrated,
                    migration.documents_skipped,
                    migration.documents_invalid,
                )
    artifact_store = configured_artifact_store()
    if artifact_store is not None:
        # Object storage is part of the delivery commit. Fail startup instead of
        # accepting jobs that can execute but cannot persist their inputs/results.
        artifact_store.initialize()
    removed = purge_expired_jobs(_JOB_TTL_SECONDS)
    if removed:
        logger.info("startup cleanup: removed %d expired job(s)", removed)
    reconciled = reconcile_interrupted_jobs()
    if reconciled:
        logger.warning("startup recovery: marked %d interrupted job(s) retryable", reconciled)
    dispatcher_task: asyncio.Task | None = None
    if durable_queue:
        dispatched = await redispatch_pending_messages()
        if dispatched:
            logger.info("startup queue recovery: dispatched %d pending job(s)", dispatched)

        async def dispatch_outbox() -> None:
            interval = max(
                5,
                int(os.environ.get("DATA_AGENT_OUTBOX_INTERVAL_SECONDS", "30")),
            )
            while True:
                await asyncio.sleep(interval)
                await redispatch_pending_messages()

        dispatcher_task = asyncio.create_task(
            dispatch_outbox(),
            name="data-agent-outbox",
        )
    try:
        yield
    finally:
        if dispatcher_task is not None:
            dispatcher_task.cancel()
            try:
                await dispatcher_task
            except asyncio.CancelledError:
                pass
        get_job_manager().shutdown()
        close_configured_artifact_store()
        close_configured_metadata_repository()


app = FastAPI(
    title="Data Cleaning Agent API",
    version="1.0.0",
    description="上传 Excel/CSV 文件，异步执行数据清洗并返回业务结果。",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
install_middlewares(app)
app.include_router(job_router)
app.include_router(recipe_router)
app.include_router(semantic_router)
app.include_router(connector_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/config")
def get_runtime_config(request: Request) -> dict:
    """Expose only the business-facing settings needed by the console."""
    from data_agent.agent.data_context import load_data_access_mode

    access_mode = load_data_access_mode().value
    protection = {
        "metadata_only": (
            "strict",
            "严格保护",
            "仅使用表名和字段信息，不读取单元格内容。",
        ),
        "trusted_samples": (
            "enhanced",
            "增强识别",
            "在明确受信任的环境中，可读取少量必要内容以提高识别准确度。",
        ),
    }.get(
        access_mode,
        (
            "standard",
            "标准保护",
            "只读取完成目标所需的少量内容，并隐藏可能敏感的信息。",
        ),
    )
    allowed_extensions = sorted(SUPPORTED_EXTENSIONS | DOCUMENT_EXTENSIONS)
    return {
        "service": {"available": True},
        # Configuration state only. Probing a paid provider on every settings load
        # would be both expensive and misleadingly transient; each job reports its
        # actual understanding_source and LLM usage separately.
        "understanding": _understanding_status(),
        "data_protection": {
            "level": protection[0],
            "label": protection[1],
            "description": protection[2],
        },
        "upload": {
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_upload_mb": round(MAX_UPLOAD_BYTES / (1024 * 1024)),
            "allowed_extensions": allowed_extensions,
        },
        "quota": quota_projection(request_tenant_id(request)),
    }


@app.post("/api/v1/cleaning/process")
async def process_cleaning_files(
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
    config: Annotated[Optional[str], Form()] = None,
) -> dict:
    """Synchronous processing endpoint (kept for simple integrations/tests).

    Heavy work is pushed to a thread so it never blocks the event loop, but the
    caller still gets the full result in the response. The console uses the async
    ``/api/v1/jobs`` endpoint instead.
    """
    _validate_upload_batch(files)
    tenant_id = request_tenant_id(request)
    job_id = uuid.uuid4().hex
    job_dir = api_work_dir() / job_id
    upload_dir = job_dir / "uploads"
    parsed_config = _parse_config(config)
    parsed_config["_tenant_id"] = tenant_id
    async with _tenant_admission_lock(tenant_id):
        usage = enforce_tenant_admission(tenant_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved_paths = await _save_uploads(files, upload_dir)
            enforce_tenant_storage_growth(
                tenant_id,
                sum(path.stat().st_size for path in saved_paths),
                current_stored_bytes=usage["stored_bytes"],
            )
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        try:
            input_artifacts = await asyncio.to_thread(
                persist_job_inputs,
                job_id,
                saved_paths,
                tenant_id=tenant_id,
            )
            admit_job_status(
                job_id,
                "running",
                config=parsed_config,
                max_active_jobs=quota_projection(tenant_id)["max_active_jobs"] or 0,
            )
            write_job_status(
                job_id,
                "running",
                payload={"input_artifacts": input_artifacts},
                config=parsed_config,
            )
        except Exception:
            delete_job(job_id)
            raise

    # This endpoint cannot pause for approval, so any plan escalated by the layered
    # risk policy is refused unless the caller opted in. The old field name remains
    # API-compatible; /api/v1/jobs can show the exact reasons before confirmation.
    confirm_destructive = bool(parsed_config.pop("confirm_destructive", False))

    def run_observed() -> dict:
        with tenant_scope(tenant_id), capture_llm_usage() as usage:
            result = process_input_paths_with_reflection(
                saved_paths,
                work_dir=job_dir,
                config=parsed_config,
                confirm_destructive=confirm_destructive,
            )
            result["llm_usage"] = list(usage)
            return result

    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            run_observed,
        )
        artifact_manifest = await asyncio.to_thread(publish_job_artifacts, job_id)
        write_job_status(
            job_id,
            STATUS_SUCCEEDED,
            payload={
                **response,
                "input_artifacts": input_artifacts,
                "artifact_manifest": artifact_manifest,
            },
            config=parsed_config,
        )
    except ValueError as exc:
        write_job_status(
            job_id,
            "failed",
            payload={"input_artifacts": input_artifacts},
            config=parsed_config,
            error=to_user_message(exc),
        )
        raise HTTPException(status_code=400, detail=to_user_message(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("job %s failed", job_id)
        write_job_status(
            job_id,
            "failed",
            payload={"input_artifacts": input_artifacts},
            config=parsed_config,
            error=to_user_message(exc),
        )
        raise HTTPException(status_code=500, detail=to_user_message(exc)) from exc

    response["job_id"] = job_id
    response["status"] = "success"
    response["files"]["download_url"] = f"/api/v1/cleaning/jobs/{job_id}/result.xlsx"
    return response


@app.post("/api/v1/jobs", status_code=202)
async def create_job(
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
    goal: Annotated[Optional[str], Form()] = None,
    mode: Annotated[str, Form()] = "answer",
    config: Annotated[Optional[str], Form()] = None,
    recipe_id: Annotated[Optional[str], Form()] = None,
    semantic_model_id: Annotated[Optional[str], Form()] = None,
) -> dict:
    """Create an async job: save uploads, queue background execution, return job_id.

    Returns 202 immediately with status ``pending``; the console polls
    ``/api/v1/jobs/{id}/status`` until a terminal state.
    """
    _validate_upload_batch(files)
    if mode not in {"discover", "plan", "answer"}:
        raise HTTPException(status_code=400, detail="mode 必须是 discover、plan 或 answer。")

    parsed_config = _parse_config(config)
    tenant_id = request_tenant_id(request)
    parsed_config["_tenant_id"] = tenant_id
    parsed_config.setdefault("job_overrides", {})
    if recipe_id:
        recipe = read_recipe(recipe_id, tenant_id=tenant_id)
        recipe_config = recipe_processing_config(recipe)
        recipe_goal = str(recipe_config["goal"])
        if goal and goal.strip() != recipe_goal:
            raise HTTPException(
                status_code=400,
                detail="Recipe 已绑定经过验收的业务目标；如需修改目标，请创建新任务。",
            )
        parsed_config.update(recipe_config)
        goal = recipe_goal
    if semantic_model_id:
        if recipe_id:
            raise HTTPException(
                status_code=400,
                detail="Recipe 与业务语义模型不能在同一次任务中同时指定。",
            )
        parsed_config["semantic_model_id"] = semantic_model_id
        parsed_config["semantic_model"] = read_semantic_model(
            semantic_model_id,
            tenant_id=tenant_id,
        )
    if goal:
        parsed_config["job_overrides"]["goal"] = goal
    parsed_config["job_overrides"]["mode"] = mode
    # The console reads discovery/plan back from job_status.json, so always capture
    # them for jobs created through this endpoint regardless of the caller config.
    parsed_config.setdefault("include_diagnostics", True)

    manager = get_job_manager()
    job_id = manager.new_job_id()
    job_dir = api_work_dir() / job_id
    upload_dir = job_dir / "uploads"
    async with _tenant_admission_lock(tenant_id):
        usage = enforce_tenant_admission(tenant_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved_paths = await _save_uploads(files, upload_dir)
            enforce_tenant_storage_growth(
                tenant_id,
                sum(path.stat().st_size for path in saved_paths),
                current_stored_bytes=usage["stored_bytes"],
            )
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        try:
            await manager.submit(job_id, saved_paths, parsed_config)
        except Exception:
            delete_job(job_id)
            raise
    return {
        "job_id": job_id,
        "status": "pending",
        "files": {"download_url": f"/api/v1/cleaning/jobs/{job_id}/result.xlsx"},
    }


@app.get("/api/v1/cleaning/jobs/{job_id}")
def get_job_status(job_id: str) -> dict:
    return public_job_payload(read_job_status(job_id))


@app.get("/api/v1/cleaning/jobs/{job_id}/result.xlsx")
def download_result(job_id: str) -> FileResponse:
    payload = read_job_status(job_id)
    output_file = materialize_relative_artifact(
        job_id,
        Path("output") / "final_result.xlsx",
        payload=payload,
    )
    if not output_file.exists():
        raise HTTPException(status_code=404, detail="结果文件尚未生成或已过期。")

    return FileResponse(
        output_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"cleaning_result_{job_id}.xlsx",
    )


def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return api_work_dir() / job_id


def _understanding_status() -> dict:
    """Report the goal-understanding mode without ever exposing the key."""

    from data_agent.agent.llm_client import load_llm_config

    config = load_llm_config()
    if config is None:
        return {
            "mode": "deterministic",
            "state": "disabled",
            "label": "规则引擎",
            "description": "未配置或未启用大模型，按关键词与数据画像理解目标，结果稳定可复现。",
        }
    return {
        "mode": "llm",
        "state": "configured",
        "label": "语义理解已配置",
        "model": config.model,
        "description": (
            f"已配置 {config.model}；此处不发起付费在线探测。"
            "每个任务会显示实际使用模型、缓存还是规则回退。"
        ),
    }


def _validate_upload_batch(files: list[UploadFile]) -> None:
    """Reject empty or oversized upload batches before anything touches disk.

    The per-file size cap alone still allowed an unbounded *number* of files in one
    request, so a single call could fill the job directory.
    """

    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件。")
    limit = _max_upload_files()
    if len(files) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {limit} 个文件，当前 {len(files)} 个。",
        )


def _max_upload_files() -> int:
    """Resolve the batch cap at call time so deployments can tune it per process."""

    raw = os.environ.get("DATA_AGENT_MAX_UPLOAD_FILES", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_UPLOAD_FILES
    except ValueError:
        return DEFAULT_MAX_UPLOAD_FILES


def _parse_config(config: Optional[str]) -> dict:
    if not config:
        return {}
    try:
        payload = json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="config 必须是合法的 JSON。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象。")
    return payload


async def _save_upload(upload: UploadFile, upload_dir: Path) -> Path:
    original_name = Path(upload.filename or "").name
    if not original_name:
        raise HTTPException(status_code=400, detail="上传文件名为空。")
    suffix = Path(original_name).suffix.lower()
    allowed_extensions = SUPPORTED_EXTENSIONS | DOCUMENT_EXTENSIONS
    if suffix not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{suffix or '（无扩展名）'}。支持的类型：{allowed}",
        )

    file_name = _safe_file_name(original_name)
    target = _unique_path(upload_dir / file_name)
    # Stream to disk in chunks with a hard size cap, instead of reading the whole
    # upload into memory (which was an unbounded memory / DoS risk).
    total = 0
    with target.open("wb") as buffer:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                buffer.close()
                target.unlink(missing_ok=True)
                limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
                raise HTTPException(
                    status_code=400,
                    detail=f"文件 '{original_name}' 超过 {limit_mb:.0f} MB 上传上限。",
                )
            buffer.write(chunk)
    return target


async def _save_uploads(files: list[UploadFile], upload_dir: Path) -> list[Path]:
    saved_paths: list[Path] = []
    total_bytes = 0
    for upload in files:
        path = await _save_upload(upload, upload_dir)
        saved_paths.append(path)
        total_bytes += path.stat().st_size
        enforce_batch_upload_bytes(total_bytes)
    return saved_paths


def _tenant_admission_lock(tenant_id: str) -> asyncio.Lock:
    lock = _TENANT_ADMISSION_LOCKS.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _TENANT_ADMISSION_LOCKS[tenant_id] = lock
    return lock


def _safe_file_name(file_name: str) -> str:
    """Make an uploaded name safe to write **without destroying its meaning**.

    The table name comes from the file stem (``excel_reader.read_tables_from_paths``),
    and that name drives base-table selection, goal understanding and every
    user-facing table label. An ASCII whitelist would collapse ``订单明细.xlsx`` into
    ``upload.xlsx``, so we strip only what is genuinely unsafe — path separators,
    control characters and the Windows-reserved punctuation — and keep the rest.
    """

    path = Path(file_name)
    suffix = path.suffix.lower()
    stem = _UNSAFE_FILE_NAME_CHARS.sub("_", path.stem)
    # Leading dots hide the file; trailing dots/spaces are illegal on Windows.
    stem = stem.strip(". \t")
    if stem.upper() in _WINDOWS_RESERVED_STEMS:
        stem = f"{stem}_file"
    stem = _truncate_stem(stem)
    return f"{stem or 'upload'}{suffix}"


def _truncate_stem(stem: str, max_bytes: int = 160) -> str:
    """Bound the stem by encoded length so CJK names stay within filesystem limits."""

    encoded = stem.encode("utf-8")
    if len(encoded) <= max_bytes:
        return stem
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(". \t")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
