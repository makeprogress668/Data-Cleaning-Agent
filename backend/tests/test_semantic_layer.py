import time
from pathlib import Path

import pandas as pd
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


def test_semantic_metric_compiles_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    model = client.post(
        "/api/v1/semantic-models",
        json={
            "name": "销售口径",
            "entities": [
                {
                    "name": "销售事实",
                    "table": "sales",
                    "dimensions": [{"name": "大区", "field": "region"}],
                    "measures": [
                        {
                            "name": "GMV",
                            "field": "amount",
                            "aggregation": "sum",
                            "output_name": "GMV",
                        }
                    ],
                }
            ],
        },
    ).json()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("semantic resolution must not call the LLM")

    monkeypatch.setattr("data_agent.services.processing.understand_goal", unexpected_call)
    monkeypatch.setattr(
        "data_agent.services.processing.refine_config_with_llm", unexpected_call
    )
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "sales.csv",
                    "region,amount\n北区,10\n北区,20\n南区,7\n".encode(),
                    "text/csv",
                ),
            )
        ],
        data={
            "goal": "按大区查看 GMV",
            "mode": "answer",
            "semantic_model_id": model["semantic_model_id"],
        },
    )
    job_id = response.json()["job_id"]
    terminal = _wait(client, job_id)
    assert terminal["status"] == "succeeded", terminal
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["goal_plan"]["understanding_source"] == "semantic_model"
    assert detail["goal_plan"]["task_spec"]["aggregation"] == {
        "group_by": ["region"],
        "metrics": [{"column": "amount", "agg": "sum", "output_name": "GMV"}],
        "column_field": "",
        "source": "llm",
    }
    summary = pd.read_excel(
        tmp_path / "api_jobs" / job_id / "output" / "final_result.xlsx",
        sheet_name="汇总结果",
    )
    assert summary.to_dict("records") == [
        {"region": "北区", "GMV": 30},
        {"region": "南区", "GMV": 7},
    ]
    assert client.get(f"/api/v1/jobs/{job_id}/events").json()["llm_usage"] == []


def test_semantic_relationship_binds_cross_entity_dimension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    client = TestClient(app)
    model = client.post(
        "/api/v1/semantic-models",
        json={
            "name": "订单客户模型",
            "entities": [
                {
                    "name": "订单",
                    "table": "orders",
                    "primary_key": ["order_id"],
                    "measures": [
                        {
                            "name": "GMV",
                            "field": "amount",
                            "aggregation": "sum",
                            "output_name": "GMV",
                        }
                    ],
                },
                {
                    "name": "客户",
                    "table": "customers",
                    "primary_key": ["customer_id"],
                    "dimensions": [{"name": "大区", "field": "region"}],
                },
            ],
            "relationships": [
                {
                    "from_entity": "订单",
                    "to_entity": "客户",
                    "from_keys": ["customer_id"],
                    "to_keys": ["customer_id"],
                    "cardinality": "many_to_one",
                }
            ],
        },
    ).json()
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                ("orders.csv", b"order_id,customer_id,amount\nO1,C1,10\nO2,C2,20\n", "text/csv"),
            ),
            (
                "files",
                ("customers.csv", "customer_id,region\nC1,北区\nC2,南区\n".encode(), "text/csv"),
            ),
        ],
        data={
            "goal": "按大区查看 GMV",
            "mode": "plan",
            "semantic_model_id": model["semantic_model_id"],
        },
    )
    job_id = response.json()["job_id"]
    terminal = _wait(client, job_id)
    assert terminal["status"] == "succeeded", terminal
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    lookup_steps = [
        item
        for item in detail["execution_plan"]["steps"]
        if item["capability_id"] == "lookup_fields"
    ]
    assert len(lookup_steps) == 1
    assert lookup_steps[0]["parameters"] == {
        "left_keys": ["customer_id"],
        "right_keys": ["customer_id"],
        "fields": ["region"],
        "match_mode": "exact",
        "duplicate_strategy": "error",
        "source_table": "customers",
    }
