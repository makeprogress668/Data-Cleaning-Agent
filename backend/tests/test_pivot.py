import pandas as pd

from data_agent.schemas.job import PivotMetric
from data_agent.tools.pivot import build_pivot


def test_build_pivot_counts_grouped_rows() -> None:
    df = pd.DataFrame({"abnormal_type": ["a", "a", "b"]})

    result = build_pivot(
        df,
        group_by=["abnormal_type"],
        metrics=[PivotMetric(agg="count", output_name="row_count")],
    )

    counts = dict(zip(result["abnormal_type"], result["row_count"], strict=True))
    assert counts == {"a": 2, "b": 1}
