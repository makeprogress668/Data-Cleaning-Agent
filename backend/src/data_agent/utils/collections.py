"""Small shared primitives that several layers were each re-declaring.

``deep_merge`` in particular had four copies with two different semantics — one of
them shallow-copied the base, so nested objects were shared between the "merged"
result and its input. Keeping one definition removes that class of aliasing bug.
"""

from __future__ import annotations

import copy
from typing import Any, TypeVar

import pandas as pd

T = TypeVar("T")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Nested dicts merge key by key; every other value is replaced by a deep copy, so
    the result never shares mutable state with its inputs.
    """

    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def unique(values) -> list:
    """Order-preserving de-duplication."""

    result: list = []
    seen: set = set()
    for value in values:
        try:
            if value in seen:
                continue
            seen.add(value)
        except TypeError:  # unhashable values fall back to a linear scan
            if value in result:
                continue
        result.append(value)
    return result


def frame_records(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Convert a DataFrame to records with NaN normalised to ``None``."""

    if df is None or df.empty:
        return []
    normalized = df.astype(object).where(pd.notna(df), None)
    return [
        {str(key): value for key, value in row.items()}
        for row in normalized.to_dict(orient="records")
    ]


def as_frame(value: object) -> pd.DataFrame:
    """Coerce an optional payload into a DataFrame."""

    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()
