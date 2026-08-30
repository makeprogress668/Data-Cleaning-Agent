"""Delivery must plan once and let reflection re-execute *that* plan.

The CLI used to run the whole planner (goal understanding + LLM planner + profiling +
document extraction) inside every reflection attempt. That made each attempt cost a
full extra planning round, and — worse — the two runs being compared could differ for
reasons unrelated to the override under test, so "keep the better run" was no longer
an A/B on the proposed change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data_agent.services import answer_input_paths
from data_agent.services import processing as processing_module


def _write_orders(input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "order_id": [1, 2, 3],
            "customer_id": ["C1", "C2", ""],
            "amount": [100, 200, 300],
        }
    ).to_csv(input_dir / "orders.csv", index=False)


def test_reflection_does_not_re_run_goal_understanding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    calls: list[str] = []
    original = processing_module.understand_goal

    def counted(goal, tables, **kwargs):
        calls.append(goal)
        return original(goal, tables, **kwargs)

    monkeypatch.setattr(processing_module, "understand_goal", counted)

    # Force the reflection loop to actually fire a round.
    def planner(_messages):
        return json.dumps(
            {
                "diagnosis": "widen critical field coverage",
                "overrides": {"quality_score": {"critical_fields": ["amount"]}},
                "expected_effect": "catch more issues",
            }
        )

    monkeypatch.setattr(
        processing_module,
        "build_default_llm_planner",
        lambda: planner,
    )
    monkeypatch.setenv("DATA_AGENT_REFLECTION_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_MAX_ROUNDS", "1")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_TARGET_SCORE", "100")

    answer_input_paths(
        [input_dir],
        goal="清洗订单数据，列出不能使用的数据原因",
        output_dir=tmp_path / "out",
    )

    assert len(calls) == 1, f"planning ran {len(calls)} times, expected exactly once"
