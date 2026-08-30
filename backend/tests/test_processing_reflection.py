"""Tests for the API-side bounded reflection wrapper.

The wrapper (``process_input_paths_with_reflection``) must give the API path the
same run-observe-revise-rerun-keep-best behaviour the CLI delivery flow has, while
staying byte-for-byte compatible with the plain path whenever no LLM is configured.
"""

import json
from pathlib import Path

import pandas as pd

from data_agent.services import (
    process_input_paths,
    process_input_paths_with_reflection,
)


def _write_sample(tmp_path: Path) -> list[Path]:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    pd.DataFrame(
        {
            "order_id": ["O1", "O2", "O3"],
            "customer_id": ["C001", "C404", "C003"],
            "amount": [100, 200, 300],
        }
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {
            "customer_id": ["C001", "C003"],
            "customer_name": ["Acme", "Globex"],
        }
    ).to_csv(customers, index=False)
    return [orders, customers]


def test_wrapper_matches_plain_path_without_llm(tmp_path: Path) -> None:
    """With no LLM configured the wrapper must behave exactly like the plain path."""
    inputs = _write_sample(tmp_path)
    goal = "清洗订单数据，按 customer_id 合并客户信息，并输出异常复核清单"

    plain = process_input_paths(
        inputs, work_dir=tmp_path / "plain", config={"goal": goal, "result_limit": 10}
    )
    wrapped = process_input_paths_with_reflection(
        inputs, work_dir=tmp_path / "wrapped", config={"goal": goal, "result_limit": 10}
    )

    assert wrapped["status"] == "success"
    assert wrapped["counts"] == plain["counts"]
    assert wrapped["quality_score"] == plain["quality_score"]
    assert wrapped["result_row_count"] == plain["result_row_count"]
    # The scoring-only internal keys must never leak into the API response.
    assert "quality_frames" not in wrapped
    assert "relationship_candidates" not in wrapped
    # No reflection ran, so no attempts are attached.
    assert "reflection_attempts" not in wrapped
    assert Path(wrapped["files"]["excel_path"]).exists()


def test_wrapper_keeps_result_and_records_attempt_with_llm(tmp_path: Path, monkeypatch) -> None:
    """A configured LLM triggers a validated reflection round; the best run is kept."""
    inputs = _write_sample(tmp_path)
    goal = "清洗订单数据，按 customer_id 合并客户信息"

    # A planner that proposes a valid, schema-safe override (tighten critical fields).
    def _planner(_messages):
        return (
            '{"diagnosis": "critical fields too narrow", '
            '"overrides": {"quality_score": {"critical_fields": ["amount"]}}, '
            '"expected_effect": "sharper scoring"}'
        )

    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: _planner
    )
    monkeypatch.setenv("DATA_AGENT_REFLECTION_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_MAX_ROUNDS", "1")
    # Force reflection to fire by demanding a target the first run can't reach.
    monkeypatch.setenv("DATA_AGENT_REFLECTION_TARGET_SCORE", "101")

    response = process_input_paths_with_reflection(
        inputs, work_dir=tmp_path / "job", config={"goal": goal, "result_limit": 10}
    )

    assert response["status"] == "success"
    # Reflection ran, so the audit trail must be present and well-formed.
    assert response.get("reflection_attempts")
    assert response["reflection_attempts"][0]["round"] == 1
    # The winning workbook is promoted to the canonical job path the API serves.
    canonical = tmp_path / "job" / "output" / "final_result.xlsx"
    assert canonical.exists()
    assert response["files"]["excel_path"] == str(canonical)
    assert "quality_frames" not in response


def test_reflection_cannot_remove_task_spec_validation_rules(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "suppliers.csv"
    pd.DataFrame(
        {
            "supplier_id": ["S1", ""],
            "supplier_name": ["Alpha", "Beta"],
        }
    ).to_csv(source, index=False)

    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.build_default_llm_planner",
        lambda: None,
    )
    monkeypatch.setattr(
        "data_agent.agent.planner.build_default_llm_planner",
        lambda: None,
    )

    def reflection_planner(_messages):
        return json.dumps(
            {
                "diagnosis": "remove rules to raise score",
                "overrides": {"anomaly_rules": []},
                "expected_effect": "no exceptions",
            }
        )

    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner",
        lambda: reflection_planner,
    )
    monkeypatch.setenv("DATA_AGENT_REFLECTION_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_MAX_ROUNDS", "1")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_TARGET_SCORE", "101")

    response = process_input_paths_with_reflection(
        [source],
        work_dir=tmp_path / "job",
        config={
            "goal": "校验供应商 supplier_id 是否为空，列出异常记录",
            "result_limit": 10,
        },
    )

    # "列出异常" asks to report the row, not to remove it from the usable result.
    assert response["counts"]["abnormal"] == 0
    assert response["review_item_count"] == 1
    assert response["reflection_attempts"][0]["status"] in {"rejected", "reverted"}
