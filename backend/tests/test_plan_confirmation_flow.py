"""Tests for the pre-execution plan confirmation gate (P0).

Covers: (a) with answer mode + CONFIRM_ENABLED a job pauses at awaiting_confirmation,
exposes the goal understanding, and resumes to succeeded after approval; (b) plan
mode generates a reviewable plan without executing; (c) discover mode generates
discovery only; (d) confirming a job that is not awaiting confirmation is rejected 400;
(e) the confirmed run keeps the original goal (approval never mutates the plan).

The gate mirrors the clarification pause/resume machinery and is default-off, so the
existing "一句话直达" answer-mode behaviour is unchanged.
"""

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.api import app

_CONFIRM = "awaiting_confirmation"
_TERMINAL = {"succeeded", "failed", "cancelled"}


def _wait_until(client: TestClient, job_id: str, predicate, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}/status")
        assert response.status_code == 200
        payload = response.json()
        if predicate(payload.get("status")):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never satisfied predicate: {payload}")


def _submit(client: TestClient, goal: str, mode: str) -> str:
    orders_csv = "order_id,customer_id,amount\nO1,C1,100\nO2,C2,200\n"
    customers_csv = "customer_id,customer_name\nC1,Acme\nC2,Globex\n"
    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("customers.csv", customers_csv.encode("utf-8"), "text/csv")),
        ],
        data={"goal": goal, "mode": mode},
    )
    assert response.status_code == 202
    return response.json()["job_id"]


# --- (a) answer mode + flag -> pause, surface understanding, resume ------------


def test_answer_mode_pauses_for_confirmation_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_CONFIRM_ENABLED", "1")
    # Deterministic (no LLM) so the run is fast and the gate fires purely on mode.
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    import data_agent.api.job_manager as job_manager

    real_plan_input_paths = job_manager.plan_input_paths
    plan_calls = {"count": 0}

    def counting_plan(*args, **kwargs):
        plan_calls["count"] += 1
        return real_plan_input_paths(*args, **kwargs)

    monkeypatch.setattr(job_manager, "plan_input_paths", counting_plan)
    client = TestClient(app)

    job_id = _submit(client, "清洗订单并合并客户名称", "answer")

    paused = _wait_until(client, job_id, lambda s: s == _CONFIRM)
    assert paused["status"] == _CONFIRM

    # The confirmation endpoint surfaces the pre-execution goal understanding.
    confirmation = client.get(f"/api/v1/jobs/{job_id}/plan-confirmation")
    assert confirmation.status_code == 200
    body = confirmation.json()
    assert body["status"] == _CONFIRM
    assert len(body["plan_hash"]) == 64
    assert plan_calls["count"] == 1
    understanding = body["goal_understanding"]
    assert "understanding_confidence" in understanding
    assert "is_generic_fallback" in understanding
    # Internal evidence stays out of a business-facing payload. understanding_source is
    # the exception: "模型没连上，这次是按关键词理解的" is the user's answer to why a
    # result differs from last time, so it has to travel with the understanding.
    assert understanding["understanding_source"] == "deterministic"
    assert {
        "field_matches",
        "table_matches",
        "matched_fields",
        "data_access",
    }.isdisjoint(understanding)

    approve = client.post(
        f"/api/v1/jobs/{job_id}/plan-confirmation",
        json={"plan_id": body["plan_id"], "plan_hash": body["plan_hash"]},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "running"

    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"
    # Approval never mutates the goal the user reviewed.
    assert done["goal"] == "清洗订单并合并客户名称"
    # Confirmation hydrates the frozen snapshot; it must not invoke planning again.
    assert plan_calls["count"] == 1


def test_high_impact_deletion_pauses_without_global_confirmation_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan policy, not only a deployment-wide flag, can require review."""

    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.delenv("DATA_AGENT_CONFIRM_ENABLED", raising=False)
    monkeypatch.setenv("DATA_AGENT_CLARIFY_ENABLED", "0")
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,amount\nO1,100\nO2,-5\nO3,30\n",
                    "text/csv",
                ),
            )
        ],
        data={"goal": "清洗订单，过滤 amount < 0 的记录", "mode": "answer"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    _wait_until(client, job_id, lambda status: status == _CONFIRM)
    confirmation = client.get(f"/api/v1/jobs/{job_id}/plan-confirmation").json()
    filter_step = next(
        step
        for step in confirmation["execution_plan"]["steps"]
        if step["capability_id"] == "filter_rows"
    )
    assert filter_step["confirmation_reasons"] == ["high_removal_ratio"]
    assert filter_step["impact_estimate"]["removal_ratio"] == pytest.approx(1 / 3)

    approve = client.post(
        f"/api/v1/jobs/{job_id}/plan-confirmation",
        json={
            "plan_id": confirmation["plan_id"],
            "plan_hash": confirmation["plan_hash"],
        },
    )
    assert approve.status_code == 200
    done = _wait_until(client, job_id, lambda status: status in _TERMINAL)
    assert done["status"] == "succeeded"


# --- (b) plan mode returns a plan without execution ----------------------------


def test_plan_mode_completes_without_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.delenv("DATA_AGENT_CONFIRM_ENABLED", raising=False)
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    client = TestClient(app)

    job_id = _submit(client, "清洗订单并合并客户名称", "plan")
    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"
    full_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert not full_job["files"]["excel_path"]
    assert client.get(f"/api/v1/jobs/{job_id}/plan").status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}/business-answer").status_code == 404


# --- (c) discover mode returns discovery without execution ---------------------


def test_discover_mode_completes_without_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_CONFIRM_ENABLED", "1")
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    client = TestClient(app)

    job_id = _submit(client, "清洗订单并合并客户名称", "discover")
    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"
    full_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert not full_job["files"]["excel_path"]
    assert client.get(f"/api/v1/jobs/{job_id}/discovery").status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}/business-answer").status_code == 404


# --- (d) confirming a non-paused job is rejected ------------------------------


def test_confirm_rejected_when_not_awaiting_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    client = TestClient(app)

    # answer mode finishes without ever pausing, so it is never awaiting confirmation.
    job_id = _submit(client, "清洗订单", "answer")
    _wait_until(client, job_id, lambda s: s in _TERMINAL)

    reject = client.post(
        f"/api/v1/jobs/{job_id}/plan-confirmation",
        json={"plan_id": "0" * 32, "plan_hash": "0" * 64},
    )
    assert reject.status_code == 400
    assert client.get(f"/api/v1/jobs/{job_id}/plan-confirmation").status_code == 409


def test_stale_plan_hash_cannot_confirm_a_newer_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_CONFIRM_ENABLED", "1")
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )
    client = TestClient(app)

    job_id = _submit(client, "清洗订单", "answer")
    _wait_until(client, job_id, lambda status: status == _CONFIRM)
    current = client.get(f"/api/v1/jobs/{job_id}/plan-confirmation").json()

    stale = client.post(
        f"/api/v1/jobs/{job_id}/plan-confirmation",
        json={"plan_id": current["plan_id"], "plan_hash": "0" * 64},
    )

    assert stale.status_code == 409
    assert "刷新" in stale.json()["detail"]
    status = client.get(f"/api/v1/jobs/{job_id}/status").json()
    assert status["status"] == _CONFIRM
