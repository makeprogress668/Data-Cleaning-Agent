import logging

import pandas as pd

from data_agent.tools.audit import (
    build_annotation_summary,
    build_annotation_tasks,
    build_annotation_taxonomy,
    build_record_audit,
)


def test_record_audit_logs_lookup_failure_details(caplog) -> None:
    abnormal = pd.DataFrame(
        {
            "_source_table": ["raw_main"],
            "_source_row_index": [3],
            "record_id": [4],
            "_lookup_matched_products_by_product_id": [False],
            "_lookup_matched_customers_by_customer_id": [True],
            "abnormal_type": ["products_by_product_id_not_found"],
        }
    )

    with caplog.at_level(logging.INFO, logger="data_agent.tools.audit"):
        audit = build_record_audit(
            abnormal,
            lookup_match_fields={
                "_lookup_matched_products_by_product_id": "products_by_product_id",
                "_lookup_matched_customers_by_customer_id": "customers_by_customer_id",
            },
        )

    row = audit.iloc[0]
    assert row["source_table"] == "raw_main"
    # Generic lookup-failure reason code (no domain-specific building code).
    assert row["reason_code"] == "LOOKUP_NOT_FOUND"
    assert row["hit_lookup_rules"] == "customers_by_customer_id"
    assert row["failed_lookup_rules"] == "products_by_product_id"
    assert "匹配失败审计" in caplog.text
    assert "failed_lookup_rules=products_by_product_id" in caplog.text


def test_annotation_workflow_defaults_to_pending_and_keeps_feedback_fields() -> None:
    audit = pd.DataFrame(
        {
            "source_table": ["raw_main", "raw_main"],
            "source_row_index": [3, 6],
            "label": ["products_by_product_id_not_found", "amount_null_values"],
            "reason_code": ["LOOKUP_NOT_FOUND", "PROFILE_NULL_VALUES"],
            "hit_rule": ["lookup", "amount_null_values"],
            "hit_lookup_rules": ["customers_by_customer_id", "products_by_product_id"],
            "failed_lookup_rules": ["products_by_product_id", "customers_by_customer_id"],
            "record_id": [4, 7],
            "record_key": ["4-A004", "7-A005"],
        }
    )

    tasks = build_annotation_tasks(audit)
    summary = build_annotation_summary(tasks)
    taxonomy = build_annotation_taxonomy(audit)

    lookup_label = "products_by_product_id_not_found"
    assert tasks["manual_status"].tolist() == ["pending", "pending"]
    assert tasks.set_index("label").loc[lookup_label, "severity"] == "error"
    # Lookup failures map to the generic mapping taxonomy.
    assert tasks.set_index("label").loc[lookup_label, "taxonomy_category"] == "lookup_mapping"
    assert "review_note" in tasks.columns
    assert "feedback_to_rule" in tasks.columns
    assert summary.set_index("label").loc[lookup_label, "task_count"] == 1
    assert taxonomy.set_index("label").loc["amount_null_values", "taxonomy_category"] == (
        "data_quality"
    )


def test_a_cell_is_never_labelled_twice_by_two_subsystems() -> None:
    """One problem, one label.

    An explicit rule and a built-in heuristic routinely notice the same cell, and the
    user was shown both ("amount 小于允许的最小值；金额为负"). The rule's wording —
    which came from the user's own requirement — wins; the heuristic's duplicate is
    dropped. Findings on fields no rule covers are still reported.
    """

    abnormal = pd.DataFrame(
        [
            {
                "_source_table": "orders",
                "_source_row_index": 0,
                "amount": -5,
                "note": "  x  ",
                "abnormal_type": (
                    "doc_rule_amount_min_value;dirty:amount:negative_amount"
                    ";dirty:note:leading_trailing_spaces"
                ),
            }
        ]
    )

    audit = build_record_audit(
        abnormal,
        rule_fields={"doc_rule_amount_min_value": "amount"},
    )

    labels = list(audit["label"])
    assert "doc_rule_amount_min_value" in labels
    assert "dirty:amount:negative_amount" not in labels
    # An untouched field keeps its finding, and now carries the field it is about.
    assert "dirty:note:leading_trailing_spaces" in labels
    note_row = audit[audit["label"].eq("dirty:note:leading_trailing_spaces")].iloc[0]
    assert note_row["affected_field"] == "note"
