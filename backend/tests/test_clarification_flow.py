"""Tests for the plan/execute split and the P1 bounded clarification flow.

Covers: (a) process_input_paths == execute_planned_job(plan_input_paths(...)) output
parity; (b) an incomplete TaskSpec pauses at needs_clarification and resumes after
the user fills its next missing slot; (c) a complete TaskSpec never pauses
("一句话直达"); (d) the sync endpoint still works; (e) invalid resume requests fail;
(f) multiple missing slots are handled one per round up to the configured limit.
"""

import json
import time
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.agent.planner import (
    collect_conditional_clarification,
    has_blocking_questions,
)
from data_agent.api import app
from data_agent.services import (
    execute_planned_job,
    plan_input_paths,
    process_input_paths,
)

_PAUSE = "needs_clarification"
_CONFIRM = "awaiting_confirmation"
_TERMINAL = {"succeeded", "failed", "cancelled"}

# A goal that names no table and no clear field, so its TaskSpec contains an
# unresolved primary-table slot.
_AMBIGUOUS_GOAL = "把两个表关联起来处理一下"


def _sample(tmp_path: Path) -> list[Path]:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    pd.DataFrame(
        {"order_id": ["O1", "O2"], "customer_id": ["C1", "C2"], "amount": [100, 200]}
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {"customer_id": ["C1", "C2"], "customer_name": ["Acme", "Globex"]}
    ).to_csv(customers, index=False)
    return [orders, customers]


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the deterministic path everywhere so confidence is rule-derived."""
    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.build_default_llm_planner", lambda: None
    )
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner", lambda: None
    )


# --- (a) plan/execute parity ---------------------------------------------------


def test_process_equals_plan_then_execute(tmp_path: Path) -> None:
    inputs = _sample(tmp_path)
    goal = "清洗订单并合并客户名称"

    combined = process_input_paths(
        inputs, work_dir=tmp_path / "combined", config={"goal": goal, "result_limit": 10}
    )
    plan = plan_input_paths(
        inputs, work_dir=tmp_path / "split", config={"goal": goal, "result_limit": 10}
    )
    split = execute_planned_job(plan, inputs)

    assert combined["counts"] == split["counts"]
    assert combined["quality_score"] == split["quality_score"]
    assert combined["result_row_count"] == split["result_row_count"]


# --- shared polling helper -----------------------------------------------------


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


# --- (b) low-confidence goal -> single-select pause + resume -------------------


def test_low_confidence_goal_pauses_with_single_select_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.delenv("DATA_AGENT_CLARIFY_ENABLED", raising=False)  # default on
    _no_llm(monkeypatch)
    client = TestClient(app)

    orders_csv = "order_id,customer_id,amount\nO1,C1,100\nO2,C2,200\n"
    customers_csv = "customer_id,customer_name\nC1,Acme\nC2,Globex\n"
    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("customers.csv", customers_csv.encode("utf-8"), "text/csv")),
        ],
        data={"goal": _AMBIGUOUS_GOAL, "mode": "answer"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    paused = _wait_until(client, job_id, lambda s: s == _PAUSE)
    assert paused["status"] == _PAUSE

    clarify = client.get(f"/api/v1/jobs/{job_id}/clarification")
    assert clarify.status_code == 200
    questions = clarify.json()["questions"]
    assert len(questions) == 1  # exactly one, the most consequential decision
    question = questions[0]
    assert question["kind"] == "base_table_selection"
    assert set(question["options"]) == {"orders", "customers"}

    assert client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": []},
    ).status_code == 422
    assert client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": question["id"], "answer": "  "}]},
    ).status_code == 400
    assert client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": question["id"], "answer": "unknown"}]},
    ).status_code == 400
    assert client.get(f"/api/v1/jobs/{job_id}/status").json()["status"] == _PAUSE

    # Pick a single-select option; the answer folds back into the goal and re-plans.
    submit = client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": question["id"], "answer": "orders"}]},
    )
    assert submit.status_code == 200
    assert submit.json()["status"] == "running"

    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"


def test_plan_mode_remains_non_executing_after_clarification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.delenv("DATA_AGENT_CLARIFY_ENABLED", raising=False)
    _no_llm(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,customer_id,amount\nO1,C1,100\nO2,C2,200\n",
                    "text/csv",
                ),
            ),
            (
                "files",
                (
                    "customers.csv",
                    b"customer_id,customer_name\nC1,Acme\nC2,Globex\n",
                    "text/csv",
                ),
            ),
        ],
        data={"goal": _AMBIGUOUS_GOAL, "mode": "plan"},
    )
    job_id = response.json()["job_id"]
    _wait_until(client, job_id, lambda status: status == _PAUSE)
    question = client.get(
        f"/api/v1/jobs/{job_id}/clarification"
    ).json()["questions"][0]

    submit = client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": question["id"], "answer": "orders"}]},
    )
    assert submit.status_code == 200

    done = _wait_until(client, job_id, lambda status: status in _TERMINAL)
    assert done["status"] == "succeeded"
    assert done["mode"] == "plan"
    full_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert not full_job["files"]["excel_path"]
    assert client.get(f"/api/v1/jobs/{job_id}/plan").status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}/business-answer").status_code == 404


def test_submit_clarification_rejected_when_not_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    _no_llm(monkeypatch)
    client = TestClient(app)

    orders_csv = "order_id,amount\nO1,100\n"
    response = client.post(
        "/api/v1/jobs",
        files=[("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv"))],
        data={"goal": "清洗订单", "mode": "answer"},
    )
    job_id = response.json()["job_id"]
    _wait_until(client, job_id, lambda s: s in _TERMINAL)

    # The job already finished (a clear goal never pauses) so answers are 400.
    submit = client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": "base_table", "answer": "orders"}]},
    )
    assert submit.status_code == 400
    assert client.get(f"/api/v1/jobs/{job_id}/clarification").status_code == 409


# --- (c) confident goal never pauses ("一句话直达") ----------------------------


def test_confident_goal_does_not_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.delenv("DATA_AGENT_CLARIFY_ENABLED", raising=False)  # default on
    _no_llm(monkeypatch)
    client = TestClient(app)

    orders_csv = "order_id,customer_id,amount\nO1,C1,100\n"
    customers_csv = "customer_id,customer_name\nC1,Acme\n"
    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("customers.csv", customers_csv.encode("utf-8"), "text/csv")),
        ],
        # Names the subject + fields -> high/medium confidence -> no interruption.
        data={"goal": "清洗订单并合并客户名称", "mode": "answer"},
    )
    job_id = response.json()["job_id"]
    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"


def test_clarify_disabled_never_pauses_even_when_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_CLARIFY_ENABLED", "0")  # explicit opt-out
    _no_llm(monkeypatch)
    client = TestClient(app)

    orders_csv = "order_id,customer_id,amount\nO1,C1,100\n"
    customers_csv = "customer_id,customer_name\nC1,Acme\n"
    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("customers.csv", customers_csv.encode("utf-8"), "text/csv")),
        ],
        data={"goal": _AMBIGUOUS_GOAL, "mode": "answer"},
    )
    job_id = response.json()["job_id"]
    done = _wait_until(client, job_id, lambda s: s in _TERMINAL)
    assert done["status"] == "succeeded"


# --- (d) sync endpoint still works --------------------------------------------


def test_sync_process_endpoint_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    orders_csv = "order_id,amount\nO1,100\nO2,200\n"
    response = client.post(
        "/api/v1/cleaning/process",
        files=[("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv"))],
        data={"config": json.dumps({"goal": "清洗订单", "result_limit": 10})},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def _install_multi_round_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def planner(messages):
        context = json.loads(messages[1]["content"].split("\n", 1)[1])
        goal = context["goal"]
        if "[用户澄清]" not in goal:
            questions = ["“能用的订单”具体按什么状态保留？"]
        elif "status=paid" in goal and "只输出" not in goal:
            questions = ["最终结果需要保留哪些字段？"]
        else:
            questions = []
        return json.dumps(
            {
                "suggested_base_table": "orders",
                "target_fields": [{"table": "orders", "field": "amount"}],
                "clarification_questions": questions,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.build_default_llm_planner",
        lambda: planner,
    )
    # Keep config refinement/reflection deterministic; this test targets only the
    # goal-understanding missing-slot loop.
    monkeypatch.setattr(
        "data_agent.agent.planner.build_default_llm_planner",
        lambda: None,
    )
    monkeypatch.setattr(
        "data_agent.services.processing.build_default_llm_planner",
        lambda: None,
    )


def test_task_session_supports_two_clarification_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_MAX_CLARIFICATION_ROUNDS", "3")
    _install_multi_round_llm(monkeypatch)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,status,amount\nO1,paid,100\nO2,unpaid,200\n",
                    "text/csv",
                ),
            )
        ],
        data={"goal": "整理订单，能用的留下", "mode": "answer"},
    )
    job_id = response.json()["job_id"]

    _wait_until(client, job_id, lambda status: status == _PAUSE)
    first = client.get(f"/api/v1/jobs/{job_id}/clarification").json()["questions"][0]
    assert first["kind"] == "retention_rule"
    client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": first["id"], "answer": "保留 status=paid"}]},
    )

    _wait_until(client, job_id, lambda status: status == _PAUSE)
    second = client.get(f"/api/v1/jobs/{job_id}/clarification").json()["questions"][0]
    assert second["kind"] == "target_field"
    assert second["id"] != first["id"]
    client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": second["id"], "answer": "只输出 order_id 和 amount"}]},
    )

    # The clarification establishes authorization, but retaining one of two rows is a
    # separate 50% impact escalation under the layered confirmation policy.
    _wait_until(client, job_id, lambda status: status == _CONFIRM)
    confirmation = client.get(f"/api/v1/jobs/{job_id}/plan-confirmation").json()
    filter_step = next(
        step
        for step in confirmation["execution_plan"]["steps"]
        if step["capability_id"] == "filter_rows"
    )
    assert filter_step["authorization"] == "stated"
    assert filter_step["confirmation_reasons"] == ["high_removal_ratio"]
    response = client.post(
        f"/api/v1/jobs/{job_id}/plan-confirmation",
        json={
            "plan_id": confirmation["plan_id"],
            "plan_hash": confirmation["plan_hash"],
        },
    )
    assert response.status_code == 200
    done = _wait_until(client, job_id, lambda status: status in _TERMINAL)
    assert done["status"] == "succeeded"
    assert done["result_row_count"] == 1
    session = client.get(f"/api/v1/jobs/{job_id}/session").json()
    assert session["clarification_round"] == 2
    assert len(
        [turn for turn in session["turns"] if turn["kind"] == "clarification_answer"]
    ) == 2


def test_clarification_round_limit_falls_back_to_plan_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_MAX_CLARIFICATION_ROUNDS", "1")
    _install_multi_round_llm(monkeypatch)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,status,amount\nO1,paid,100\n",
                    "text/csv",
                ),
            )
        ],
        data={"goal": "整理订单，能用的留下", "mode": "answer"},
    )
    job_id = response.json()["job_id"]

    _wait_until(client, job_id, lambda status: status == _PAUSE)
    first = client.get(f"/api/v1/jobs/{job_id}/clarification").json()["questions"][0]
    client.post(
        f"/api/v1/jobs/{job_id}/clarification",
        json={"answers": [{"question_id": first["id"], "answer": "保留 status=paid"}]},
    )

    paused = _wait_until(client, job_id, lambda status: status == "awaiting_confirmation")
    assert paused["status"] == "awaiting_confirmation"
    session = client.get(f"/api/v1/jobs/{job_id}/session").json()
    assert session["clarification_round"] == 1
    assert session["max_clarification_rounds"] == 1


def test_unspecified_filter_asks_instead_of_aborting_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"过滤掉金额小于 0 的记录" names an action but not a condition.

    The task already records a `filter_rule` missing slot for exactly this case, but
    action-coverage validation ran first and killed the job with an internal error —
    so the question the agent had prepared could never be asked.
    """

    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame({"order_id": [1, 2], "amount": [100, -5]}).to_csv(
        input_dir / "orders.csv", index=False
    )

    plan = plan_input_paths(
        [input_dir],
        work_dir=tmp_path / "work",
        config={"goal": "清洗订单数据，过滤掉金额小于 0 的记录"},
    )

    task_spec = plan.goal_plan["task_spec"]
    assert "filter" in task_spec["actions"]
    assert task_spec["filters"] == [], "condition is not parseable, so none is invented"
    assert [slot["slot_id"] for slot in task_spec["missing_slots"]] == ["filter_rule"]

    questions = collect_conditional_clarification(plan.goal_plan)
    assert has_blocking_questions(questions)
    assert "保留记录" in questions[0]["question"]
