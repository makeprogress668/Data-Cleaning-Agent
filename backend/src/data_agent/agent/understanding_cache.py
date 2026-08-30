"""Same files, same sentence — same understanding, every time.

Goal understanding is the one step in this pipeline that is not reproducible. The
model times out (30s, twice, then give up), the run silently falls back to keyword
interpretation, and the user gets a visibly different workbook from the one they got
yesterday with the same file and the same request. For a business user running the
same report every morning that reads as "昨天还好好的" — and nothing in the output
explains it.

So a successful understanding is persisted under a key made of the things that
should change the answer — the goal text, the schema it ran against, the prompt
version, the model — and a later run with all four identical reuses it instead of
asking again. That makes the same request stable across days, cheap on the second
run, and unaffected by a model outage. Change the file, the wording, the prompt, or
the model and the key changes with it, so nothing goes stale silently.

Off by default is the wrong default here: reproducibility is the point. Set
``DATA_AGENT_UNDERSTANDING_CACHE=0`` to force a fresh call every time (debugging
prompt changes), and delete the file to drop everything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.tenancy import current_tenant_id

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Bounded so a long-lived deployment cannot grow the file without limit. Entries are
# evicted oldest-first; a re-run of an evicted goal simply calls the model again.
MAX_ENTRIES = 200
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60
_CACHE_LOCK = threading.Lock()

# Keys that describe *this run* rather than the understanding itself. Storing them
# would pin one run's transient state onto every later replay.
_VOLATILE_KEYS = ("data_access",)


def cache_enabled() -> bool:
    """Whether understanding reuse is on. On unless explicitly disabled."""

    raw = os.environ.get("DATA_AGENT_UNDERSTANDING_CACHE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def cache_path() -> Path:
    """Resolve the cache file path, mirroring the memory/api_work_dir convention."""

    return Path(
        os.environ.get(
            "DATA_AGENT_UNDERSTANDING_CACHE_PATH",
            PROJECT_ROOT / "data" / "understanding_cache.json",
        )
    )


def cache_ttl_seconds() -> int:
    """Maximum age of a reusable understanding; non-positive disables expiry."""

    raw = os.environ.get(
        "DATA_AGENT_UNDERSTANDING_CACHE_TTL_SECONDS",
        str(DEFAULT_TTL_SECONDS),
    ).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "DATA_AGENT_UNDERSTANDING_CACHE_TTL_SECONDS 必须是整数"
        ) from exc


def cache_backend(path: Path | None = None) -> str:
    """Use a local file for development and shared PostgreSQL in production."""

    if path is not None:
        return "file"
    configured = os.environ.get("DATA_AGENT_UNDERSTANDING_CACHE_BACKEND", "").strip().lower()
    backend = configured or (
        "postgres"
        if os.environ.get("DATA_AGENT_METADATA_BACKEND", "file").strip().lower() == "postgres"
        else "file"
    )
    if backend not in {"file", "postgres"}:
        raise RuntimeError(
            "DATA_AGENT_UNDERSTANDING_CACHE_BACKEND 仅支持 file 或 postgres"
        )
    return backend


def cache_key(
    goal: str,
    tables: dict[str, pd.DataFrame],
    *,
    prompt_version: str,
    model: str,
    data_access_mode: str = "",
    tenant_id: str | None = None,
) -> str:
    """Fingerprint everything that should change the understanding.

    Raw values never enter the key. A compact semantic profile does: dtypes and value
    shape categories distinguish, for example, an accounting-number column from a
    free-text column without persisting business data. Row counts are excluded so a
    monthly refresh with the same semantics can still reuse the understanding.

    ``data_access_mode`` is in the key because it decides how much of the data the
    model was allowed to see. Left out, an operator who widened the policy from
    metadata_only to trusted_samples specifically to get a better reading would keep
    being served the narrower one — a setting that silently does nothing.
    """

    tenant = (tenant_id or current_tenant_id()).strip()
    semantic_profile = _semantic_profile(tables)
    material = json.dumps(
        {
            "version": CACHE_VERSION,
            "tenant_id": tenant,
            "goal": " ".join(goal.split()),
            "semantic_profile": semantic_profile,
            "prompt_version": prompt_version,
            "model": model,
            "data_access_mode": data_access_mode,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_entry(key: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a stored understanding, or ``None`` when absent/unusable."""

    if not cache_enabled():
        return None
    try:
        if cache_backend(path) == "postgres":
            return _shared_repository().read_understanding_cache(
                key,
                max_age_seconds=cache_ttl_seconds(),
            )
        entry = _load_all(path).get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        if _is_expired(str(entry.get("stored_at") or "")):
            return None
        plan = entry.get("plan")
        return dict(plan) if isinstance(plan, dict) else None
    except Exception as exc:  # noqa: BLE001 - cache failure must not fail the job
        logger.warning("理解结果缓存读取失败，本次继续调用模型：%s", exc)
        return None


def store_entry(key: str, plan: dict[str, Any], path: Path | None = None) -> None:
    """Persist one successful understanding. Never raises — a cache is not the job."""

    if not cache_enabled():
        return
    target = path or cache_path()
    payload = {
        key: value for key, value in plan.items() if key not in _VOLATILE_KEYS
    }
    try:
        if cache_backend(path) == "postgres":
            _shared_repository().write_understanding_cache(key, payload, MAX_ENTRIES)
            return
        with _CACHE_LOCK:
            data = _load_all(target)
            entries = data.setdefault("entries", {})
            entries[key] = {
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "plan": payload,
            }
            _evict(entries)
            _write(target, data)
    except Exception as exc:  # noqa: BLE001 - cache failure must not fail the job
        logger.warning("理解结果缓存写入失败，本次不影响执行：%s", exc)


def clear_cache(path: Path | None = None) -> int:
    """Forget every stored understanding. Returns how many were dropped.

    Reuse makes a good reading repeatable — and a bad one permanent. Without a way to
    drop it, a goal the model once misread would keep being misread on every rerun,
    with nothing in the product to escape it short of knowing the file exists and
    deleting it by hand. That is not something a business user can be expected to do.
    """

    try:
        if cache_backend(path) == "postgres":
            return _shared_repository().clear_understanding_cache()
        target = path or cache_path()
        count = len(_load_all(target).get("entries", {}))
        with _CACHE_LOCK:
            if target.exists():
                target.unlink()
        return count
    except Exception as exc:  # noqa: BLE001 - a maintenance cache cannot break the CLI
        logger.warning("理解结果缓存清空失败：%s", exc)
        return 0


def cache_stats(path: Path | None = None) -> dict[str, Any]:
    """What is stored right now, for the CLI to show before clearing."""

    try:
        backend = cache_backend(path)
        if backend == "postgres":
            return {
                "enabled": cache_enabled(),
                "backend": backend,
                "path": "postgresql:data_agent_understanding_cache",
                "ttl_seconds": cache_ttl_seconds(),
                **_shared_repository().understanding_cache_stats(),
            }
    except Exception as exc:  # noqa: BLE001 - stats should remain inspectable on outage
        logger.warning("理解结果缓存状态读取失败：%s", exc)
        return {
            "enabled": cache_enabled(),
            "backend": "postgres",
            "path": "postgresql:data_agent_understanding_cache",
            "entry_count": 0,
            "newest": "",
            "oldest": "",
            "ttl_seconds": cache_ttl_seconds(),
        }
    data = _load_all(path)
    entries = data.get("entries", {})
    stored = sorted(
        (str(entry.get("stored_at") or "") for entry in entries.values()),
        reverse=True,
    )
    return {
        "enabled": cache_enabled(),
        "backend": "file",
        "path": str(path or cache_path()),
        "entry_count": len(entries),
        "newest": stored[0] if stored else "",
        "oldest": stored[-1] if stored else "",
        "ttl_seconds": cache_ttl_seconds(),
    }


def _semantic_profile(tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Describe column semantics without retaining raw business values."""

    profile: dict[str, Any] = {}
    for table_name, frame in sorted(tables.items(), key=lambda item: str(item[0])):
        columns = []
        for column in frame.columns:
            sample = frame[column].dropna().head(64).tolist()
            columns.append(
                {
                    "name": str(column),
                    "dtype": str(frame[column].dtype),
                    "value_shapes": sorted({_value_shape(value) for value in sample}),
                }
            )
        profile[str(table_name)] = columns
    return profile


def _value_shape(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, (datetime, pd.Timestamp)):
        return "datetime"
    text = str(value).strip()
    if not text:
        return "empty_text"
    if re.fullmatch(r"\([+-]?[\d,.]+\)", text):
        return "accounting_number_text"
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        return "grouped_number_text"
    if re.fullmatch(r"[+-]?[\d.]+(?:万|亿|%)?", text):
        return "numeric_text"
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}.*", text):
        return "date_text"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        return "email_text"
    if re.search(r"[A-Za-z]", text) and re.search(r"\d", text):
        return "code_text"
    return "short_text" if len(text) <= 16 else "long_text"


def _is_expired(stored_at: str) -> bool:
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return False
    try:
        stored = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - stored).total_seconds() > ttl


def _shared_repository():
    """Resolve lazily so local/CLI installs do not import production dependencies."""

    from data_agent.api.metadata_repository import configured_metadata_repository

    repository = configured_metadata_repository()
    if repository is None:
        raise RuntimeError("PostgreSQL 理解缓存需要启用 PostgreSQL 元数据存储")
    return repository


def _load_all(path: Path | None = None) -> dict[str, Any]:
    target = path or cache_path()
    if not target.exists():
        return {"version": CACHE_VERSION, "entries": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("忽略无法读取的理解缓存 %s：%s", target, exc)
        return {"version": CACHE_VERSION, "entries": {}}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "entries": {}}
    entries = data.get("entries")
    return {
        "version": CACHE_VERSION,
        "entries": entries if isinstance(entries, dict) else {},
    }


def _evict(entries: dict[str, Any]) -> None:
    if len(entries) <= MAX_ENTRIES:
        return
    ordered = sorted(
        entries.items(),
        key=lambda item: str(item[1].get("stored_at") or ""),
    )
    for key, _ in ordered[: len(entries) - MAX_ENTRIES]:
        entries.pop(key, None)


def _write(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
