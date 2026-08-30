import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.api import app
from data_agent.api.job_store import (
    append_session_turn,
    create_task_session,
    read_task_session,
    record_session_plan,
    update_task_session,
)
from data_agent.schemas.session import TaskSession

_JOB_ID = "a" * 32


def _use_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))


def test_task_session_persists_instruction_plan_and_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_work_dir(tmp_path, monkeypatch)
    created = create_task_session(_JOB_ID, "清洗订单")
    assert created["original_instruction"] == "清洗订单"
    assert created["turns"][0]["kind"] == "instruction"

    record_session_plan(
        _JOB_ID,
        task_spec={"version": 1, "objective": "清洗订单"},
        plan_hash="b" * 64,
    )
    append_session_turn(
        _JOB_ID,
        role="assistant",
        kind="clarification_question",
        content="请选择主表",
        structured_payload={"slot_id": "primary_table"},
        status="needs_clarification",
    )
    update_task_session(_JOB_ID, clarification_round=1)

    session = TaskSession.model_validate(read_task_session(_JOB_ID))
    assert session.current_task_spec_version == 1
    assert session.current_plan_version == 1
    assert session.current_plan_hash == "b" * 64
    assert session.clarification_round == 1
    assert [turn.kind for turn in session.turns] == [
        "instruction",
        "plan_created",
        "clarification_question",
    ]


def test_api_job_exposes_session_and_version_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_work_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_AGENT_CLARIFY_ENABLED", "0")
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[("files", ("orders.csv", b"order_id,amount\nO1,100\n", "text/csv"))],
        data={"goal": "清洗订单", "mode": "answer"},
    )
    job_id = response.json()["job_id"]

    deadline = time.time() + 10
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}/status").json()
        if status["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert status["session_id"]
    assert status["task_spec_version"] == 1
    assert status["plan_version"] == 1
    session_response = client.get(f"/api/v1/jobs/{job_id}/session")
    assert session_response.status_code == 200
    session = TaskSession.model_validate(session_response.json())
    assert session.original_instruction == "清洗订单"
    assert session.status == "succeeded"
    assert session.task_spec["objective"] == "清洗订单"
