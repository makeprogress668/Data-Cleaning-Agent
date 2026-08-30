import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.api import app


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        payload = client.get(f"/api/v1/jobs/{job_id}/status").json()
        if payload.get("status") in {"succeeded", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_recipe_reuses_typed_contract_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    client = TestClient(app)
    source = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                ("sales.csv", b"city,amount\nA,10\nA,20\nB,5\n", "text/csv"),
            )
        ],
        data={"goal": "按 city 统计 amount 合计", "mode": "plan"},
    )
    assert source.status_code == 202
    source_id = source.json()["job_id"]
    assert _wait(client, source_id)["status"] == "succeeded"

    saved = client.post(
        "/api/v1/recipes",
        json={"name": "城市销售汇总", "source_job_id": source_id},
    )
    assert saved.status_code == 201
    recipe = saved.json()
    assert recipe["task_spec"]["actions"] == ["analyze", "export"]

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("a recipe must not call semantic understanding or refinement")

    monkeypatch.setattr("data_agent.services.processing.understand_goal", unexpected_call)
    monkeypatch.setattr(
        "data_agent.services.processing.refine_config_with_llm", unexpected_call
    )
    applied = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                ("sales.csv", b"city,amount\nA,7\nB,8\n", "text/csv"),
            )
        ],
        data={"recipe_id": recipe["recipe_id"], "mode": "answer"},
    )
    assert applied.status_code == 202
    applied_id = applied.json()["job_id"]
    terminal = _wait(client, applied_id)
    assert terminal["status"] == "succeeded", terminal
    detail = client.get(f"/api/v1/jobs/{applied_id}").json()
    assert detail["goal_plan"]["source"] == "recipe"
    assert client.get(f"/api/v1/jobs/{applied_id}/events").json()["llm_usage"] == []

    listed = client.get("/api/v1/recipes").json()["recipes"]
    assert [item["recipe_id"] for item in listed] == [recipe["recipe_id"]]
    assert client.delete(f"/api/v1/recipes/{recipe['recipe_id']}").status_code == 200
    assert client.get(f"/api/v1/recipes/{recipe['recipe_id']}").status_code == 404


def test_recipe_rejects_a_different_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    client = TestClient(app)
    source = client.post(
        "/api/v1/jobs",
        files=[("files", ("sales.csv", b"city,amount\nA,1\n", "text/csv"))],
        data={"goal": "按 city 统计 amount 合计", "mode": "plan"},
    )
    source_id = source.json()["job_id"]
    assert _wait(client, source_id)["status"] == "succeeded"
    recipe_id = client.post(
        "/api/v1/recipes",
        json={"name": "固定合同", "source_job_id": source_id},
    ).json()["recipe_id"]

    response = client.post(
        "/api/v1/jobs",
        files=[("files", ("sales.csv", b"city,amount\nA,2\n", "text/csv"))],
        data={"recipe_id": recipe_id, "goal": "删除全部数据", "mode": "answer"},
    )
    assert response.status_code == 400
