from __future__ import annotations

import logging
import math
import re
from typing import Any

import pandas as pd

from data_agent.schemas.job import FormulaConfig
from data_agent.tools.conditions import evaluate_condition

logger = logging.getLogger(__name__)


def apply_formulas(df: pd.DataFrame, formulas: list[FormulaConfig]) -> pd.DataFrame:
    result = df.copy()
    result.attrs.pop("data_agent_last_engine", None)
    for formula in formulas:
        try:
            values = evaluate_formula(result, formula)
            engine = values.attrs.get("data_agent_engine")
            if formula.op in {"filter_rows", "dedupe"}:
                keep_mask = values.fillna(False).astype(bool)
                result = result.loc[keep_mask].copy()
            else:
                result[formula.output] = values
            if engine:
                result.attrs["data_agent_last_engine"] = engine
        except Exception as exc:  # noqa: BLE001 - per-formula isolation
            if formula.required:
                raise ValueError(
                    f"必需公式 {formula.output}（{formula.op}）执行失败：{exc}"
                ) from exc
            logger.warning(
                "跳过可选公式 %s（%s）：%s",
                getattr(formula, "output", "?"),
                getattr(formula, "op", "?"),
                exc,
            )
    return result


def evaluate_formula(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    op = formula.op

    if op == "copy":
        return _column(df, formula.source)
    if op == "literal":
        return pd.Series([formula.value] * len(df), index=df.index)
    if op == "trim":
        return _column(df, formula.source).fillna("").astype(str).str.strip()
    if op == "upper":
        return _column(df, formula.source).fillna("").astype(str).str.upper()
    if op == "lower":
        return _column(df, formula.source).fillna("").astype(str).str.lower()
    if op == "concat":
        return _concat(df, formula.columns, formula.sep)
    if op == "coalesce":
        return _coalesce(df, formula.columns, formula.default)
    if op == "map":
        return _column(df, formula.source).map(formula.mapping).fillna(formula.default)
    if op == "if_else":
        if formula.condition is None:
            raise ValueError(f"Formula {formula.output} requires condition")
        mask = evaluate_condition(df, formula.condition)
        return pd.Series(
            [formula.true_value if matched else formula.false_value for matched in mask],
            index=df.index,
        )
    if op == "ifs":
        return _ifs(df, formula)
    if op in {"and", "or"}:
        return _logical(df, formula, op)
    if op == "not":
        return ~_single_logical_mask(df, formula)
    if op in {"add", "subtract", "multiply", "divide"}:
        return _arithmetic(df, formula.columns, op)
    if op == "is_blank":
        source = _column(df, formula.source)
        return source.isna() | source.astype(str).str.strip().eq("")
    if op == "equals":
        return _column(df, formula.source).astype(str).str.strip().eq(str(formula.value))
    if op == "isin":
        values = [str(value) for value in formula.values]
        return _column(df, formula.source).astype(str).isin(values)
    if op == "contains":
        return _column(df, formula.source).astype(str).str.contains(str(formula.value), na=False)
    if op == "iferror":
        return _iferror(df, formula.source, formula.default)
    if op == "ifna":
        return _ifna(df, formula.source, formula.default)
    if op == "countif":
        return _countif(df, formula)
    if op == "sumif":
        return _sumif(df, formula)
    if op == "countifs":
        return _countifs(df, formula)
    if op == "sumifs":
        return _sumifs(df, formula)
    if op == "filter_rows":
        return _multi_criteria_mask(df, formula)
    if op == "dedupe":
        return _dedupe(df, formula)
    if op == "sort_rank":
        return _sort_rank(df, formula)
    if op == "top_n":
        return _top_n(df, formula)
    if op == "date_diff":
        return _date_diff(df, formula)
    if op == "fill_down":
        return _column(df, formula.source).ffill()
    if op in {"xlookup", "index_match"}:
        return _lookup_formula(df, formula)
    if op == "left":
        return _text_slice(df, formula.source, start=0, length=_int_value(formula.value))
    if op == "right":
        return _right(df, formula.source, _int_value(formula.value))
    if op == "mid":
        return _mid(df, formula)
    if op == "len":
        return _column(df, formula.source).fillna("").astype(str).str.len()
    if op == "substitute":
        return _substitute(df, formula)
    if op == "replace":
        return _replace_text(df, formula)
    if op == "text":
        return _text_format(df, formula.source, formula.value)
    if op == "date":
        return _date(df, formula)
    if op in {"year", "month", "day"}:
        return _date_part(df, formula.source, op)
    if op == "today":
        return _constant_datetime(df, formula.value, normalize=True)
    if op == "now":
        return _constant_datetime(df, formula.value, normalize=False)
    if op == "round":
        return pd.to_numeric(_column(df, formula.source), errors="coerce").round(
            _int_value(formula.value, default=0)
        )
    if op == "roundup":
        return _round_direction(df, formula, direction="up")
    if op == "rounddown":
        return _round_direction(df, formula, direction="down")
    if op == "abs":
        return pd.to_numeric(_column(df, formula.source), errors="coerce").abs()
    if op == "bucket":
        return _bucket(df, formula)
    if op == "regex_extract":
        return _regex_extract(df, formula)
    if op == "regex_replace":
        return _regex_replace(df, formula)
    if op == "split":
        return _split(df, formula)
    if op == "join":
        return _concat(df, formula.columns, formula.sep)
    if op == "textjoin":
        return _textjoin(df, formula)
    if op == "remove_special_chars":
        return _remove_special_chars(df, formula)
    if op == "normalize_width":
        return _normalize_width_series(_column(df, formula.source))

    raise ValueError(f"Unsupported formula op: {op}")


def _column(df: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        raise ValueError("Formula requires source column")
    if column not in df.columns:
        raise KeyError(f"Formula source column not found: {column}")
    return df[column]


def _concat(df: pd.DataFrame, columns: list[str], sep: str) -> pd.Series:
    if not columns:
        raise ValueError("concat formula requires columns")
    _ensure_columns(df, columns)
    return df[columns].fillna("").astype(str).agg(sep.join, axis=1)


def _coalesce(df: pd.DataFrame, columns: list[str], default: Any) -> pd.Series:
    if not columns:
        raise ValueError("coalesce formula requires columns")
    _ensure_columns(df, columns)

    result = pd.Series([default] * len(df), index=df.index)
    for column in reversed(columns):
        values = df[column]
        non_blank = values.notna() & ~values.astype(str).str.strip().eq("")
        result = result.where(~non_blank, values)
    return result


def _arithmetic(df: pd.DataFrame, columns: list[str], op: str) -> pd.Series:
    if len(columns) != 2:
        raise ValueError(f"{op} formula requires exactly two columns")
    _ensure_columns(df, columns)

    left = pd.to_numeric(df[columns[0]], errors="coerce")
    right = pd.to_numeric(df[columns[1]], errors="coerce")

    if op == "add":
        return left + right
    if op == "subtract":
        return left - right
    if op == "multiply":
        return left * right
    return left / right


def _ifs(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if not formula.conditions:
        raise ValueError("ifs formula requires conditions")
    if len(formula.values) < len(formula.conditions):
        raise ValueError("ifs formula requires values for every condition")

    result = pd.Series([formula.default] * len(df), index=df.index)
    unresolved = pd.Series([True] * len(df), index=df.index)
    # The guard above already rejects too-few values; surplus values are ignored on
    # purpose, so pair only up to the condition count.
    for condition, value in zip(formula.conditions, formula.values, strict=False):
        mask = evaluate_condition(df, condition).fillna(False).astype(bool) & unresolved
        result = result.where(~mask, value)
        unresolved = unresolved & ~mask
    return result


def _logical(df: pd.DataFrame, formula: FormulaConfig, op: str) -> pd.Series:
    masks = _logical_masks(df, formula)
    result = pd.Series([op == "and"] * len(df), index=df.index)
    for mask in masks:
        if op == "and":
            result = result & mask
        else:
            result = result | mask
    return result


def _single_logical_mask(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    masks = _logical_masks(df, formula)
    if len(masks) > 1:
        return _logical(df, formula, "and")
    return masks[0]


def _logical_masks(df: pd.DataFrame, formula: FormulaConfig) -> list[pd.Series]:
    masks = []
    if formula.condition is not None:
        masks.append(evaluate_condition(df, formula.condition))
    masks.extend(evaluate_condition(df, condition) for condition in formula.conditions)
    if not masks and formula.source:
        masks.append(_truthy_series(_column(df, formula.source)))
    if not masks:
        raise ValueError(f"{formula.op} formula requires condition(s) or source")
    return [mask.fillna(False).astype(bool) for mask in masks]


def _truthy_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    text = series.fillna("").astype(str).str.strip().str.lower()
    return text.isin({"true", "1", "yes", "y", "是", "对"})


def _iferror(df: pd.DataFrame, source: str | None, default: Any) -> pd.Series:
    values = _column(df, source)
    text = values.fillna("").astype(str).str.strip()
    numeric = pd.to_numeric(values, errors="coerce")
    error_tokens = {"#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"}
    invalid = values.isna() | text.isin(error_tokens) | numeric.abs().eq(float("inf"))
    return values.where(~invalid, default)


def _ifna(df: pd.DataFrame, source: str | None, default: Any) -> pd.Series:
    values = _column(df, source)
    text = values.fillna("").astype(str).str.strip().str.upper()
    invalid = values.isna() | text.eq("#N/A")
    return values.where(~invalid, default)


def _countif(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    mask = _criteria_mask(df, formula)
    count = int(mask.sum())
    return pd.Series([count] * len(df), index=df.index)


def _sumif(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if not formula.columns:
        raise ValueError("sumif formula requires columns=[sum_range]")
    mask = _criteria_mask(df, formula)
    values = pd.to_numeric(_column(df, formula.columns[0]), errors="coerce")
    total = values.where(mask, 0).sum()
    return pd.Series([total] * len(df), index=df.index)


def _countifs(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    mask = _multi_criteria_mask(df, formula)
    count = int(mask.sum())
    return pd.Series([count] * len(df), index=df.index)


def _sumifs(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if not formula.columns:
        raise ValueError("sumifs formula requires columns=[sum_range]")
    mask = _multi_criteria_mask(df, formula)
    values = pd.to_numeric(_column(df, formula.columns[0]), errors="coerce")
    total = values.where(mask, 0).sum()
    return pd.Series([total] * len(df), index=df.index)


def _multi_criteria_mask(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    masks = []
    if formula.condition is not None:
        masks.append(evaluate_condition(df, formula.condition))
    masks.extend(evaluate_condition(df, condition) for condition in formula.conditions)
    if not masks:
        masks.append(_criteria_mask(df, formula))

    result = pd.Series([True] * len(df), index=df.index)
    engine = None
    for mask in masks:
        engine = engine or mask.attrs.get("data_agent_engine")
        result = result & mask
    if engine:
        result.attrs["data_agent_engine"] = engine
    return result


def _criteria_mask(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if formula.condition is not None:
        return evaluate_condition(df, formula.condition)
    values = _column(df, formula.source).astype(str).str.strip()
    return values.eq(str(formula.value).strip())


def _dedupe(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    keys = formula.columns or ([formula.source] if formula.source else [])
    if not keys:
        raise ValueError("dedupe formula requires columns or source")
    _ensure_columns(df, keys)

    strategy = str(formula.value or "first").lower()
    if strategy in {"first", "last", "all_duplicates"}:
        keep = False if strategy == "all_duplicates" else strategy
        duplicated = df.duplicated(subset=keys, keep=keep)
        return duplicated if strategy == "all_duplicates" else ~duplicated

    if strategy not in {"latest", "earliest"}:
        raise ValueError(f"Unsupported dedupe strategy: {strategy}")
    if formula.source is None:
        raise ValueError("latest/earliest dedupe requires source as order column")
    if formula.source in keys:
        raise ValueError("dedupe order source must not also be a key column")

    order_values = _sortable_values(_column(df, formula.source))
    sorted_index = (
        df.assign(_dedupe_order=order_values)
        .sort_values("_dedupe_order", ascending=strategy == "earliest")
        .drop_duplicates(subset=keys, keep="first")
        .index
    )
    return pd.Series(df.index.isin(sorted_index), index=df.index)


def _sort_rank(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    values = _sortable_values(_column(df, formula.source))
    ascending = _ascending(formula.value, default=False)
    if formula.columns:
        _ensure_columns(df, formula.columns)
        return values.groupby([df[column] for column in formula.columns]).rank(
            method="dense",
            ascending=ascending,
        )
    return values.rank(method="dense", ascending=ascending)


def _top_n(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    n = _int_value(formula.value, default=1)
    direction = formula.default if isinstance(formula.default, str) else "desc"
    rank_formula = formula.model_copy(update={"value": direction})
    return _sort_rank(df, rank_formula) <= n


def _date_diff(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    start = pd.to_datetime(_column(df, formula.source), errors="coerce")
    if formula.columns:
        _ensure_columns(df, [formula.columns[0]])
        end = pd.to_datetime(df[formula.columns[0]], errors="coerce")
        unit = str(formula.value or "days").lower()
    else:
        end = pd.to_datetime(formula.value, errors="coerce")
        unit = str(formula.default or "days").lower()

    delta = end - start
    if unit in {"day", "days", "d"}:
        return delta.dt.days
    if unit in {"hour", "hours", "h"}:
        return delta.dt.total_seconds() / 3600
    if unit in {"minute", "minutes", "m"}:
        return delta.dt.total_seconds() / 60
    raise ValueError(f"Unsupported date_diff unit: {unit}")


def _lookup_formula(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if len(formula.columns) != 2:
        raise ValueError(f"{formula.op} formula requires columns=[lookup_array, return_array]")
    lookup_array, return_array = formula.columns
    _ensure_columns(df, [lookup_array, return_array])
    source = _column(df, formula.source).fillna("").astype(str).str.strip()
    lookup_keys = df[lookup_array].fillna("").astype(str).str.strip()
    lookup_map = (
        pd.DataFrame({"key": lookup_keys, "value": df[return_array]})
        .drop_duplicates(subset=["key"], keep="first")
        .set_index("key")["value"]
        .to_dict()
    )
    return source.map(lookup_map).fillna(formula.default)


def _text_slice(
    df: pd.DataFrame,
    source: str | None,
    start: int,
    length: int,
) -> pd.Series:
    text = _column(df, source).fillna("").astype(str)
    return text.str.slice(start, start + length)


def _right(df: pd.DataFrame, source: str | None, length: int) -> pd.Series:
    text = _column(df, source).fillna("").astype(str)
    return text.str[-length:]


def _mid(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if len(formula.values) < 2:
        raise ValueError("mid formula requires values=[start, length]")
    start = max(_int_value(formula.values[0]) - 1, 0)
    length = _int_value(formula.values[1])
    return _text_slice(df, formula.source, start, length)


def _substitute(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if len(formula.values) >= 2:
        old, new = formula.values[0], formula.values[1]
    else:
        old, new = formula.value, "" if formula.default is None else formula.default
    return (
        _column(df, formula.source)
        .fillna("")
        .astype(str)
        .str.replace(str(old), str(new), regex=False)
    )


def _replace_text(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if len(formula.values) < 2:
        raise ValueError("replace formula requires values=[start, length]")
    start = max(_int_value(formula.values[0]) - 1, 0)
    length = max(_int_value(formula.values[1]), 0)
    replacement = "" if formula.value is None else str(formula.value)

    def replace_one(value: Any) -> str:
        text = "" if pd.isna(value) else str(value)
        return text[:start] + replacement + text[start + length :]

    return _column(df, formula.source).map(replace_one)


def _text_format(df: pd.DataFrame, source: str | None, pattern: Any) -> pd.Series:
    values = _column(df, source)
    fmt = str(pattern or "")
    if any(token in fmt.lower() for token in ("y", "m", "d")):
        strftime_pattern = _excel_date_pattern(fmt)
        return pd.to_datetime(values, errors="coerce").dt.strftime(strftime_pattern).fillna("")
    if fmt:
        return values.map(lambda value: "" if pd.isna(value) else format(value, fmt))
    return values.fillna("").astype(str)


def _date(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if len(formula.columns) == 3:
        _ensure_columns(df, formula.columns)
        date_parts = {
            "year": pd.to_numeric(df[formula.columns[0]], errors="coerce"),
            "month": pd.to_numeric(df[formula.columns[1]], errors="coerce"),
            "day": pd.to_numeric(df[formula.columns[2]], errors="coerce"),
        }
        return pd.to_datetime(date_parts, errors="coerce")
    return pd.to_datetime(_column(df, formula.source), errors="coerce")


def _date_part(df: pd.DataFrame, source: str | None, part: str) -> pd.Series:
    dates = pd.to_datetime(_column(df, source), errors="coerce")
    if part == "year":
        return dates.dt.year
    if part == "month":
        return dates.dt.month
    return dates.dt.day


def _constant_datetime(df: pd.DataFrame, value: Any, normalize: bool) -> pd.Series:
    timestamp = pd.Timestamp(value) if value is not None else pd.Timestamp.now()
    if normalize:
        timestamp = timestamp.normalize()
    return pd.Series([timestamp] * len(df), index=df.index)


def _round_direction(df: pd.DataFrame, formula: FormulaConfig, direction: str) -> pd.Series:
    digits = _int_value(formula.value, default=0)
    scale = 10**digits
    values = pd.to_numeric(_column(df, formula.source), errors="coerce")

    def round_one(value: float) -> float:
        if pd.isna(value):
            return value
        scaled = float(value) * scale
        if direction == "up":
            rounded = math.ceil(scaled) if scaled >= 0 else math.floor(scaled)
        else:
            rounded = math.floor(scaled) if scaled >= 0 else math.ceil(scaled)
        return rounded / scale

    return values.map(round_one)


def _bucket(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    values = pd.to_numeric(_column(df, formula.source), errors="coerce")
    thresholds = [float(value) for value in formula.values]

    def bucket_value(value):
        if pd.isna(value):
            return formula.default
        for threshold in thresholds:
            if value <= threshold:
                return _mapping_value(formula.mapping, threshold, threshold)
        return formula.default

    return values.map(bucket_value)


def _regex_extract(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    pattern = str(formula.value)
    group_index = _int_value(formula.values[0], default=0) if formula.values else 0
    extracted = _column(df, formula.source).fillna("").astype(str).str.extract(pattern)
    default = "" if formula.default is None else formula.default
    if extracted.empty:
        return pd.Series([default] * len(df), index=df.index)
    if group_index >= len(extracted.columns):
        raise ValueError(f"regex_extract group index out of range: {group_index}")
    return extracted.iloc[:, group_index].fillna(default)


def _regex_replace(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    replacement = "" if formula.default is None else str(formula.default)
    return (
        _column(df, formula.source)
        .fillna("")
        .astype(str)
        .str.replace(str(formula.value), replacement, regex=True)
    )


def _split(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    index = _int_value(formula.value, default=0)
    parts = _column(df, formula.source).fillna("").astype(str).str.split(formula.sep)
    default = "" if formula.default is None else formula.default
    return parts.map(lambda values: values[index] if index < len(values) else default)


def _textjoin(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    if not formula.columns:
        raise ValueError("textjoin formula requires columns")
    _ensure_columns(df, formula.columns)
    ignore_empty = bool(formula.value) if formula.value is not None else True

    def join_row(row: pd.Series) -> str:
        values = []
        for value in row.tolist():
            text = "" if pd.isna(value) else str(value)
            if ignore_empty and text == "":
                continue
            values.append(text)
        return formula.sep.join(values)

    return df[formula.columns].apply(join_row, axis=1)


def _remove_special_chars(df: pd.DataFrame, formula: FormulaConfig) -> pd.Series:
    pattern = str(formula.value or r"[^0-9A-Za-z\u4e00-\u9fff]+")
    replacement = "" if formula.default is None else str(formula.default)
    return _column(df, formula.source).fillna("").astype(str).str.replace(
        pattern,
        replacement,
        regex=True,
    )


def _normalize_width_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).map(_normalize_width_text)


def _normalize_width_text(value: str) -> str:
    value = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", value).strip()
    chars = []
    for char in value:
        code = ord(char)
        if code == 12288:
            chars.append(" ")
        elif 65281 <= code <= 65374:
            chars.append(chr(code - 65248))
        else:
            chars.append(char)
    return "".join(chars).strip()


def _sortable_values(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().any():
        return numeric

    dates = pd.to_datetime(series, errors="coerce")
    if dates.notna().any():
        return dates.map(lambda value: value.timestamp() if pd.notna(value) else pd.NA)

    return series.fillna("").astype(str)


def _ascending(value: Any, default: bool) -> bool:
    if value is None:
        return default
    normalized = str(value).lower()
    if normalized in {"asc", "ascending", "true", "1"}:
        return True
    if normalized in {"desc", "descending", "false", "0"}:
        return False
    return default


def _int_value(value: Any, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("Formula requires integer value")
        return default
    return int(value)


def _excel_date_pattern(pattern: str) -> str:
    result = pattern
    replacements = {
        "yyyy": "%Y",
        "YYYY": "%Y",
        "yy": "%y",
        "YY": "%y",
        "mm": "%m",
        "MM": "%m",
        "dd": "%d",
        "DD": "%d",
    }
    for excel_token, strftime_token in replacements.items():
        result = result.replace(excel_token, strftime_token)
    return result


def _mapping_value(mapping: dict[str, Any], key: Any, default: Any) -> Any:
    candidates = [key, str(key)]
    if isinstance(key, float) and key.is_integer():
        candidates.append(str(int(key)))
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    return default


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise KeyError(f"Formula columns not found: {missing}")
