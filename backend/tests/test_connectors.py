import time
from pathlib import Path

import pytest
from pydantic import BaseModel

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from data_agent.api import app
from data_agent.connectors import (
    ConnectorContext,
    ConnectorDescriptor,
    ConnectorRegistry,
)


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        payload = client.get(f"/api/v1/jobs/{job_id}/status").json()
        if payload.get("status") in {"succeeded", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_inline_connector_runs_through_the_normal_job_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    client = TestClient(app)
    descriptors = client.get("/api/v1/connectors").json()["connectors"]
    assert {item["connector_id"] for item in descriptors} >= {
        "inline_table",
        "job_artifact",
    }
    response = client.post(
        "/api/v1/connectors/jobs",
        json={
            "goal": "按 city 统计 amount 合计",
            "sources": [
                {
                    "connector_id": "inline_table",
                    "config": {
                        "file_name": "sales.csv",
                        "records": [
                            {"city": "A", "amount": 10},
                            {"city": "A", "amount": 20},
                            {"city": "B", "amount": 5},
                        ],
                    },
                }
            ],
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    terminal = _wait(client, job_id)
    assert terminal["status"] == "succeeded", terminal
    snapshot = client.get(f"/api/v1/jobs/{job_id}/session").json()
    assert snapshot["status"] == "succeeded"
    assert client.get(f"/api/v1/jobs/{job_id}/files/final_result.xlsx").status_code == 200


class _EmptyConfig(BaseModel):
    pass


class _EscapingConnector:
    descriptor = ConnectorDescriptor(
        connector_id="escaping_test",
        version="1",
        name="escaping",
        source_kind="test",
        produces=[".csv"],
    )
    config_model = _EmptyConfig

    def materialize(
        self,
        config: BaseModel,
        destination: Path,
        context: ConnectorContext,
    ) -> list[Path]:
        path = destination.parent / "escaped.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("id\n1\n", encoding="utf-8")
        return [path]


def test_connector_registry_rejects_paths_outside_the_job_upload_directory(
    tmp_path: Path,
) -> None:
    registry = ConnectorRegistry()
    registry.register(_EscapingConnector())
    with pytest.raises(ValueError, match="目录边界"):
        registry.materialize(
            "escaping_test",
            {},
            tmp_path / "uploads",
            ConnectorContext(tenant_id="default", job_id="a" * 32),
        )
