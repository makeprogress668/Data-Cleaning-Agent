"""Evidence-gated DuckDB execution for large relational operations."""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from data_agent.schemas.job import ConditionConfig, PivotMetric

logger = logging.getLogger(__name__)

_NUMERIC_AGGREGATIONS = frozenset({"sum", "mean", "median", "min", "max"})
_SUPPORTED_AGGREGATIONS = _NUMERIC_AGGREGATIONS | {
    "count",
    "nunique",
}


def duckdb_min_rows() -> int:
    raw = os.environ.get("DATA_AGENT_DUCKDB_MIN_ROWS", "100000").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 100000


def try_duckdb_aggregate(
    frame: pd.DataFrame,
    group_by: list[str],
    metrics: list[PivotMetric],
) -> pd.DataFrame | None:
    """Run a semantics-safe aggregate in DuckDB, otherwise request pandas fallback."""

    if len(frame) < duckdb_min_rows() or not group_by or not metrics:
        return None
    if any(field not in frame.columns for field in group_by):
        return None
    for metric in metrics:
        if metric.agg not in _SUPPORTED_AGGREGATIONS:
            return None
        if metric.agg != "count" and (
            metric.column is None or metric.column not in frame.columns
        ):
            return None
        # Object columns may contain business notation such as 1.2万 or (567).
        # The shared pandas path deliberately parses those values; SQL casting would
        # silently change the delivered total, so it is not an eligible pushdown.
        if (
            metric.agg in _NUMERIC_AGGREGATIONS
            and metric.column is not None
            and not pd.api.types.is_numeric_dtype(frame[metric.column])
        ):
            return None
    try:
        import duckdb
    except ImportError:
        return None

    connection = duckdb.connect()
    try:
        connection.register("input_frame", frame)
        dimensions = ", ".join(_identifier(field) for field in group_by)
        expressions = ", ".join(_metric_expression(metric) for metric in metrics)
        result = connection.execute(
            f"SELECT {dimensions}, {expressions} "  # noqa: S608 - identifiers are quoted
            f"FROM input_frame GROUP BY {dimensions} ORDER BY {dimensions}"
        ).fetch_df()
    except Exception as exc:  # noqa: BLE001 - fallback is the compatibility contract
        logger.warning("DuckDB 聚合下推失败，回退 pandas: %s", exc)
        return None
    finally:
        connection.close()
    for field in group_by:
        if result[field].isna().any():
            result[field] = result[field].where(result[field].notna(), float("nan"))
    result.attrs["data_agent_engine"] = "duckdb"
    return result


def try_duckdb_condition(
    frame: pd.DataFrame,
    condition: ConditionConfig,
) -> pd.Series | None:
    """Evaluate common large-table filters in DuckDB and preserve pandas row order."""

    if len(frame) < duckdb_min_rows() or condition.field not in frame.columns:
        return None
    expression, parameters = _condition_expression(condition)
    if expression is None:
        return None
    try:
        import duckdb
    except ImportError:
        return None

    registered = frame[[condition.field]].copy()
    registered.insert(0, "_row_position", range(len(frame)))
    connection = duckdb.connect()
    try:
        connection.register("input_frame", registered)
        matched = connection.execute(
            f"SELECT _row_position FROM input_frame WHERE {expression}",  # noqa: S608
            parameters,
        ).fetch_df()
    except Exception as exc:  # noqa: BLE001 - fallback is the compatibility contract
        logger.warning("DuckDB 过滤下推失败，回退 pandas: %s", exc)
        return None
    finally:
        connection.close()
    positions = set(matched["_row_position"].astype(int).tolist())
    mask = pd.Series(
        (position in positions for position in range(len(frame))),
        index=frame.index,
        dtype=bool,
        name=condition.field,
    )
    mask.attrs["data_agent_engine"] = "duckdb"
    return mask


def try_duckdb_left_join_indices(
    left_keys: pd.Series,
    right_keys: pd.Series,
) -> pd.Series | None:
    """Resolve a large one-to-one left join lazily and return right row positions.

    Lookup key normalization remains in the shared pandas code because it is part of
    the product's matching contract. DuckDB owns the expensive relational operator;
    only the matched row positions cross back into pandas at the delivery boundary.
    The right side must already be unique, which keeps row count and order stable.
    """

    if len(left_keys) + len(right_keys) < duckdb_min_rows():
        return None
    if right_keys.duplicated().any():
        return None
    try:
        import duckdb
    except ImportError:
        return None

    left = pd.DataFrame(
        {
            "_left_position": range(len(left_keys)),
            "_lookup_join_key": left_keys.reset_index(drop=True),
        }
    )
    right = pd.DataFrame(
        {
            "_right_position": range(len(right_keys)),
            "_lookup_join_key": right_keys.reset_index(drop=True),
        }
    )
    connection = duckdb.connect()
    try:
        connection.register("left_keys", left)
        connection.register("right_keys", right)
        positions = connection.execute(
            """
            SELECT r._right_position
            FROM left_keys AS l
            LEFT JOIN right_keys AS r
              ON l._lookup_join_key <> ''
             AND l._lookup_join_key = r._lookup_join_key
            ORDER BY l._left_position
            """
        ).fetch_df()["_right_position"]
    except Exception as exc:  # noqa: BLE001 - fallback is the compatibility contract
        logger.warning("DuckDB 关联下推失败，回退 pandas: %s", exc)
        return None
    finally:
        connection.close()
    result = pd.Series(positions, index=left_keys.index, dtype="Int64")
    result.attrs["data_agent_engine"] = "duckdb"
    return result


def _metric_expression(metric: PivotMetric) -> str:
    alias = _identifier(metric.output_name)
    if metric.agg == "count":
        return f"count(*) AS {alias}"
    column = _identifier(str(metric.column))
    if metric.agg == "sum":
        return f"coalesce(sum({column}), 0) AS {alias}"
    if metric.agg == "nunique":
        return f"count(DISTINCT {column}) AS {alias}"
    function = {
        "mean": "avg",
        "median": "median",
        "min": "min",
        "max": "max",
    }[metric.agg]
    return f"{function}({column}) AS {alias}"


def _condition_expression(condition: ConditionConfig) -> tuple[str | None, list[Any]]:
    column = _identifier(condition.field)
    op = condition.op
    if op == "is_null":
        return f"{column} IS NULL", []
    if op == "not_null":
        return f"{column} IS NOT NULL", []
    if op in {"is_blank", "not_blank"}:
        blank = f"({column} IS NULL OR trim(cast({column} AS varchar)) = '')"
        return (f"NOT {blank}" if op == "not_blank" else blank), []
    if op in {"gt", "gte", "lt", "lte"}:
        operator = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        return f"try_cast({column} AS double) {operator} ?", [float(condition.value)]
    text = f"trim(cast(coalesce({column}, '') AS varchar))"
    if not condition.case_sensitive:
        text = f"lower({text})"
    value = str(condition.value).strip()
    values = [str(item).strip() for item in condition.values]
    if not condition.case_sensitive:
        value = value.lower()
        values = [item.lower() for item in values]
    if op in {"equals", "not_equals"}:
        operator = "<>" if op == "not_equals" else "="
        return f"{text} {operator} ?", [value]
    if op in {"contains", "not_contains"}:
        expression = f"contains({text}, ?)"
        return (f"NOT {expression}" if op == "not_contains" else expression), [value]
    if op in {"in", "not_in"} and values:
        placeholders = ", ".join("?" for _value in values)
        expression = f"{text} IN ({placeholders})"
        return (f"NOT ({expression})" if op == "not_in" else expression), values
    return None, []


def _identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'
