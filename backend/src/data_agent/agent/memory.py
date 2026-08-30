"""Lightweight long-term memory for stable, cross-task user preferences.

Phase three of the agent upgrade. Over many tasks a user tends to treat the same
things the same way - the same table as the main table, the same columns as
"critical", the same match tolerance. This module remembers those *stable*
preferences (as frequency counts of schema-level names, never raw data) and feeds
a compact soft hint back into the planner so the agent gets more consistent and
needs less repeat instruction.

Design principles, matching the rest of the project:
- Opt-in. Disabled by default (``DATA_AGENT_MEMORY_ENABLED``); when off, every
  function is a no-op and no file is written, so behaviour is unchanged.
- Deterministic and local. A single small JSON file, same serialization idiom as
  the rest of the repo (ensure_ascii=False, indent=2, utf-8, mkdir parents).
- Safe. Only field/table names and match-mode strings with counts are stored.
  Never raw rows, values, file contents, or secrets.
- Advisory only. The summary is a *soft* hint. The LLM still may not invent
  tables/fields, and every plan still passes schema + field-existence validation,
  so a stale memory can never corrupt a result.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_agent.tenancy import current_tenant_id

logger = logging.getLogger(__name__)

MEMORY_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# How many top preferences to surface in the prompt hint. Kept small on purpose:
# memory nudges, it does not dictate.
TOP_BASE_TABLES = 3
TOP_CRITICAL_FIELDS = 5
TOP_MATCH_MODES = 2
_MEMORY_LOCK = threading.Lock()


def memory_enabled() -> bool:
    """Whether long-term memory is turned on (off by default)."""

    raw = os.environ.get("DATA_AGENT_MEMORY_ENABLED")
    if raw is None or not raw.strip():
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def memory_path() -> Path:
    """Resolve the memory file path, mirroring the api_work_dir convention."""

    base = Path(
        os.environ.get("DATA_AGENT_MEMORY_PATH", PROJECT_ROOT / "data" / "user_memory.json")
    )
    if not os.environ.get("DATA_AGENT_TENANT_API_KEYS_JSON", "").strip():
        return base
    return base.parent / "tenants" / current_tenant_id() / base.name


def _empty_memory() -> dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "updated_at": None,
        "task_count": 0,
        "preferences": {
            "base_table": {},
            "critical_fields": {},
            "match_modes": {},
            # What this user picked last time a clarification offered choices, per
            # slot kind. Stored the same way as everything else here: the chosen
            # option string and a count, never the goal text around it.
            "clarification": {},
        },
    }


def load_memory(path: Path | None = None) -> dict[str, Any]:
    """Load the memory file, returning a well-formed empty memory when absent.

    Corruption-tolerant: a malformed file is logged and treated as empty rather
    than raising, so a bad file can never break a run.
    """

    target = path or memory_path()
    if not target.exists():
        return _empty_memory()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Ignoring unreadable memory file %s: %s", target, exc)
        return _empty_memory()
    if not isinstance(data, dict):
        return _empty_memory()
    return _normalize(data)


def save_memory(memory: dict[str, Any], path: Path | None = None) -> Path | None:
    """Persist memory, skipping the write when nothing meaningful changed.

    Returns the written path, or ``None`` when the write was skipped (debounced)
    because the payload is identical to what is already on disk.
    """

    target = path or memory_path()
    normalized = _normalize(memory)
    existing = load_memory(target)
    if _payload(existing) == _payload(normalized):
        return None
    normalized["updated_at"] = datetime.now(timezone.utc).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return target


def update_from_job_config(
    memory: dict[str, Any],
    job_config: dict[str, Any],
    goal: str = "",
) -> dict[str, Any]:
    """Fold one executed job's schema-level choices into memory (pure function).

    Records only names and mode strings as frequency counts. ``goal`` is accepted
    for future use but intentionally not stored verbatim (avoids persisting free
    text that could carry sensitive context).
    """

    updated = _normalize(memory)
    prefs = updated["preferences"]

    base_table = str(job_config.get("base_table") or "").strip()
    if base_table:
        _bump(prefs["base_table"], base_table)

    for critical_field in job_config.get("quality_score", {}).get("critical_fields", []):
        name = str(critical_field).strip()
        if name:
            _bump(prefs["critical_fields"], name)

    for lookup in _iter_lookups(job_config):
        mode = str(lookup.get("match_mode") or "").strip()
        if mode:
            _bump(prefs["match_modes"], mode)

    updated["task_count"] = int(updated.get("task_count", 0)) + 1
    return updated


def record_task(
    job_config: dict[str, Any],
    goal: str = "",
    path: Path | None = None,
) -> Path | None:
    """Convenience: load -> fold in this job -> save. No-op when disabled."""

    if not memory_enabled():
        return None
    try:
        with _MEMORY_LOCK:
            memory = load_memory(path)
            memory = update_from_job_config(memory, job_config, goal)
            return save_memory(memory, path)
    except Exception as exc:  # noqa: BLE001 - memory must never break a delivery
        logger.warning("Skipping memory update after task: %s", exc)
        return None


def summarize_for_prompt(memory: dict[str, Any]) -> dict[str, Any] | None:
    """Build a compact, advisory hint from memory, or ``None`` when empty."""

    prefs = _normalize(memory)["preferences"]
    base_tables = _top(prefs["base_table"], TOP_BASE_TABLES)
    critical_fields = _top(prefs["critical_fields"], TOP_CRITICAL_FIELDS)
    match_modes = _top(prefs["match_modes"], TOP_MATCH_MODES)
    if not (base_tables or critical_fields or match_modes):
        return None

    hint: dict[str, Any] = {
        "note": (
            "Soft hints learned from this user's past tasks. Apply only when they "
            "fit the current tables and columns; never invent tables or fields to "
            "match a hint."
        )
    }
    if base_tables:
        hint["frequent_base_tables"] = base_tables
    if critical_fields:
        hint["frequent_critical_fields"] = critical_fields
    if match_modes:
        hint["preferred_match_modes"] = match_modes
    return hint


def load_memory_summary(path: Path | None = None) -> dict[str, Any] | None:
    """Load memory and return its prompt hint, or ``None`` when off/empty."""

    if not memory_enabled():
        return None
    try:
        return summarize_for_prompt(load_memory(path))
    except Exception as exc:  # noqa: BLE001 - never let memory break planning
        logger.warning("Skipping memory hint: %s", exc)
        return None


def _iter_lookups(job_config: dict[str, Any]) -> list[dict[str, Any]]:
    lookups = [lookup for lookup in job_config.get("lookups", []) if isinstance(lookup, dict)]
    single = job_config.get("lookup")
    if isinstance(single, dict):
        lookups.append(single)
    return lookups


def _bump(counter: dict[str, Any], key: str) -> None:
    counter[key] = int(counter.get(key, 0)) + 1


def _top(counter: dict[str, Any], limit: int) -> list[str]:
    items = [(name, int(count)) for name, count in counter.items() if int(count) > 0]
    items.sort(key=lambda item: (-item[1], item[0]))
    return [name for name, _count in items[:limit]]


def _payload(memory: dict[str, Any]) -> dict[str, Any]:
    """The debounce-relevant content of memory (everything except the timestamp)."""

    return {
        "version": memory.get("version"),
        "task_count": memory.get("task_count"),
        "preferences": memory.get("preferences"),
    }


def _normalize(memory: dict[str, Any]) -> dict[str, Any]:
    base = _empty_memory()
    if not isinstance(memory, dict):
        return base
    base["version"] = memory.get("version", MEMORY_VERSION)
    base["updated_at"] = memory.get("updated_at")
    base["task_count"] = int(memory.get("task_count", 0) or 0)
    prefs = memory.get("preferences", {})
    if isinstance(prefs, dict):
        for key in base["preferences"]:
            value = prefs.get(key, {})
            if not isinstance(value, dict):
                continue
            # "clarification" is one level deeper — {slot kind: {chosen option: count}}
            # — so the flat counter normalisation would have emptied it on every load.
            base["preferences"][key] = (
                {
                    str(kind): _counter(counts)
                    for kind, counts in value.items()
                    if isinstance(counts, dict)
                }
                if key == "clarification"
                else _counter(value)
            )
    return base


def _counter(value: dict[Any, Any]) -> dict[str, int]:
    return {str(name): int(count) for name, count in value.items() if _is_int(count)}


def _is_int(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def record_clarification_choice(
    kind: str,
    answer: str,
    path: Path | None = None,
) -> Path | None:
    """Remember which option a user picked, so the next question can lead with it.

    Asking again is not a failure — a column that mattered last month still needs
    confirming this month. What should improve is the *question*: 「按什么统计？」 with
    「城市」 first, because that is what this user has meant every previous time.

    Only the option string is kept, and only when it names something the run offered.
    """

    if not memory_enabled():
        return None
    slot_kind = str(kind).strip()
    choice = str(answer).strip()
    if not slot_kind or not choice:
        return None
    try:
        with _MEMORY_LOCK:
            memory = load_memory(path)
            bucket = memory["preferences"].setdefault("clarification", {})
            _bump(bucket.setdefault(slot_kind, {}), choice)
            return save_memory(memory, path)
    except Exception as exc:  # noqa: BLE001 - memory must never break a delivery
        logger.warning("Skipping clarification memory update: %s", exc)
        return None


def remembered_choices(kind: str, path: Path | None = None) -> list[str]:
    """Previously chosen options for one slot kind, most frequent first."""

    if not memory_enabled():
        return []
    try:
        bucket = load_memory(path)["preferences"].get("clarification", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skipping clarification memory read: %s", exc)
        return []
    counts = bucket.get(str(kind).strip()) or {}
    if not isinstance(counts, dict):
        return []
    return [
        name
        for name, _count in sorted(
            counts.items(), key=lambda item: (-int(item[1] or 0), str(item[0]))
        )
    ]
