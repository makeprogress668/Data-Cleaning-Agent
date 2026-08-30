import json
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from data_agent.api import app
from data_agent.api.job_store import write_job_status

_TERMINAL = {"succeeded", "failed", "cancelled"}


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    """Poll a job's status until it reaches a terminal state (async execution)."""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}/status")
        assert response.status_code == 200
        payload = response.json()
        if payload.get("status") in _TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time: {payload}")


def test_api_upload_returns_cleaning_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    raw_csv = "record_id,sitecode,amount\n1,S001,100\n2,S002,200\n"
    buildings_csv = (
        "sitecode,building_id,building_status,city\n"
        "S001,B001,active,北京\n"
        "S002,B002,inactive,上海\n"
    )

    response = client.post(
        "/api/v1/cleaning/process",
        files=[
            ("files", ("raw_sitecode.csv", raw_csv.encode("utf-8"), "text/csv")),
            ("files", ("buildings.csv", buildings_csv.encode("utf-8"), "text/csv")),
        ],
        data={"config": json.dumps({"result_limit": 10}, ensure_ascii=False)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["job_id"]
    assert payload["counts"] == {"total": 2, "valid": 2, "abnormal": 0}
    assert payload["result_truncated"] is False
    assert payload["files"]["download_url"].endswith("/result.xlsx")
    assert payload["summary"]
    assert payload["result_rows"]

    status_response = client.get(f"/api/v1/cleaning/jobs/{payload['job_id']}")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "succeeded"
    assert status_payload["counts"] == payload["counts"]
    assert status_payload["files"]["download_url"] == payload["files"]["download_url"]

    download_response = client.get(payload["files"]["download_url"])
    assert download_response.status_code == 200
    assert (
        download_response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_api_upload_accepts_supporting_rule_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    orders_csv = "order_id,amount,status\n1,100,paid\n2,-5,cancelled\n"
    rules_md = "amount 必须 >= 0\nstatus 允许值: paid, unpaid\n"

    response = client.post(
        "/api/v1/cleaning/process",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("cleaning_rules.md", rules_md.encode("utf-8"), "text/markdown")),
        ],
        data={"config": json.dumps({"include_diagnostics": True}, ensure_ascii=False)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["processing"]["document_rule_count"] >= 2
    assert payload["diagnostics"]["document_business_rules"]


def test_job_api_wraps_legacy_processing_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    raw_csv = "record_id,sitecode,amount\n1,S001,100\n"
    buildings_csv = "sitecode,building_id,building_status,city\nS001,B001,active,北京\n"

    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("raw_sitecode.csv", raw_csv.encode("utf-8"), "text/csv")),
            ("files", ("buildings.csv", buildings_csv.encode("utf-8"), "text/csv")),
        ],
        data={"goal": "清洗站点数据", "mode": "answer"},
    )

    assert response.status_code == 202
    payload = response.json()
    job_id = payload["job_id"]

    status_payload = _wait_for_job(client, job_id)
    assert status_payload["status"] == "succeeded"
    assert status_payload["goal"] == "清洗站点数据"
    assert status_payload["review_count"] == 0
    assert status_payload["counts"] == {"total": 1, "valid": 1, "abnormal": 0}
    assert status_payload["has_discovery"] is True
    assert status_payload["has_plan"] is True
    assert status_payload["has_result"] is True
    assert set(status_payload["input_files"]) == {"raw_sitecode.csv", "buildings.csv"}

    jobs_response = client.get("/api/v1/jobs?limit=10")
    assert jobs_response.status_code == 200
    listed = jobs_response.json()["jobs"]
    assert listed[0]["job_id"] == job_id
    assert listed[0]["goal"] == "清洗站点数据"
    assert listed[0]["has_result"] is True

    files_response = client.get(f"/api/v1/jobs/{job_id}/files")
    assert files_response.status_code == 200
    assert files_response.json()["files"]["download_url"].endswith("/result.xlsx")

    business_answer_response = client.get(f"/api/v1/jobs/{job_id}/business-answer")
    assert business_answer_response.status_code == 200
    business_answer = business_answer_response.json()
    assert business_answer["goal"] == "清洗站点数据"
    assert "goal_understanding" in business_answer
    assert business_answer["output_spec"]["primary_artifact"]["name"] == "处理结果"
    assert business_answer["data_quality_score"] == 100
    assert business_answer["key_metrics"]
    assert business_answer["charts"] == []
    assert business_answer["exception_impact"] == []
    assert business_answer["review_items"] == []
    assert business_answer["output_files"][0]["name"] == "final_result.xlsx"

    download_response = client.get(f"/api/v1/jobs/{job_id}/files/final_result.xlsx")
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    events_response = client.get(f"/api/v1/jobs/{job_id}/events")
    assert events_response.status_code == 200
    events = events_response.json()
    assert events["plan_hash"]
    assert events["execution_duration_ms"] >= 0
    assert events["execution_events"]
    # Events are recorded as stages actually run, so every one carries real timing and
    # nothing claims success without having executed.
    executed = [
        item for item in events["execution_events"] if item["status"] == "succeeded"
    ]
    assert executed
    assert all(item["duration_ms"] is not None for item in executed)
    # A successful v2 execution has no phantom plan steps: every declared stage ran.
    assert all(
        item["status"] == "succeeded" for item in events["execution_events"]
    )
    export_event = next(
        item for item in events["execution_events"] if item["capability_id"] == "export_table"
    )
    assert export_event["status"] == "succeeded"
    assert export_event["output_rows"] == len(business_answer["result_preview"])
    assert events["llm_usage_summary"]["call_count"] == 0


def test_job_api_rejects_unknown_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        files=[("files", ("orders.csv", b"order_id\nO1\n", "text/csv"))],
        data={"goal": "清洗订单", "mode": "execute_everything"},
    )

    assert response.status_code == 400


def test_job_api_generates_only_the_declared_chart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,category,amount\nO1,A,10\nO2,A,20\nO3,B,30\nO4,B,40\n",
                    "text/csv",
                ),
            )
        ],
        data={
            "goal": "按 category 分析订单并生成一张柱状图和简要报告",
            "mode": "answer",
        },
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    status_payload = _wait_for_job(client, job_id)
    assert status_payload["status"] == "succeeded"
    job_payload = client.get(f"/api/v1/jobs/{job_id}").json()
    assert [item["chart_id"] for item in job_payload["charts"]] == [
        "top_business_dimension"
    ]

    answer = client.get(f"/api/v1/jobs/{job_id}/business-answer").json()
    assert len(answer["charts"]) == 1
    chart_url = answer["charts"][0]["url"]
    assert chart_url.endswith("/top_business_dimension.html")
    assert client.get(chart_url).status_code == 200
    report_files = {
        item["name"]: item["url"]
        for item in answer["output_files"]
        if item["type"] in {"html", "markdown"}
    }
    assert set(report_files) == {"business_answer.html", "business_answer.md"}
    assert all(client.get(url).status_code == 200 for url in report_files.values())


def test_api_upload_preserves_extension_for_non_ascii_file_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    response = client.post(
        "/api/v1/cleaning/process",
        files=[
            (
                "files",
                (
                    "楼宇详细收货地址.xlsx",
                    _xlsx_bytes(
                        {
                            "record_id": [1, 2],
                            "sitecode": ["S001", "S002"],
                            "amount": [100, 200],
                        }
                    ),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
        ],
        data={"config": json.dumps({"result_limit": 10}, ensure_ascii=False)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["input"]["table_count"] >= 1
    # The table name is derived from the uploaded file stem and drives base-table
    # selection and goal understanding, so the Chinese name must survive upload.
    table_names = {str(row.get("table", "")) for row in payload["input"]["tables"]}
    assert "楼宇详细收货地址" in table_names


def test_safe_file_name_keeps_meaning_and_strips_unsafe_parts() -> None:
    from data_agent.api.app import _safe_file_name

    assert _safe_file_name("订单明细.xlsx") == "订单明细.xlsx"
    assert _safe_file_name("客户信息2024.XLSX") == "客户信息2024.xlsx"
    # Path traversal and separators never survive.
    assert _safe_file_name("../../etc/passwd.csv") == "passwd.csv"
    assert _safe_file_name("C:" + chr(92) + "tmp" + chr(92) + "x.xlsx") == "x.xlsx"
    # Control characters, Windows-reserved stems and hidden-file dots are neutralised.
    assert _safe_file_name("商品" + chr(0) + "主数据.xlsx") == "商品_主数据.xlsx"
    assert _safe_file_name("CON.xlsx") == "CON_file.xlsx"
    assert _safe_file_name("  .hidden.xlsx") == "hidden.xlsx"
    # Long CJK names stay inside filesystem name limits.
    assert len(_safe_file_name("很" * 200 + ".csv").encode("utf-8")) <= 176


def test_create_job_rejects_too_many_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_MAX_UPLOAD_FILES", "2")
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", (f"t{index}.csv", b"a,b\n1,2\n", "text/csv"))
            for index in range(3)
        ],
        data={"goal": "清洗", "mode": "answer"},
    )
    assert response.status_code == 400
    assert "最多上传" in response.json()["detail"]


def test_job_api_understands_goal_keywords_and_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        files=[
            (
                "files",
                (
                    "orders.csv",
                    b"order_id,amount\nO1,100\nO2,200\n",
                    "text/csv",
                ),
            ),
            (
                "files",
                (
                    "address.csv",
                    "order_id,收货地址\nO1,北京市朝阳区\nO2,上海市徐汇区\n".encode(),
                    "text/csv",
                ),
            ),
        ],
        data={"goal": "清洗并合并收货地址信息，输出需要复核的异常记录", "mode": "answer"},
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    _wait_for_job(client, job_id)
    business_answer = client.get(f"/api/v1/jobs/{job_id}/business-answer").json()
    understanding = business_answer["goal_understanding"]
    assert "异常识别与复核" in understanding["focus"]
    assert "异常复核清单" in understanding["requested_outputs"]
    assert understanding["task_spec"]["primary_entity"] == "address"
    assert {"lookup", "review"} <= set(understanding["task_spec"]["actions"])
    assert {
        "field_matches",
        "table_matches",
        "matched_fields",
        "data_access",
        "execution_steps",
        "capabilities",
    }.isdisjoint(understanding)


def _xlsx_bytes(data: dict[str, list[object]]) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    pd.DataFrame(data).to_excel(buffer, index=False)
    return buffer.getvalue()


def test_api_rejects_oversized_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    app_module = sys.modules["data_agent.api.app"]
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 1024)
    client = TestClient(app)

    oversized = ("record_id,value\n" + "1,x\n" * 5000).encode("utf-8")

    response = client.post(
        "/api/v1/cleaning/process",
        files=[("files", ("big.csv", oversized, "text/csv"))],
    )

    assert response.status_code == 400
    assert "上传上限" in response.json()["detail"]


def test_api_rejects_unsupported_file_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)

    response = client.post(
        "/api/v1/cleaning/process",
        files=[("files", ("payload.exe", b"MZ\x00\x00", "application/octet-stream"))],
    )

    assert response.status_code == 400
    assert "不支持的文件类型" in response.json()["detail"]


def test_async_upload_failure_cleans_partial_job_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "api_jobs"
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(work_dir))
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("keep.csv", b"id\n1\n", "text/csv")),
            ("files", ("reject.exe", b"MZ", "application/octet-stream")),
        ],
        data={"goal": "清洗数据", "mode": "answer"},
    )

    assert response.status_code == 400
    assert list(work_dir.iterdir()) == []


def test_runtime_config_endpoint_exposes_only_business_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)

    response = client.get("/api/v1/config")
    assert response.status_code == 200
    config = response.json()
    assert config["service"]["available"] is True
    assert config["data_protection"]["label"] == "标准保护"
    assert "llm" not in config
    assert "performance" not in config
    assert config["upload"]["max_upload_mb"] >= 1
    assert ".csv" in config["upload"]["allowed_extensions"]


def test_runtime_config_describes_llm_as_configured_not_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_API_KEY", "not-sent-by-this-endpoint")
    monkeypatch.setenv("DATA_AGENT_LLM_MODEL", "semantic-model")
    client = TestClient(app)

    understanding = client.get("/api/v1/config").json()["understanding"]

    assert understanding["mode"] == "llm"
    assert understanding["state"] == "configured"
    assert understanding["label"] == "语义理解已配置"
    assert "不发起付费在线探测" in understanding["description"]


def _create_answer_job(client: TestClient) -> str:
    orders_csv = "order_id,amount,status\n1,100,paid\n,-5,cancelled\n"
    rules_md = "order_id 必填\namount 必须 >= 0\nstatus 允许值: paid, unpaid\n"
    response = client.post(
        "/api/v1/jobs",
        files=[
            ("files", ("orders.csv", orders_csv.encode("utf-8"), "text/csv")),
            ("files", ("cleaning_rules.md", rules_md.encode("utf-8"), "text/markdown")),
        ],
        data={"goal": "清洗订单数据并列出异常", "mode": "answer"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    _wait_for_job(client, job_id)
    return job_id


def test_job_discovery_and_plan_endpoints_return_real_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    job_id = _create_answer_job(client)

    discovery = client.get(f"/api/v1/jobs/{job_id}/discovery")
    assert discovery.status_code == 200
    discovery_payload = discovery.json()
    assert "orders.csv" in discovery_payload["files"]
    assert any(table["name"] == "orders" for table in discovery_payload["tables"])

    plan = client.get(f"/api/v1/jobs/{job_id}/plan")
    assert plan.status_code == 200
    plan_payload = plan.json()
    assert plan_payload["main_table"]
    # Document-derived rules should surface as anomaly rules in the plan view.
    assert plan_payload["anomaly_rules"]


def test_job_plan_uses_completed_row_count_for_legacy_jobs_without_impact_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    job_id = "c" * 32
    write_job_status(
        job_id,
        "succeeded",
        config={"goal": "清洗订单"},
        payload={
            "counts": {"total": 17, "valid": 17, "abnormal": 0},
            "plan": {"main_table": "orders"},
            "goal_plan": {},
        },
    )
    client = TestClient(app)

    response = client.get(f"/api/v1/jobs/{job_id}/plan")

    assert response.status_code == 200
    assert response.json()["impact_preview"] == {
        "base_table": "orders",
        "input_rows": 17,
        "estimated_output_rows": None,
        "estimated_removed_rows": None,
        "estimated_removal_ratio": None,
        "estimate_available": False,
        "affected_fields": [],
        "added_fields": [],
    }


def test_discovery_plan_and_review_return_not_ready_for_running_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    job_id = "a" * 32
    write_job_status(job_id, "running", config={"goal": "清洗订单"})
    client = TestClient(app)

    assert client.get(f"/api/v1/jobs/{job_id}/discovery").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}/plan").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}/review").status_code == 404


def test_job_review_write_back_persists_and_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    job_id = _create_answer_job(client)

    # Empty state before any decision.
    initial = client.get(f"/api/v1/jobs/{job_id}/review")
    assert initial.status_code == 200
    assert initial.json()["decisions"] == {}
    assert initial.json()["items"]
    total = initial.json()["total"]
    assert initial.json()["pending"] == total
    status = client.get(f"/api/v1/jobs/{job_id}/status").json()
    assert status["review_count"] == total
    assert status["pending_review_count"] == total
    row_id = initial.json()["items"][0]["id"]

    # Persist a decision against a real review row.
    submit = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={
            "decisions": [
                {"row_id": row_id, "status": "accepted"},
            ]
        },
    )
    assert submit.status_code == 200
    assert submit.json()["decisions"] == {row_id: "accepted"}
    assert submit.json()["pending"] == total - 1

    # Re-read confirms persistence; a follow-up exclusion only records the business
    # decision and does not pretend the source value was edited.
    excluded = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": row_id, "status": "excluded"}]},
    )
    assert excluded.status_code == 200
    reloaded = client.get(f"/api/v1/jobs/{job_id}/review").json()
    assert reloaded["decisions"] == {row_id: "excluded"}

    fixed = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": row_id, "status": "fixed"}]},
    )
    assert fixed.status_code == 400

    # The total remains available for the review entry, while the navigation badge
    # reports only rows that still need a business decision.
    all_decisions = [
        {"row_id": item["id"], "status": "accepted"}
        for item in initial.json()["items"]
    ]
    resolved = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": all_decisions},
    )
    assert resolved.status_code == 200
    assert resolved.json()["pending"] == 0
    final_status = client.get(f"/api/v1/jobs/{job_id}/status").json()
    assert final_status["review_count"] == total
    assert final_status["pending_review_count"] == 0


def test_review_decisions_rematerialize_a_second_result_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review decisions must not duplicate a reported row already in the result."""

    work_dir = tmp_path / "api_jobs"
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(work_dir))
    client = TestClient(app)
    job_id = _create_answer_job(client)

    before = client.get(f"/api/v1/jobs/{job_id}/business-answer").json()
    assert before["can_rematerialize"] is False
    assert before["review_applied"] is None
    baseline_rows = _sheet_row_count(
        work_dir / job_id / "output" / "final_result.xlsx",
        "处理结果",
    )

    review = client.get(f"/api/v1/jobs/{job_id}/review").json()
    assert review["items"], "fixture goal must produce at least one review row"
    row_id = review["items"][0]["id"]
    assert review["items"][0]["result_index"] is not None

    # Nothing decided yet -> re-materialisation is refused rather than silently no-op.
    assert client.post(f"/api/v1/jobs/{job_id}/rematerialize").status_code == 400

    client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": row_id, "status": "accepted"}]},
    )
    assert client.get(f"/api/v1/jobs/{job_id}/business-answer").json()["can_rematerialize"]

    applied = client.post(f"/api/v1/jobs/{job_id}/rematerialize")
    assert applied.status_code == 200
    summary = applied.json()
    assert summary["accepted_rows"] == 1
    assert summary["excluded_rows"] == 0
    assert summary["file_name"] == "final_result_v2.xlsx"
    assert summary["result_row_count"] == baseline_rows

    # The accepted row remains in the new workbook exactly once, and the file is
    # downloadable.
    v2_path = work_dir / job_id / "output" / "final_result_v2.xlsx"
    assert _sheet_row_count(v2_path, "处理结果") == baseline_rows
    download = client.get(f"/api/v1/jobs/{job_id}/files/final_result_v2.xlsx")
    assert download.status_code == 200

    answer = client.get(f"/api/v1/jobs/{job_id}/business-answer").json()
    assert answer["review_applied"]["accepted_rows"] == 1
    assert answer["selected_result_version_id"] == "v2"
    assert [item["version_id"] for item in answer["result_versions"]] == ["v1", "v2"]
    assert any(
        item["name"] == "final_result_v2.xlsx" for item in answer["output_files"]
    )

    # Excluding the same row removes it, so the loop is genuinely two-way.
    client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": row_id, "status": "excluded"}]},
    )
    reverted = client.post(f"/api/v1/jobs/{job_id}/rematerialize").json()
    assert reverted["accepted_rows"] == 0
    assert reverted["excluded_rows"] == 1
    assert reverted["result_row_count"] == baseline_rows - 1
    assert reverted["version_id"] == "v3"
    assert reverted["parent_version_id"] == "v2"

    # Undo only moves the selected pointer; it never destroys later versions.
    undone = client.post(f"/api/v1/jobs/{job_id}/versions/undo").json()
    assert undone["selected_version_id"] == "v2"
    versions = client.get(f"/api/v1/jobs/{job_id}/versions").json()
    assert [item["version_id"] for item in versions["versions"]] == ["v1", "v2", "v3"]
    assert all("path" not in item and "decisions" not in item for item in versions["versions"])
    assert versions["versions"][2]["url"].endswith("/final_result_v3.xlsx")

    # Branch from v1 with an independent decision set. The new version's parent is
    # v1 even though v2/v3 continue to exist.
    branch = client.post(
        f"/api/v1/jobs/{job_id}/versions/v1/branch",
        json={"decisions": [{"row_id": row_id, "status": "accepted"}]},
    ).json()
    assert branch["version_id"] == "v4"
    assert branch["parent_version_id"] == "v1"
    assert branch["result_row_count"] == baseline_rows
    selected = client.post(f"/api/v1/jobs/{job_id}/versions/v3/select").json()
    assert selected["selected_version_id"] == "v3"
    final_answer = client.get(f"/api/v1/jobs/{job_id}/business-answer").json()
    delivered_names = {item["name"] for item in final_answer["output_files"]}
    assert {
        "final_result_v2.xlsx",
        "final_result_v3.xlsx",
        "final_result_v4.xlsx",
    } <= delivered_names


def _sheet_row_count(workbook: Path, sheet_name: str) -> int:
    return len(pd.read_excel(workbook, sheet_name=sheet_name))


def test_job_review_rejects_invalid_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    job_id = _create_answer_job(client)

    response = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": "ISSUE-1", "status": "not-a-status"}]},
    )
    assert response.status_code == 400
    assert "Invalid review status" in response.json()["detail"]


def test_job_review_pages_full_catalog_and_accepts_later_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "api_jobs"
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(work_dir))
    job_id = "b" * 32
    catalog_path = work_dir / job_id / "review_items.json"
    catalog_path.parent.mkdir(parents=True)
    items = [
        {
            "id": f"ROW-{index}",
            "issue_type": "待确认",
            "severity": "medium",
            "source_row": str(index),
            "affected_field": "amount",
            "current_value": "",
            "recommended_action": "核对",
        }
        for index in range(1, 4)
    ]
    catalog_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    write_job_status(
        job_id,
        "succeeded",
        config={"goal": "复核问题"},
        payload={
            "review_items": items[:1],
            "review_item_count": 3,
            "review_items_truncated": True,
            "review_catalog_path": str(catalog_path),
        },
    )
    client = TestClient(app)

    first = client.get(f"/api/v1/jobs/{job_id}/review?offset=0&limit=2").json()
    assert [item["id"] for item in first["items"]] == ["ROW-1", "ROW-2"]
    assert first["has_more"] is True

    second = client.get(f"/api/v1/jobs/{job_id}/review?offset=2&limit=2").json()
    assert [item["id"] for item in second["items"]] == ["ROW-3"]
    assert second["has_more"] is False

    saved = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"decisions": [{"row_id": "ROW-3", "status": "accepted"}]},
    )
    assert saved.status_code == 200
    assert saved.json()["pending"] == 2


def test_delete_job_removes_it_and_makes_status_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    job_id = _create_answer_job(client)

    # The job exists and is queryable before deletion.
    assert client.get(f"/api/v1/jobs/{job_id}/status").status_code == 200

    deleted = client.delete(f"/api/v1/jobs/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"job_id": job_id, "status": "deleted"}

    # After deletion the job is gone (404), and deleting again is also 404.
    assert client.get(f"/api/v1/jobs/{job_id}/status").status_code == 404
    assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 404


def test_delete_running_job_cancels_before_physical_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    monkeypatch.setenv("DATA_AGENT_CLARIFY_ENABLED", "0")
    started = threading.Event()
    release = threading.Event()

    def slow_execution(_plan, _paths, work_dir):
        del work_dir
        started.set()
        release.wait(timeout=5)
        return {"status": "success", "files": {}, "counts": {}}

    monkeypatch.setattr(
        "data_agent.api.job_manager.process_planned_job_with_reflection",
        slow_execution,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files=[("files", ("orders.csv", b"order_id,amount\nO1,100\n", "text/csv"))],
        data={"goal": "清洗订单", "mode": "answer"},
    )
    job_id = response.json()["job_id"]
    assert started.wait(timeout=5)

    cancelled = client.delete(f"/api/v1/jobs/{job_id}")
    assert cancelled.json() == {"job_id": job_id, "status": "cancelled"}
    assert client.get(f"/api/v1/jobs/{job_id}/status").json()["status"] == "cancelled"
    assert (tmp_path / "api_jobs" / job_id).exists()

    release.set()
    from data_agent.api.job_manager import get_job_manager

    deadline = time.time() + 5
    while get_job_manager().is_active(job_id) and time.time() < deadline:
        time.sleep(0.01)

    # The worker must not overwrite cancellation with succeeded or recreate after
    # physical deletion. Cleanup is allowed only after the Future has completed.
    assert client.get(f"/api/v1/jobs/{job_id}/status").json()["status"] == "cancelled"
    deleted = client.delete(f"/api/v1/jobs/{job_id}")
    assert deleted.json() == {"job_id": job_id, "status": "deleted"}
    assert client.get(f"/api/v1/jobs/{job_id}/status").status_code == 404


def test_delete_rejects_malformed_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    # A malformed id must never resolve to a real path.
    assert client.delete("/api/v1/jobs/not-a-real-id").status_code == 404


def test_business_answer_returns_404_while_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before a job reaches a terminal state the console must be told to keep polling."""
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    from data_agent.api.job_store import STATUS_RUNNING, write_job_status

    client = TestClient(app)

    job_id = "d" * 32
    write_job_status(job_id, STATUS_RUNNING, config={"goal": "清洗"})
    response = client.get(f"/api/v1/jobs/{job_id}/business-answer")
    assert response.status_code == 404
    assert "尚未生成" in response.json()["detail"]


def test_create_job_requires_at_least_one_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_AGENT_API_WORK_DIR", str(tmp_path / "api_jobs"))
    client = TestClient(app)
    # FastAPI rejects a missing required file field with 422 before our handler runs.
    response = client.post("/api/v1/jobs", data={"goal": "清洗"})
    assert response.status_code in {400, 422}
