import pandas as pd

from data_agent.tools.profiler import profile_dataset


def test_profile_dataset_discovers_lookup_relationship_candidates() -> None:
    tables = {
        "raw": pd.DataFrame({"sitecode": ["S001", "S001", "S002", "S003", "S404"]}),
        "buildings": pd.DataFrame({"sitecode": ["S001", "S002", "S003"]}),
    }

    result = profile_dataset(tables)
    relationships = result["relationship_candidates"]
    raw_to_buildings = relationships[
        relationships["left_table"].eq("raw") & relationships["right_table"].eq("buildings")
    ].iloc[0]

    assert raw_to_buildings["left_field"] == "sitecode"
    assert raw_to_buildings["right_field"] == "sitecode"
    assert raw_to_buildings["relationship_type"] == "N:1"
    assert raw_to_buildings["recommendation"] == "recommended_lookup"
    assert raw_to_buildings["unmatched_left_sample"] == "s404"


def test_profile_dataset_reports_join_expansion_risk() -> None:
    tables = {
        "raw": pd.DataFrame({"sitecode": ["S001", "S002"]}),
        "buildings": pd.DataFrame({"sitecode": ["S001", "S001", "S002"]}),
    }

    result = profile_dataset(tables)
    relationships = result["relationship_candidates"]
    raw_to_buildings = relationships[
        relationships["left_table"].eq("raw") & relationships["right_table"].eq("buildings")
    ].iloc[0]

    assert raw_to_buildings["relationship_type"] == "1:N"
    assert raw_to_buildings["right_duplicate_key_count"] == 1
    assert raw_to_buildings["right_duplicate_key_sample"] == "s001"
    assert raw_to_buildings["estimated_join_row_count"] == 3
    assert raw_to_buildings["join_row_growth_count"] == 1
    assert raw_to_buildings["join_will_expand"]
    assert raw_to_buildings["recommendation"] == "right_key_not_unique"
