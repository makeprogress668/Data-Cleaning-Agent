from __future__ import annotations

from difflib import SequenceMatcher

import pandas as pd

from data_agent.tools.duckdb_runner import try_duckdb_left_join_indices
from data_agent.utils.field_names import normalize_key_value

SUPPORTED_MATCH_MODES = {
    "exact",
    "trim",
    "case_insensitive",
    "normalized_exact",
    "fuzzy",
}
SUPPORTED_DUPLICATE_STRATEGIES = {"first", "error", "list", "aggregate", "review"}


def vlookup(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_key: str | list[str],
    right_key: str | list[str],
    fields: list[str],
    field_aliases: dict[str, str] | None = None,
    suffix: str = "_lookup",
    match_field: str = "_lookup_matched",
    confidence_field: str | None = None,
    explanation_field: str | None = None,
    ambiguity_field: str | None = None,
    match_mode: str = "exact",
    duplicate_strategy: str = "first",
    aggregate_sep: str = ", ",
    fuzzy_threshold: float = 0.85,
) -> pd.DataFrame:
    """Excel VLOOKUP-style left join with explicit match tracking."""

    left_keys = _coerce_lookup_keys(left_key, "left_key")
    right_keys = _coerce_lookup_keys(right_key, "right_key")
    if len(left_keys) != len(right_keys):
        raise ValueError("left_key and right_key must have the same number of fields")

    missing_left_keys = [key for key in left_keys if key not in left_df.columns]
    if missing_left_keys:
        raise KeyError(f"Left key not found: {missing_left_keys}")
    missing_right_keys = [key for key in right_keys if key not in right_df.columns]
    if missing_right_keys:
        raise KeyError(f"Right key not found: {missing_right_keys}")

    missing_fields = [field for field in fields if field not in right_df.columns]
    if missing_fields:
        raise KeyError(f"Lookup fields not found in right table: {missing_fields}")

    if match_mode not in SUPPORTED_MATCH_MODES:
        raise ValueError(f"Unsupported lookup match_mode: {match_mode}")
    if duplicate_strategy not in SUPPORTED_DUPLICATE_STRATEGIES:
        raise ValueError(f"Unsupported duplicate_strategy: {duplicate_strategy}")

    field_aliases = field_aliases or {}
    left = left_df.copy()
    right = right_df[_unique_columns([*right_keys, *fields])].copy()
    left["_lookup_join_key"] = _build_lookup_keys(left, left_keys, match_mode)
    right["_lookup_join_key"] = _build_lookup_keys(right, right_keys, match_mode)
    left["_lookup_original_key_blank"] = left["_lookup_join_key"].eq("")
    right = right[~right["_lookup_join_key"].eq("")]
    right_match_counts = right["_lookup_join_key"].value_counts()
    prepared_right = _resolve_duplicate_keys(
        right,
        fields=fields,
        duplicate_strategy=duplicate_strategy,
        aggregate_sep=aggregate_sep,
    )

    if match_mode == "fuzzy":
        fuzzy_matches = _best_fuzzy_matches(
            left["_lookup_join_key"],
            prepared_right["_lookup_join_key"],
            threshold=fuzzy_threshold,
        )
        left["_lookup_join_key"] = fuzzy_matches["match_key"]
        left["_lookup_match_confidence"] = fuzzy_matches["confidence"]
    else:
        prepared_keys = set(prepared_right["_lookup_join_key"].astype(str).tolist())
        left["_lookup_match_confidence"] = left["_lookup_join_key"].map(
            lambda value: 1.0 if value in prepared_keys and value else 0.0
        )

    prepared_right = prepared_right[["_lookup_join_key", *fields]].rename(columns=field_aliases)
    right_positions = try_duckdb_left_join_indices(
        left["_lookup_join_key"],
        prepared_right["_lookup_join_key"],
    )
    if right_positions is None:
        merged = left.merge(
            prepared_right,
            on="_lookup_join_key",
            how="left",
            suffixes=("", suffix),
            indicator="_lookup_status",
        )
    else:
        merged = left.copy()
        matched_positions = right_positions.notna()
        for source_field in fields:
            output_field = field_aliases.get(source_field, source_field)
            if output_field in merged.columns:
                output_field = f"{output_field}{suffix}"
            values = prepared_right[field_aliases.get(source_field, source_field)].reset_index(
                drop=True
            )
            merged[output_field] = right_positions.map(values)
        merged["_lookup_status"] = matched_positions.map(
            {True: "both", False: "left_only"}
        )
        merged.attrs["data_agent_engine"] = "duckdb"
    matched = merged["_lookup_status"].eq("both")
    duplicate_counts = merged["_lookup_join_key"].map(right_match_counts).fillna(0).astype(int)
    ambiguous = (
        matched & duplicate_counts.gt(1)
        if duplicate_strategy == "review"
        else pd.Series(False, index=merged.index)
    )
    merged[match_field] = matched
    if confidence_field:
        confidence = merged["_lookup_match_confidence"].where(matched, 0.0).astype(float)
        merged[confidence_field] = confidence.where(~ambiguous, confidence.clip(upper=0.65))
    if ambiguity_field:
        merged[ambiguity_field] = ambiguous
    if explanation_field:
        merged[explanation_field] = [
            _match_explanation(
                is_matched=bool(is_matched),
                key_blank=bool(key_blank),
                ambiguous=bool(is_ambiguous),
                duplicate_count=int(duplicate_count),
                match_mode=match_mode,
                confidence=float(confidence),
            )
            for is_matched, key_blank, is_ambiguous, duplicate_count, confidence in zip(
                matched,
                merged["_lookup_original_key_blank"],
                ambiguous,
                duplicate_counts,
                merged["_lookup_match_confidence"],
                strict=True,
            )
        ]
    merged = merged.drop(
        columns=[
            "_lookup_status",
            "_lookup_join_key",
            "_lookup_original_key_blank",
            "_lookup_match_confidence",
        ]
    )

    if right_positions is not None:
        merged.attrs["data_agent_engine"] = "duckdb"
    return merged


def _coerce_lookup_keys(key: str | list[str], label: str) -> list[str]:
    if isinstance(key, str):
        keys = [key]
    else:
        keys = list(key)
    if not keys or any(not item for item in keys):
        raise ValueError(f"{label} cannot be empty")
    return keys


def _unique_columns(columns: list[str]) -> list[str]:
    result = []
    for column in columns:
        if column not in result:
            result.append(column)
    return result


def _build_lookup_keys(df: pd.DataFrame, keys: list[str], match_mode: str) -> pd.Series:
    parts = [_build_lookup_key(df[key], match_mode) for key in keys]
    combined = parts[0].copy()
    blank_mask = parts[0].eq("")
    for part in parts[1:]:
        combined = combined + "\x1f" + part
        blank_mask = blank_mask | part.eq("")
    combined = combined.mask(blank_mask, "")
    return combined


def _build_lookup_key(series: pd.Series, match_mode: str) -> pd.Series:
    if match_mode == "exact":
        return series.fillna("").astype(str)
    if match_mode == "trim":
        return series.fillna("").astype(str).str.strip()
    if match_mode in {"case_insensitive", "normalized_exact", "fuzzy"}:
        text = series.fillna("").astype(str)
        if match_mode in {"normalized_exact", "fuzzy"}:
            return text.map(normalize_key_value)
        return text.str.lower()
    raise ValueError(f"Unsupported lookup match_mode: {match_mode}")


def _resolve_duplicate_keys(
    right: pd.DataFrame,
    fields: list[str],
    duplicate_strategy: str,
    aggregate_sep: str,
) -> pd.DataFrame:
    duplicate_mask = right["_lookup_join_key"].duplicated(keep=False)
    if duplicate_strategy == "error" and duplicate_mask.any():
        duplicate_keys = sorted(right.loc[duplicate_mask, "_lookup_join_key"].unique().tolist())
        raise ValueError(f"Right lookup key contains duplicates: {duplicate_keys[:10]}")

    if duplicate_strategy in {"first", "review"}:
        return right.drop_duplicates(subset=["_lookup_join_key"], keep="first")

    if duplicate_strategy == "aggregate":
        aggregations = {
            field: lambda series: _aggregate_field_values(series, aggregate_sep)
            for field in fields
        }
    else:
        aggregations = {
            field: lambda series: _list_field_values(series, aggregate_sep)
            for field in fields
        }
    return right.groupby("_lookup_join_key", as_index=False).agg(aggregations)


def _aggregate_field_values(series: pd.Series, aggregate_sep: str):
    values = series.dropna()
    numeric = pd.to_numeric(values, errors="coerce")
    if len(values) > 0 and numeric.notna().all():
        return numeric.sum()
    return _list_field_values(series, aggregate_sep)


def _list_field_values(series: pd.Series, aggregate_sep: str) -> str:
    values = []
    for value in series.dropna().astype(str):
        cleaned = value.strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return aggregate_sep.join(values)


def _best_fuzzy_matches(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
) -> pd.DataFrame:
    """Return the best fuzzy match per left key with match confidence.

    This is the join hot path for fuzzy lookups. A naive implementation compares
    every left row against every right candidate with a fresh ``SequenceMatcher``,
    which is O(rows x candidates) and rebuilds the candidate index every call. We
    keep the exact same scoring (``ratio()`` with ``a=left, b=right`` and default
    autojunk) but make it cheap by:
      * scoring each distinct left value once, then mapping back onto every row;
      * reusing a single ``SequenceMatcher`` and setting each right candidate as
        ``seq2`` once (its junk/b2j index is built n_candidates times, not
        n_rows x n_candidates times);
      * pruning with ``real_quick_ratio``/``quick_ratio`` upper bounds so a full
        ``ratio()`` only runs when a candidate can still beat the current best.
    """

    left_str = left_keys.astype(str)
    right_values = [value for value in right_keys.dropna().astype(str).unique() if value]
    right_lookup = set(right_values)
    distinct_left = list(pd.unique(left_str))

    best_value: dict[str, str] = {}
    best_score: dict[str, float] = {}
    pending: list[str] = []
    for value in distinct_left:
        if not value:
            best_value[value] = ""
            best_score[value] = 0.0
        elif value in right_lookup:
            # Exact hit short-circuits, mirroring the previous behavior.
            best_value[value] = value
            best_score[value] = 1.0
        else:
            best_value[value] = ""
            best_score[value] = 0.0
            pending.append(value)

    if pending and right_values:
        matcher = SequenceMatcher()
        for candidate in right_values:
            matcher.set_seq2(candidate)
            for value in pending:
                current = best_score[value]
                matcher.set_seq1(value)
                if matcher.real_quick_ratio() <= current or matcher.quick_ratio() <= current:
                    continue
                score = matcher.ratio()
                if score > current:
                    best_score[value] = score
                    best_value[value] = candidate

    resolved_key: dict[str, str] = {}
    resolved_confidence: dict[str, float] = {}
    for value in distinct_left:
        score = best_score[value]
        resolved_key[value] = best_value[value] if score >= threshold else value
        resolved_confidence[value] = round(score, 4)

    return pd.DataFrame(
        {
            "match_key": left_str.map(resolved_key),
            "confidence": left_str.map(resolved_confidence),
        },
        index=left_keys.index,
    )


def _match_explanation(
    is_matched: bool,
    key_blank: bool,
    ambiguous: bool,
    duplicate_count: int,
    match_mode: str,
    confidence: float,
) -> str:
    if key_blank:
        return "匹配失败：左表 key 为空或组合 key 不完整"
    if not is_matched:
        if match_mode == "fuzzy" and confidence > 0:
            return f"匹配失败：最高模糊相似度 {confidence:.2f} 未达到阈值"
        return "匹配失败：未在右表找到对应 key"
    if ambiguous:
        return f"匹配存在歧义：右表 key 命中 {duplicate_count} 条，已取首条并标记复核"
    if match_mode == "fuzzy":
        return f"模糊匹配成功，置信度 {confidence:.2f}"
    return "匹配成功"
