"""Authorization and risk escalation for record-removing plans.

Quoted evidence authorizes an action. A separate deterministic policy decides whether
the authorized plan still needs review because its blast radius is high or unknown.

Three layers, each tested here:

1. A low-impact rule traceable to the user's goal runs without a second question.
   (``planning.provenance`` decides traceability.)
2. With a goal present, a destructive rule that is *not* in the TaskSpec is rejected
   outright by ``validate_task_spec_operations`` — injection is an error, not a prompt.
3. High-impact and authorization-free rules stop at the same shared confirmation gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.api import app
from data_agent.services import (
    DestructivePlanNotConfirmedError,
    answer_input_paths,
    process_input_paths_with_reflection,
)

_DESTRUCTIVE_GOAL = "清洗订单，过滤 amount < 0 的记录"
# A row-removing rule with no goal behind it. Nothing established that the user wanted
# records gone, so it carries the default authorization ("inferred") and must stop.
_UNASKED_DELETION = {
    "job_overrides": {
        "formulas": [
            {
                "output": "_filter_rows_1",
                "op": "filter_rows",
                "condition": {"field": "amount", "op": "gte", "value": 0},
            }
        ]
    }
}


def _write_orders(input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4, 5],
            "amount": [100, -5, 30, 40, 50],
        }
    ).to_csv(input_dir / "orders.csv", index=False)


def test_cli_runs_a_deletion_the_goal_asked_for_without_a_second_question(
    tmp_path: Path,
) -> None:
    """An explicit instruction is authorization; asking again would be the bug."""

    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    result = answer_input_paths(
        [input_dir],
        goal=_DESTRUCTIVE_GOAL,
        output_dir=tmp_path / "out",
    )

    delivered = pd.read_excel(tmp_path / "out" / "final_result.xlsx", sheet_name="处理结果")
    assert -5 not in set(delivered["amount"])
    assert result.answer.usable_records_count == 4


def test_sync_endpoint_runs_a_deletion_the_goal_asked_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    response = client.post(
        "/api/v1/cleaning/process",
        files=[(
            "files",
            (
                "orders.csv",
                b"order_id,amount\n1,100\n2,-5\n3,30\n4,40\n5,50\n",
                "text/csv",
            ),
        )],
        data={"config": json.dumps({"goal": _DESTRUCTIVE_GOAL})},
    )
    assert response.status_code == 200


def test_a_deletion_absent_from_the_task_spec_is_rejected_not_merely_confirmed(
    tmp_path: Path,
) -> None:
    """With a goal in play, an injected destructive rule never reaches a prompt.

    The goal says only 清洗订单数据. A filter smuggled in through job_overrides has no
    counterpart in the TaskSpec, and the comparison is exact, so this is an error
    rather than something the user could wave through by accident.
    """

    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    with pytest.raises(ValueError, match="与 TaskSpec 不一致"):
        process_input_paths_with_reflection(
            [input_dir],
            work_dir=tmp_path / "work",
            config={"goal": "清洗订单数据", **_UNASKED_DELETION},
        )


def test_hand_written_config_without_a_goal_still_stops_at_the_gate(
    tmp_path: Path,
) -> None:
    """No goal means no TaskSpec to check against — the gate is the only guard left."""

    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    with pytest.raises(DestructivePlanNotConfirmedError) as excinfo:
        process_input_paths_with_reflection(
            [input_dir],
            work_dir=tmp_path / "work",
            config=_UNASKED_DELETION,
        )

    assert "删行规则" in str(excinfo.value)
    assert "--yes" in str(excinfo.value)


def test_hand_written_config_runs_once_it_is_approved(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    result = process_input_paths_with_reflection(
        [input_dir],
        work_dir=tmp_path / "work",
        config=_UNASKED_DELETION,
        confirm_destructive=True,
    )

    assert result["status"] == "success"
    assert Path(result["files"]["excel_path"]).exists()


def test_stated_high_impact_deletion_stops_for_confirmation(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True)
    pd.DataFrame(
        {"order_id": [1, 2, 3], "amount": [100, -5, 30]}
    ).to_csv(input_dir / "orders.csv", index=False)

    with pytest.raises(DestructivePlanNotConfirmedError) as excinfo:
        answer_input_paths(
            [input_dir],
            goal=_DESTRUCTIVE_GOAL,
            output_dir=tmp_path / "out",
        )

    assert "33.3%" in str(excinfo.value)

    result = answer_input_paths(
        [input_dir],
        goal=_DESTRUCTIVE_GOAL,
        output_dir=tmp_path / "approved",
        confirm_destructive=True,
    )
    assert result.answer.usable_records_count == 2


def test_non_destructive_plan_needs_no_approval(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_orders(input_dir)

    result = answer_input_paths(
        [input_dir],
        goal="清洗订单数据",
        output_dir=tmp_path / "out",
    )
    assert result.answer.usable_records_count >= 1
