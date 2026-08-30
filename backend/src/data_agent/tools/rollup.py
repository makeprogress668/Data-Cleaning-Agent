"""Summarise a detail table back onto its master — Excel's cross-sheet SUMIF.

「把订单明细的金额按客户汇总到客户表」 is the other half of what people use VLOOKUP
for. VLOOKUP carries one matching value across; SUMIF carries the *total* of every
matching row. Without it a user can join customer names onto orders but cannot put
order totals onto customers, which is the direction most reports actually need.

Both halves already existed — group-and-aggregate, and match-by-key — but nothing
composed them, so the operation had no way to be asked for. This is that composition
and nothing more: build the per-key aggregate, then bring it across.

Rows are never added. An aggregate has one row per key by construction, so the master
table comes out with exactly the rows it went in with — unlike a plain join against a
detail table, which multiplies them.
"""

from __future__ import annotations

import pandas as pd

from data_agent.schemas.job import PivotMetric
from data_agent.tools.pivot import build_pivot


def rollup_to_master(
    master: pd.DataFrame,
    detail: pd.DataFrame,
    *,
    left_key: str,
    right_key: str,
    measure: str,
    agg: str,
    output: str,
    fill_missing: object = 0,
) -> pd.DataFrame:
    """Attach ``agg`` of ``measure`` per ``right_key`` onto ``master`` as ``output``.

    A master row whose key appears nowhere in the detail gets ``fill_missing`` — zero
    by default, because "this customer placed no orders" is a total of nothing, not an
    unknown. Excel's SUMIF answers 0 there too.
    """

    for frame, column, label in (
        (master, left_key, "master"),
        (detail, right_key, "detail"),
        (detail, measure, "detail"),
    ):
        if column not in frame.columns:
            raise KeyError(f"rollup column not found in {label}: {column}")

    metric = PivotMetric(column=measure, agg=agg, output_name=output)
    summary = build_pivot(detail, [right_key], [metric])
    lookup = summary.set_index(right_key)[output]

    result = master.copy()
    joined = result[left_key].map(lookup)
    result[output] = joined if fill_missing is None else joined.fillna(fill_missing)
    if summary.attrs.get("data_agent_engine"):
        result.attrs["data_agent_engine"] = summary.attrs["data_agent_engine"]
    return result
