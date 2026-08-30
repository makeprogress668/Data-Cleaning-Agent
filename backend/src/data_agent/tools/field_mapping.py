from __future__ import annotations

from typing import Any

import pandas as pd


def apply_field_mapping(
    df: pd.DataFrame,
    rename: dict[str, str] | None = None,
    value_maps: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """Apply column rename mapping and enum/value mapping."""

    result = df.copy()

    if rename:
        result = result.rename(columns=rename)

    if value_maps:
        for column, mapping in value_maps.items():
            if column not in result.columns:
                raise KeyError(f"Value mapping column not found: {column}")
            result[f"{column}_mapped"] = result[column].map(mapping).fillna(result[column])

    return result
