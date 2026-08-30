import json

import pandas as pd

from data_agent.agent.data_context import (
    DataAccessMode,
    build_column_context,
    group_columns_by_table,
    load_data_access_mode,
    relationship_context,
)
from data_agent.agent.planner import build_llm_planner_messages
from data_agent.tools.profiler import profile_dataset
from data_agent.tools.recommender import build_recommendation_package


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "orders": pd.DataFrame(
            {
                "customer_id": ["C001", "C002"],
                "customer_name": ["Acme", "Globex"],
                "status": ["paid", "unpaid"],
                "amount": [100, 200],
            }
        )
    }


def test_metadata_only_contains_no_samples() -> None:
    columns, access = build_column_context(
        _tables(),
        mode=DataAccessMode.METADATA_ONLY,
    )

    assert access["mode"] == "metadata_only"
    assert access["sample_limit_per_field"] == 0
    assert all("sample_values" not in column for column in columns)


def test_masked_samples_redact_sensitive_fields_but_keep_low_risk_categories() -> None:
    columns, access = build_column_context(
        _tables(),
        mode=DataAccessMode.MASKED_SAMPLES,
    )
    by_field = {column["field"]: column for column in columns}

    assert access["raw_samples_allowed"] is False
    assert set(access["masked_fields"]) == {
        "orders.customer_id",
        "orders.customer_name",
    }
    assert "C001" not in by_field["customer_id"]["sample_values"]
    assert "Acme" not in by_field["customer_name"]["sample_values"]
    assert by_field["status"]["sample_values"] == ["paid", "unpaid"]


def test_trusted_samples_include_raw_values() -> None:
    columns, access = build_column_context(
        _tables(),
        mode=DataAccessMode.TRUSTED_SAMPLES,
    )
    grouped = group_columns_by_table(columns)
    by_field = {column["name"]: column for column in grouped["orders"]}

    assert access["raw_samples_allowed"] is True
    assert by_field["customer_id"]["sample_values"] == ["C001", "C002"]
    assert by_field["customer_name"]["sample_values"] == ["Acme", "Globex"]


def test_masked_samples_redact_sensitive_values_inside_free_text() -> None:
    columns, access = build_column_context(
        {
            "tickets": pd.DataFrame(
                {"notes": ["Alice phone 13800138000, Beijing address"]}
            )
        },
        mode=DataAccessMode.MASKED_SAMPLES,
    )

    assert columns[0]["sample_values"][0].startswith("<text:")
    assert "13800138000" not in columns[0]["sample_values"][0]
    assert access["masked_fields"] == ["tickets.notes"]


def test_relationship_samples_only_allowed_in_trusted_mode() -> None:
    frame = pd.DataFrame(
        [
            {
                "left_table": "orders",
                "left_field": "customer_id",
                "right_table": "customers",
                "right_field": "customer_id",
                "left_match_rate": 0.5,
                "unmatched_left_sample": "C404",
                "unmatched_left_values": "C404, C405",
                "right_duplicate_key_sample": "C001",
            }
        ]
    )

    masked = relationship_context(frame, mode=DataAccessMode.MASKED_SAMPLES)
    trusted = relationship_context(frame, mode=DataAccessMode.TRUSTED_SAMPLES)

    assert masked[0]["left_match_rate"] == 0.5
    assert "unmatched_left_sample" not in masked[0]
    assert trusted[0]["unmatched_left_sample"] == "C404"


def test_invalid_env_mode_fails_closed_to_masked_samples(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_DATA_ACCESS_MODE", "unknown-mode")
    assert load_data_access_mode() is DataAccessMode.MASKED_SAMPLES


def test_metadata_only_planner_prompt_contains_no_lookup_key_samples(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_DATA_ACCESS_MODE", "metadata_only")
    tables = {
        "orders": pd.DataFrame(
            {
                "customer_id": ["C001", "C002", "C003", "C005", "C006"],
                "amount": [1, 2, 3, 4, 5],
            }
        ),
        "customers": pd.DataFrame(
            {
                "customer_id": ["C001", "C002", "C003", "C005"],
                "customer_name": ["A", "B", "C", "E"],
            }
        ),
    }
    profiles = profile_dataset(tables)
    recommendations, job_config = build_recommendation_package(
        tables,
        profiles,
        output_job_path=tmp_path / "job.json",
        goal="以 orders 为主表，按 customer_id 合并 customers 的 customer_name",
    )

    messages = build_llm_planner_messages(
        goal="以 orders 为主表，按 customer_id 合并 customers 的 customer_name",
        deterministic_config=job_config,
        profile_sheets=profiles,
        recommendation_sheets=recommendations,
        tables=tables,
    )
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    serialized = messages[1]["content"]

    assert context["data_access"]["mode"] == "metadata_only"
    assert "C006" not in serialized
    assert str(tmp_path) not in serialized
    assert all(
        "unmatched_left_sample" not in lookup
        and "right_duplicate_key_sample" not in lookup
        for lookup in context["recommended_lookups"]
    )
