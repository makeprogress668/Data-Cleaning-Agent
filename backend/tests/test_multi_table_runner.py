from pathlib import Path

import pandas as pd

from data_agent.pipelines import run_job
from data_agent.schemas.job import (
    AnomalyRuleConfig,
    ConditionConfig,
    FormulaConfig,
    JobConfig,
    LookupConfig,
    SourceConfig,
)


def test_run_job_supports_multi_table_lookup_and_formulas(tmp_path: Path) -> None:
    raw = tmp_path / "raw.xlsx"
    buildings = tmp_path / "buildings.xlsx"
    owners = tmp_path / "owners.xlsx"
    output = tmp_path / "result.xlsx"

    pd.DataFrame({"sitecode": ["S001", "S002"], "owner_code": ["O01", "O404"]}).to_excel(
        raw,
        index=False,
    )
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_status": ["活跃", "inactive"],
        }
    ).to_excel(buildings, index=False)
    pd.DataFrame({"owner_code": ["O01"], "owner_name": ["Alice"]}).to_excel(owners, index=False)

    job = JobConfig(
        sources={
            "raw": SourceConfig(path=raw),
            "buildings": SourceConfig(path=buildings),
            "owners": SourceConfig(path=owners),
        },
        base_table="raw",
        lookups=[
            LookupConfig(
                name="building",
                source_table="buildings",
                left_key="sitecode",
                right_key="sitecode",
                fields=["building_status"],
                match_field="_lookup_matched_building",
            ),
            LookupConfig(
                name="owner",
                source_table="owners",
                left_key="owner_code",
                right_key="owner_code",
                fields=["owner_name"],
                match_field="_lookup_matched_owner",
            ),
        ],
        formulas=[
            FormulaConfig(
                output="status_std",
                op="map",
                source="building_status",
                mapping={"活跃": "active", "inactive": "inactive"},
                default="unknown",
            )
        ],
        # Generic, config-driven anomaly rule replacing the old domain classifier:
        # a record is abnormal when its standardized status is not active. S001 maps
        # to "active" (clean); S002 maps to "inactive" (abnormal).
        anomaly_rules=[
            AnomalyRuleConfig(
                name="status_not_active",
                condition=ConditionConfig(field="status_std", op="not_equals", value="active"),
                reason="状态非活跃",
                severity="error",
            )
        ],
        export={"output_file": output, "include_source_tables": True},
    )

    result = run_job(job)

    assert result.cleaned_count == 1
    assert result.abnormal_count == 1
    assert result.output_file.exists()
    assert set(result.table_profile["table"]) == {"raw", "buildings", "owners"}

