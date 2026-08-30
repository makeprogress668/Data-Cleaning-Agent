"""Policy-controlled data context for LLM goal understanding and planning.

The model benefits from value-level signals when column names alone are ambiguous,
but it does not need an unrestricted copy of the dataset. This module is the single
place that decides which schema statistics and samples may enter an LLM prompt.

Three modes are supported:

``metadata_only``
    Table/column names, dtypes and aggregate profile statistics only.
``masked_samples`` (default)
    Metadata plus small samples. Values from identifier or sensitive-looking fields
    are replaced with structural patterns; low-risk categorical values stay visible.
``trusted_samples``
    Metadata plus a few raw samples, intended for private deployments or explicit
    user authorization.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from typing import Any

import pandas as pd

from data_agent.utils.field_names import looks_like_key

DEFAULT_SAMPLE_LIMIT = 3


class DataAccessMode(str, Enum):
    METADATA_ONLY = "metadata_only"
    MASKED_SAMPLES = "masked_samples"
    TRUSTED_SAMPLES = "trusted_samples"


_SENSITIVE_FIELD_TOKENS = (
    "address",
    "bank",
    "card",
    "email",
    "idcard",
    "identity",
    "mail",
    "memo",
    "mobile",
    "name",
    "note",
    "notes",
    "passport",
    "phone",
    "remark",
    "comment",
    "content",
    "description",
    "text",
    "ssn",
    "tel",
    "住址",
    "地址",
    "姓名",
    "手机",
    "电话",
    "邮箱",
    "证件",
    "身份证",
    "银行卡",
    "备注",
    "评论",
    "内容",
    "描述",
    "说明",
)
_RAW_RELATIONSHIP_VALUE_COLUMNS = frozenset(
    {
        "unmatched_left_sample",
        "unmatched_left_values",
        "right_duplicate_key_sample",
    }
)
_RAW_CONTEXT_KEY_TOKENS = ("sample",)


def load_data_access_mode() -> DataAccessMode:
    """Resolve the configured LLM data-access policy.

    Invalid values fail closed to ``masked_samples`` rather than silently exposing raw
    values. The mode is read per request so tests and deployments can change it without
    rebuilding a global client.
    """

    raw = os.environ.get(
        "DATA_AGENT_LLM_DATA_ACCESS_MODE",
        DataAccessMode.MASKED_SAMPLES.value,
    )
    try:
        return DataAccessMode(raw.strip().lower())
    except ValueError:
        return DataAccessMode.MASKED_SAMPLES


def build_column_context(
    tables: dict[str, pd.DataFrame],
    *,
    column_profile: pd.DataFrame | None = None,
    mode: DataAccessMode | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a flat, policy-filtered column context plus audit metadata."""

    resolved_mode = mode or load_data_access_mode()
    profile_index = _profile_index(column_profile)
    columns: list[dict[str, Any]] = []
    masked_fields: list[str] = []

    for table_name, frame in tables.items():
        for column in frame.columns:
            field_name = str(column)
            profile = profile_index.get((str(table_name), field_name), {})
            record: dict[str, Any] = {
                "table": str(table_name),
                "field": field_name,
                "dtype": str(profile.get("dtype") or frame[column].dtype),
            }
            for key in ("null_rate", "unique_count"):
                if profile.get(key) is not None:
                    record[key] = _json_scalar(profile[key])

            if resolved_mode is not DataAccessMode.METADATA_ONLY:
                raw_samples = [
                    str(value)
                    for value in frame[column].dropna().unique()[: max(0, sample_limit)]
                ]
                if resolved_mode is DataAccessMode.TRUSTED_SAMPLES:
                    record["sample_values"] = raw_samples
                elif _is_sensitive_field(field_name) or any(
                    _contains_sensitive_value(value) for value in raw_samples
                ):
                    record["sample_values"] = [_value_pattern(value) for value in raw_samples]
                    masked_fields.append(f"{table_name}.{field_name}")
                else:
                    record["sample_values"] = raw_samples
            columns.append(record)

    access = {
        "mode": resolved_mode.value,
        "sample_limit_per_field": (
            0 if resolved_mode is DataAccessMode.METADATA_ONLY else max(0, sample_limit)
        ),
        "raw_samples_allowed": resolved_mode is DataAccessMode.TRUSTED_SAMPLES,
        "masked_fields": sorted(set(masked_fields)),
        "column_count": len(columns),
    }
    return columns, access


def group_columns_by_table(columns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group the canonical flat context for planner prompts."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in columns:
        table = str(record.get("table") or "")
        if not table:
            continue
        item = {key: value for key, value in record.items() if key != "table"}
        # Preserve the planner prompt's established per-table contract while the
        # canonical flat context uses ``field`` for consistency with goal understanding.
        item["name"] = item.pop("field", "")
        grouped.setdefault(table, []).append(item)
    return grouped


def build_profile_column_context(
    column_profile: pd.DataFrame,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a metadata-only context when callers do not hold source tables.

    This compatibility path intentionally omits profile ``sample_values`` because a
    pre-rendered string cannot be reliably masked value by value. Normal planning
    passes the source tables to :func:`build_column_context` and therefore keeps the
    configured sample policy.
    """

    columns: list[dict[str, Any]] = []
    if column_profile is not None and not column_profile.empty:
        for row in _records(column_profile):
            table = str(row.get("table") or "")
            field = str(row.get("name") or row.get("field") or "")
            if not table or not field:
                continue
            record: dict[str, Any] = {
                "table": table,
                "field": field,
                "dtype": str(row.get("dtype") or ""),
            }
            for key in ("null_rate", "unique_count"):
                if row.get(key) is not None:
                    record[key] = row[key]
            columns.append(record)
    return columns, {
        "mode": DataAccessMode.METADATA_ONLY.value,
        "sample_limit_per_field": 0,
        "raw_samples_allowed": False,
        "masked_fields": [],
        "column_count": len(columns),
    }


def relationship_context(
    frame: pd.DataFrame,
    *,
    mode: DataAccessMode | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Project relationship evidence without leaking key samples by default."""

    if frame is None or frame.empty:
        return []
    resolved_mode = mode or load_data_access_mode()
    records = _records(frame.head(limit))
    if resolved_mode is DataAccessMode.TRUSTED_SAMPLES:
        return records
    return [
        {
            key: value
            for key, value in record.items()
            if key not in _RAW_RELATIONSHIP_VALUE_COLUMNS
        }
        for record in records
    ]


def policy_filtered_records(
    frame: pd.DataFrame,
    *,
    mode: DataAccessMode | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Project planner diagnostics through the same sample-access policy.

    Recommendation frames often repeat raw key samples already present in the
    relationship profile. Filtering only ``relationship_context`` is insufficient:
    the duplicate fields would otherwise re-enter the serialized planner prompt.
    """

    if frame is None or frame.empty:
        return []
    selected = frame.head(limit) if limit is not None else frame
    records = _records(selected)
    resolved_mode = mode or load_data_access_mode()
    if resolved_mode is DataAccessMode.TRUSTED_SAMPLES:
        return records
    return [
        {
            key: value
            for key, value in record.items()
            if not any(token in key.lower() for token in _RAW_CONTEXT_KEY_TOKENS)
        }
        for record in records
    ]


def _profile_index(frame: pd.DataFrame | None) -> dict[tuple[str, str], dict[str, Any]]:
    if frame is None or frame.empty:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _records(frame):
        table = str(row.get("table") or "")
        field = str(row.get("name") or row.get("field") or "")
        if table and field:
            result[(table, field)] = row
    return result


def _is_sensitive_field(field: str) -> bool:
    lowered = field.lower()
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", lowered)
    word_tokens = {
        token for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", lowered) if token
    }
    if "id" in word_tokens or "no" in word_tokens or re.search(r"Id$", field):
        return True
    return any(token in normalized for token in _SENSITIVE_FIELD_TOKENS) or looks_like_key(
        normalized
    )


def _value_pattern(value: str) -> str:
    text = str(value)
    if "@" in text:
        return "<email>"
    if text.isdigit():
        return f"<digits:length={len(text)}>"
    digits = sum(character.isdigit() for character in text)
    letters = sum(character.isalpha() for character in text)
    return f"<text:length={len(text)},letters={letters},digits={digits}>"


def _contains_sensitive_value(value: str) -> bool:
    text = str(value)
    return bool(
        re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        or re.search(r"(?<!\d)(?:\+?\d[\d\s()-]{7,}\d)(?!\d)", text)
        or re.search(r"(?:地址|住址|address)\s*[:：]?", text, flags=re.IGNORECASE)
    )


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    normalized = frame.astype(object).where(pd.notna(frame), None)
    return [
        {str(key): _json_scalar(value) for key, value in row.items()}
        for row in normalized.to_dict("records")
    ]


def _json_scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            return value
    return value
