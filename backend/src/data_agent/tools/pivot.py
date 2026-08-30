import pandas as pd

from data_agent.schemas.job import PivotMetric
from data_agent.tools.duckdb_runner import try_duckdb_aggregate
from data_agent.utils.numeric import to_business_numeric

# Aggregations that only make sense over numbers.
_NUMERIC_AGGREGATIONS = frozenset({"sum", "mean", "median", "min", "max"})


def build_pivot(df: pd.DataFrame, group_by: list[str], metrics: list[PivotMetric]) -> pd.DataFrame:
    """Build a pivot-style aggregate table."""

    if df.empty:
        return pd.DataFrame(columns=[*group_by, *(metric.output_name for metric in metrics)])

    missing_group_fields = [field for field in group_by if field not in df.columns]
    if missing_group_fields:
        raise KeyError(f"Pivot group fields not found: {missing_group_fields}")

    accelerated = try_duckdb_aggregate(df, group_by, metrics)
    if accelerated is not None:
        return accelerated

    grouped = df.groupby(group_by, dropna=False)
    parts = []

    for metric in metrics:
        if metric.agg == "count":
            series = grouped.size()
        else:
            if metric.column is None:
                raise ValueError(f"Metric {metric.output_name} requires a column")
            if metric.column not in df.columns:
                raise KeyError(f"Metric column not found: {metric.column}")
            column = df[metric.column]
            if metric.agg in _NUMERIC_AGGREGATIONS and column.dtype == object:
                # Real business columns hold junk: a repeated header row puts the
                # literal "金额" in the amount column, a 合计 row puts a label in it.
                # pandas then sums an object column by concatenating strings and dies
                # on the first number. Excel's SUM simply skips text, so match that
                # rather than failing the whole delivery over one stray cell.
                #
                # Plain to_numeric skips far more than Excel does, though: it reads
                # "1,234" and "(567)" as nothing, and a column of those summed to a
                # confident, entirely wrong 0. Parse the notation the user wrote in.
                column = to_business_numeric(column)
                series = getattr(column.groupby([df[field] for field in group_by],
                                                dropna=False), metric.agg)()
            else:
                series = getattr(grouped[metric.column], metric.agg)()

        parts.append(series.rename(metric.output_name))

    return pd.concat(parts, axis=1).reset_index()


def build_crosstab(
    df: pd.DataFrame,
    group_by: list[str],
    column_field: str,
    metric: PivotMetric,
) -> pd.DataFrame:
    """Rows × columns, the shape people mean by 「透视表」.

    「行是城市，列是月份，值是金额」 is a different deliverable from a grouped list of
    (城市, 月份, 金额) — same numbers, but one is read across and the other down, and a
    report laid out the wrong way is a report nobody uses.

    Column headers come from the data, so they are values rather than field names. A
    key that appears in no row of some group leaves a 0, not a blank: 「that city had
    no March sales」 is a number, the same answer Excel's pivot gives.
    """

    if df.empty or not group_by or column_field not in df.columns:
        return pd.DataFrame()

    working = df.copy()
    if metric.agg in _NUMERIC_AGGREGATIONS and metric.column:
        working[metric.column] = to_business_numeric(working[metric.column])

    if metric.agg == "count":
        table = working.pivot_table(
            index=group_by, columns=column_field, aggfunc="size", fill_value=0
        )
    else:
        table = working.pivot_table(
            index=group_by,
            columns=column_field,
            values=metric.column,
            aggfunc=metric.agg,
            fill_value=0,
        )
    table.columns = [str(column) for column in table.columns]
    return table.reset_index()
