from __future__ import annotations

from itertools import combinations
from typing import Any

import pandas as pd

from data_agent.utils.field_names import (
    looks_like_key,
    normalize_field_name,
    normalize_key_value,
)


def profile_dataframe(df: pd.DataFrame) -> dict[str, Any]:
    """Return a compact data-quality profile for audit and reporting."""

    columns = []
    for column in df.columns:
        series = df[column]
        empty_string_count = int(
            series.notna().astype(bool).mul(series.astype(str).str.strip().eq("")).sum()
        )
        columns.append(
            {
                "name": column,
                "dtype": str(series.dtype),
                "null_count": int(series.isna().sum()),
                "null_rate": round(float(series.isna().mean()), 4),
                "empty_string_count": empty_string_count,
                "unique_count": int(series.nunique(dropna=True)),
                "duplicate_value_count": int(series.duplicated(keep=False).sum()),
                "sample_values": ", ".join(series.dropna().astype(str).head(3).tolist()),
            }
        )

    return {
        "row_count": int(len(df)),
        "column_count": int(len(df.columns)),
        "duplicate_row_count": int(df.duplicated().sum()),
        "columns": columns,
    }


def profile_tables(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    table_rows = []
    column_rows = []
    issue_rows = []

    for table_name, df in tables.items():
        profile = profile_dataframe(df)
        table_rows.append(
            {
                "table": table_name,
                "row_count": profile["row_count"],
                "column_count": profile["column_count"],
                "duplicate_row_count": profile["duplicate_row_count"],
            }
        )

        for column in profile["columns"]:
            column_rows.append({"table": table_name, **column})
            if column["null_count"] > 0:
                issue_rows.append(
                    {
                        "table": table_name,
                        "field": column["name"],
                        "issue_type": "null_values",
                        "count": column["null_count"],
                    }
                )
            if column["empty_string_count"] > 0:
                issue_rows.append(
                    {
                        "table": table_name,
                        "field": column["name"],
                        "issue_type": "empty_strings",
                        "count": column["empty_string_count"],
                    }
                )

    return {
        "table_profile": pd.DataFrame(table_rows),
        "column_profile": pd.DataFrame(column_rows),
        "profile_issues": pd.DataFrame(issue_rows),
    }


def profile_dataset(
    tables: dict[str, pd.DataFrame],
    source_inventory: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build a product-oriented data map for many tables and sheets."""

    base = profile_tables(tables)
    key_candidates = detect_key_candidates(tables, base["column_profile"])
    relationship_candidates = detect_relationship_candidates(tables)
    overview = build_overview(tables, base, key_candidates, relationship_candidates)

    result = {
        "overview": overview,
        "table_profile": base["table_profile"],
        "column_profile": base["column_profile"],
        "profile_issues": base["profile_issues"],
        "key_candidates": key_candidates,
        "relationship_candidates": relationship_candidates,
    }
    if source_inventory is not None and not source_inventory.empty:
        result["source_inventory"] = source_inventory

    return result


def build_overview(
    tables: dict[str, pd.DataFrame],
    profile_sheets: dict[str, pd.DataFrame],
    key_candidates: pd.DataFrame,
    relationship_candidates: pd.DataFrame,
) -> pd.DataFrame:
    issue_count = len(profile_sheets["profile_issues"])
    recommended_lookup_count = 0
    if not relationship_candidates.empty:
        recommended_lookup_count = int(
            relationship_candidates["recommendation"].eq("recommended_lookup").sum()
        )

    rows = [
        {"metric": "table_count", "value": len(tables), "explanation": "识别到的表或 sheet 数"},
        {
            "metric": "total_rows",
            "value": int(sum(len(table) for table in tables.values())),
            "explanation": "所有表行数之和",
        },
        {
            "metric": "total_columns",
            "value": int(sum(len(table.columns) for table in tables.values())),
            "explanation": "所有表字段数之和",
        },
        {"metric": "profile_issue_count", "value": issue_count, "explanation": "基础质量问题数"},
        {
            "metric": "key_candidate_count",
            "value": len(key_candidates),
            "explanation": "可作为主键或匹配键的候选字段数",
        },
        {
            "metric": "relationship_candidate_count",
            "value": len(relationship_candidates),
            "explanation": "自动发现的跨表字段关系数",
        },
        {
            "metric": "recommended_lookup_count",
            "value": recommended_lookup_count,
            "explanation": "建议优先用于 VLOOKUP/mapping 的关系数",
        },
    ]
    return pd.DataFrame(rows)


def detect_key_candidates(
    tables: dict[str, pd.DataFrame],
    column_profile: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for row in column_profile.to_dict(orient="records"):
        table_name = row["table"]
        table = tables[table_name]
        non_null_count = row["row_count"] if "row_count" in row else len(table) - row["null_count"]
        non_null_count = max(int(non_null_count), 0)
        uniqueness_rate = _safe_rate(row["unique_count"], non_null_count)
        completeness_rate = 1 - float(row["null_rate"])
        name_score = _key_name_score(str(row["name"]))
        if name_score == 0:
            continue

        duplicate_count = int(row["duplicate_value_count"])
        score = round(uniqueness_rate * 0.55 + completeness_rate * 0.25 + name_score * 0.20, 4)

        recommendation = "low_confidence"
        if row["null_count"] == 0 and duplicate_count == 0 and uniqueness_rate == 1:
            recommendation = "primary_key_candidate"
        elif uniqueness_rate >= 0.8 and float(row["null_rate"]) <= 0.2:
            recommendation = "lookup_key_candidate"

        rows.append(
            {
                "table": table_name,
                "field": row["name"],
                "dtype": row["dtype"],
                "non_null_count": non_null_count,
                "unique_count": row["unique_count"],
                "null_rate": row["null_rate"],
                "duplicate_value_count": duplicate_count,
                "uniqueness_rate": round(uniqueness_rate, 4),
                "score": score,
                "recommendation": recommendation,
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "table",
                "field",
                "dtype",
                "non_null_count",
                "unique_count",
                "null_rate",
                "duplicate_value_count",
                "uniqueness_rate",
                "score",
                "recommendation",
            ]
        )

    return pd.DataFrame(rows).sort_values(
        by=["recommendation", "score"],
        ascending=[True, False],
        ignore_index=True,
    )


def detect_relationship_candidates(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for left_table, right_table in combinations(tables.keys(), 2):
        rows.extend(
            _relationship_rows(left_table, tables[left_table], right_table, tables[right_table])
        )
        rows.extend(
            _relationship_rows(right_table, tables[right_table], left_table, tables[left_table])
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "left_table",
                "left_field",
                "right_table",
                "right_field",
                "left_non_null_rows",
                "right_non_null_rows",
                "left_match_rate",
                "unmatched_left_count",
                "unmatched_left_sample",
                "unmatched_left_values",
                "left_duplicate_key_rows",
                "right_duplicate_key_rows",
                "right_duplicate_key_count",
                "right_duplicate_key_sample",
                "relationship_type",
                "estimated_join_row_count",
                "join_row_growth_count",
                "join_row_growth_rate",
                "join_will_expand",
                "max_right_matches_per_key",
                "match_mode",
                "right_duplicate_strategy_suggestion",
                "recommendation",
            ]
        )

    return pd.DataFrame(rows).sort_values(
        by=["recommendation", "left_match_rate"],
        ascending=[True, False],
        ignore_index=True,
    )


def _relationship_rows(
    left_table: str,
    left_df: pd.DataFrame,
    right_table: str,
    right_df: pd.DataFrame,
) -> list[dict[str, object]]:
    rows = []
    left_columns = {normalize_field_name(column): column for column in left_df.columns}
    right_columns = {normalize_field_name(column): column for column in right_df.columns}

    for normalized_name in sorted(set(left_columns) & set(right_columns)):
        left_field = left_columns[normalized_name]
        right_field = right_columns[normalized_name]
        left_values = _normalized_values(left_df[left_field])
        right_values = _normalized_values(right_df[right_field])
        if left_values.empty or right_values.empty:
            continue

        right_set = set(right_values)
        matched = left_values.isin(right_set)
        unmatched_values = sorted(set(left_values[~matched].tolist()))
        left_duplicate_rows = int(left_values.duplicated(keep=False).sum())
        right_duplicate_rows = int(right_values.duplicated(keep=False).sum())
        join_stats = _join_expansion_stats(left_df[left_field], right_df[right_field])
        match_rate = _safe_rate(int(matched.sum()), len(left_values))

        rows.append(
            {
                "left_table": left_table,
                "left_field": left_field,
                "right_table": right_table,
                "right_field": right_field,
                "left_non_null_rows": len(left_values),
                "right_non_null_rows": len(right_values),
                "left_match_rate": round(match_rate, 4),
                "unmatched_left_count": int((~matched).sum()),
                "unmatched_left_sample": ", ".join(unmatched_values[:10]),
                "unmatched_left_values": ", ".join(unmatched_values[:50]),
                "left_duplicate_key_rows": left_duplicate_rows,
                "right_duplicate_key_rows": right_duplicate_rows,
                "right_duplicate_key_count": join_stats["right_duplicate_key_count"],
                "right_duplicate_key_sample": join_stats["right_duplicate_key_sample"],
                "relationship_type": _relationship_type(left_duplicate_rows, right_duplicate_rows),
                "estimated_join_row_count": join_stats["estimated_join_row_count"],
                "join_row_growth_count": join_stats["join_row_growth_count"],
                "join_row_growth_rate": join_stats["join_row_growth_rate"],
                "join_will_expand": join_stats["join_will_expand"],
                "max_right_matches_per_key": join_stats["max_right_matches_per_key"],
                "match_mode": "normalized_exact",
                "right_duplicate_strategy_suggestion": (
                    _duplicate_strategy_suggestion(right_duplicate_rows)
                ),
                "recommendation": _relationship_recommendation(match_rate, right_duplicate_rows),
            }
        )

    return rows


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _key_name_score(column_name: str) -> float:
    return 1.0 if looks_like_key(column_name) else 0.0


def _normalized_values(series: pd.Series) -> pd.Series:
    values = series.dropna().map(normalize_key_value)
    return values[~values.eq("")]


def _normalized_join_values(series: pd.Series) -> pd.Series:
    return series.fillna("").map(normalize_key_value)


def _join_expansion_stats(left_series: pd.Series, right_series: pd.Series) -> dict[str, object]:
    left_keys = _normalized_join_values(left_series)
    right_keys = _normalized_join_values(right_series)
    right_keys = right_keys[~right_keys.eq("")]
    right_counts = right_keys.value_counts()

    estimated_join_rows = 0
    for value in left_keys:
        if value == "":
            estimated_join_rows += 1
            continue
        estimated_join_rows += max(int(right_counts.get(value, 0)), 1)

    duplicate_counts = right_counts[right_counts.gt(1)]
    growth_count = int(estimated_join_rows - len(left_series))
    return {
        "estimated_join_row_count": int(estimated_join_rows),
        "join_row_growth_count": growth_count,
        "join_row_growth_rate": round(_safe_rate(growth_count, len(left_series)), 4),
        "join_will_expand": growth_count > 0,
        "max_right_matches_per_key": int(right_counts.max()) if not right_counts.empty else 0,
        "right_duplicate_key_count": int(len(duplicate_counts)),
        "right_duplicate_key_sample": ", ".join(sorted(duplicate_counts.index.tolist())[:10]),
    }


def _relationship_type(left_duplicate_rows: int, right_duplicate_rows: int) -> str:
    left_many = left_duplicate_rows > 0
    right_many = right_duplicate_rows > 0
    if left_many and right_many:
        return "N:N"
    if left_many:
        return "N:1"
    if right_many:
        return "1:N"
    return "1:1"


def _relationship_recommendation(match_rate: float, right_duplicate_rows: int) -> str:
    if right_duplicate_rows > 0:
        return "right_key_not_unique"
    if match_rate >= 0.8:
        return "recommended_lookup"
    if match_rate >= 0.5:
        return "possible_lookup_needs_review"
    return "low_match_rate"


def _duplicate_strategy_suggestion(right_duplicate_rows: int) -> str:
    if right_duplicate_rows > 0:
        return "error_or_aggregate_review"
    return "first"
