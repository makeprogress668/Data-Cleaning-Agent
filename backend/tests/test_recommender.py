import pandas as pd

from data_agent.tools.profiler import profile_dataset
from data_agent.tools.recommender import build_recommendation_package


def test_recommender_builds_lookup_labels_and_executable_job_config() -> None:
    tables = {
        "raw_sitecode": pd.DataFrame(
            {
                "record_id": [1, 2, 3, 4],
                "sitecode": ["S001", "S001", "S002", "S404"],
                "owner_code": ["O01", "O02", "O99", "O03"],
                "amount": [100, 200, None, 300],
            }
        ),
        "buildings": pd.DataFrame(
            {
                "sitecode": ["S001", "S002"],
                "building_status": ["active", "inactive"],
            }
        ),
        "owners": pd.DataFrame(
            {
                "owner_code": ["O01", "O02", "O03"],
                "owner_name": ["Alice", "Bob", "Cindy"],
            }
        ),
    }
    profile_sheets = profile_dataset(tables)

    # Lookups are goal-driven now: a matching/merge goal enables cross-table lookup.
    recommendation_sheets, job_config = build_recommendation_package(
        tables,
        profile_sheets,
        goal="以 raw_sitecode 为主表，匹配关联补齐 building_status 和 owner_name",
    )

    lookup_names = set(recommendation_sheets["recommended_lookups"]["name"])
    labels = set(recommendation_sheets["label_taxonomy"]["label"])
    formula_outputs = {formula["output"] for formula in job_config["formulas"]}

    assert job_config["base_table"] == "raw_sitecode"
    assert "buildings_by_sitecode" in lookup_names
    assert "owners_by_owner_code" in lookup_names
    assert "buildings_by_sitecode_not_found" in labels
    assert "owners_by_owner_code_not_found" in labels
    assert "duplicate_sitecode" in labels
    # Zero-redundancy: the recommender no longer fabricates a record_key column.
    assert "record_key" not in formula_outputs
    assert "status_std" not in formula_outputs
    assert "is_active_status" not in formula_outputs
    assert job_config["lookups"][0]["match_mode"] == "normalized_exact"
    assert job_config["lookups"][0]["duplicate_strategy"] == "first"
    assert "matching_diagnostics" in recommendation_sheets
    assert "recommended_processing_steps" in recommendation_sheets
    assert "field_cleaning_plan" in recommendation_sheets
    assert "rule_impact_summary" in recommendation_sheets
    assert "label_impact_summary" in recommendation_sheets
    assert "record_label_preview" in recommendation_sheets
    assert "approval_checklist" in recommendation_sheets

    impact = recommendation_sheets["rule_impact_summary"].set_index("label")
    assert impact.loc["amount_null_values", "affected_count"] == 1
    assert impact.loc["owners_by_owner_code_not_found", "affected_count"] == 1
    assert impact.loc["buildings_by_sitecode_not_found", "affected_count"] == 1
    assert impact.loc["duplicate_sitecode", "affected_count"] == 2

    checklist = recommendation_sheets["approval_checklist"].set_index("item")
    assert checklist.loc["execution", "status"] == "ready"


def test_recommender_does_not_force_generic_status_into_active_model() -> None:
    tables = {
        "tickets": pd.DataFrame(
            {
                "ticket_id": ["T1", "T2"],
                "assignee_id": ["U1", "U2"],
                "ticket_status": ["open", "closed"],
            }
        ),
        "users": pd.DataFrame(
            {
                "assignee_id": ["U1", "U2"],
                "team": ["support", "ops"],
            }
        ),
    }
    profile_sheets = profile_dataset(tables)

    _, job_config = build_recommendation_package(
        tables,
        profile_sheets,
        goal="关联 users 表，把 team 匹配到 tickets 上",
    )

    formula_outputs = {formula["output"] for formula in job_config["formulas"]}
    # No fabricated derived columns, and generic status is never forced active.
    assert "record_key" not in formula_outputs
    assert "status_std" not in formula_outputs
    assert "is_active_status" not in formula_outputs


def test_recommender_suggests_duplicate_key_strategy() -> None:
    tables = {
        "raw": pd.DataFrame({"owner_code": ["O01", "O02"]}),
        "owners": pd.DataFrame(
            {
                "owner_code": ["O01", "O01", "O02"],
                "owner_name": ["Alice", "Alice Backup", "Bob"],
            }
        ),
    }
    profile_sheets = profile_dataset(tables)

    recommendation_sheets, job_config = build_recommendation_package(
        tables,
        profile_sheets,
        goal="以 raw 为主表，匹配关联补齐 owner_name",
    )

    recommended = recommendation_sheets["recommended_lookups"].iloc[0]
    assert recommended["name"] == "owners_by_owner_code"
    assert recommended["duplicate_strategy"] == "review"
    assert recommended["join_will_expand"]
    assert job_config["lookups"][0]["duplicate_strategy"] == "review"
    assert job_config["lookups"][0]["confidence_field"] == "_lookup_confidence_owners_by_owner_code"
    assert (
        job_config["lookups"][0]["explanation_field"]
        == "_lookup_explanation_owners_by_owner_code"
    )
    assert job_config["lookups"][0]["ambiguity_field"] == "_lookup_ambiguous_owners_by_owner_code"
