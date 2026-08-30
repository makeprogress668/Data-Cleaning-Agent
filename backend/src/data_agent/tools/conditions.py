from __future__ import annotations

from typing import Any

import pandas as pd

from data_agent.schemas.job import ConditionConfig
from data_agent.tools.duckdb_runner import try_duckdb_condition


def normalize_text(series: pd.Series, case_sensitive: bool = False) -> pd.Series:
    text = series.fillna("").astype(str).str.strip()
    return text if case_sensitive else text.str.lower()


def evaluate_condition(df: pd.DataFrame, condition: ConditionConfig) -> pd.Series:
    mask = try_duckdb_condition(df, condition)
    if mask is None:
        mask = _evaluate(df, condition)
    engine = mask.attrs.get("data_agent_engine")
    result = ~mask if condition.negate else mask
    if engine:
        result.attrs["data_agent_engine"] = engine
    return result


def _evaluate(df: pd.DataFrame, condition: ConditionConfig) -> pd.Series:
    if condition.field not in df.columns:
        raise KeyError(f"Condition field not found: {condition.field}")

    series = df[condition.field]
    op = condition.op

    if op == "is_blank":
        return series.isna() | series.astype(str).str.strip().eq("")
    if op == "not_blank":
        return ~(series.isna() | series.astype(str).str.strip().eq(""))
    if op == "is_null":
        return series.isna()
    if op == "not_null":
        return series.notna()
    if op == "duplicated":
        return series.duplicated(keep=False)
    if op == "lookup_missing":
        return ~series.fillna(False).astype(bool)
    if op == "numeric_invalid":
        blank = series.isna() | series.astype(str).str.strip().eq("")
        return pd.to_numeric(series, errors="coerce").isna() & ~blank
    if op == "date_invalid":
        blank = series.isna() | series.astype(str).str.strip().eq("")
        return pd.to_datetime(series, errors="coerce", format="mixed").isna() & ~blank

    text_series = normalize_text(series, condition.case_sensitive)
    value = condition.value
    compare_value = str(value).strip()
    if not condition.case_sensitive:
        compare_value = compare_value.lower()

    if op == "equals":
        return text_series.eq(compare_value)
    if op == "not_equals":
        return ~text_series.eq(compare_value)
    if op == "contains":
        return text_series.str.contains(compare_value, na=False, regex=False)
    if op == "not_contains":
        return ~text_series.str.contains(compare_value, na=False, regex=False)
    if op == "regex_match":
        return text_series.str.match(str(value), na=False)
    if op == "regex_not_match":
        blank = series.isna() | series.astype(str).str.strip().eq("")
        return ~text_series.str.match(str(value), na=False) & ~blank
    if op == "in":
        values = _normalize_values(condition.values, condition.case_sensitive)
        return text_series.isin(values)
    if op == "not_in":
        values = _normalize_values(condition.values, condition.case_sensitive)
        return ~text_series.isin(values)
    if op == "gt":
        return pd.to_numeric(series, errors="coerce") > float(value)
    if op == "gte":
        return pd.to_numeric(series, errors="coerce") >= float(value)
    if op == "lt":
        return pd.to_numeric(series, errors="coerce") < float(value)
    if op == "lte":
        return pd.to_numeric(series, errors="coerce") <= float(value)

    raise ValueError(f"Unsupported condition op: {op}")


def _normalize_values(values: list[Any], case_sensitive: bool) -> list[str]:
    normalized = [str(value).strip() for value in values]
    return normalized if case_sensitive else [value.lower() for value in normalized]
