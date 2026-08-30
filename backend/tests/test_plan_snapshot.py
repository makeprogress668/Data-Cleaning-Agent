import shutil
from pathlib import Path

import pandas as pd
import pytest

import data_agent.services.processing as processing
from data_agent.observability import capture_llm_usage, record_llm_usage
from data_agent.services import (
    build_plan_snapshot,
    execute_planned_job,
    hydrate_plan_snapshot,
    plan_input_paths,
)


def _inputs(tmp_path: Path) -> list[Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    pd.DataFrame(
        {
            "order_id": ["O1", "O2"],
            "customer_id": ["C1", "C2"],
            "amount": [100, 200],
        }
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "customer_name": ["Acme", "Globex"],
        }
    ).to_csv(customers, index=False)
    return [orders, customers]


def test_snapshot_round_trip_executes_without_replanning(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(
        inputs,
        tmp_path / "job",
        config={"goal": "清洗订单并合并客户名称", "include_diagnostics": True},
    )
    snapshot = build_plan_snapshot(plan, inputs)

    hydrated = hydrate_plan_snapshot(snapshot, tmp_path / "job")
    response = execute_planned_job(hydrated, inputs)

    assert len(snapshot.plan_hash) == 64
    assert snapshot.version == 7
    assert len(snapshot.plan_id) == 32
    assert len(snapshot.task_spec_hash) == 64
    assert len(snapshot.input_fingerprints) == 2
    assert snapshot.planner_metadata.goal_prompt_version
    assert snapshot.planner_metadata.planner_prompt_version
    assert len(snapshot.planner_metadata.capability_registry_hash) == 64
    assert snapshot.compiler_version == processing.PLAN_COMPILER_VERSION
    assert hydrated.job_config["base_table"] == plan.job_config["base_table"]
    assert response["status"] == "success"
    assert response["plan"]["main_table"] == plan.job_config["base_table"]


def test_snapshot_input_identity_survives_a_different_worker_staging_root(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "api-worker")
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    worker_inputs = []
    for source in inputs:
        destination = tmp_path / "arq-worker" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        worker_inputs.append(destination)
    snapshot["input_paths"] = [str(path) for path in worker_inputs]

    hydrated = hydrate_plan_snapshot(snapshot, tmp_path / "worker-job")

    assert hydrated.tables


def test_v4_snapshot_remains_readable_after_confirmation_policy_upgrade(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs)
    payload = snapshot.model_dump(mode="json")
    payload["version"] = 4
    for step in payload["execution_plan"]["steps"]:
        step.pop("confirmation_reasons")
        step.pop("impact_estimate")
    payload["plan_hash"] = processing._plan_hash(
        snapshot.job_config,
        snapshot.execution_plan,
        snapshot.output_spec,
        snapshot.planner_metadata,
        snapshot.schema_fingerprint,
        plan_id=snapshot.plan_id,
        task_spec_hash=snapshot.task_spec_hash,
        input_fingerprints=snapshot.input_fingerprints,
        goal_plan=snapshot.goal_plan,
        legacy_confirmation_policy=True,
    )

    hydrated = hydrate_plan_snapshot(payload, tmp_path / "job")

    assert hydrated.execution_plan.steps


def test_tampered_snapshot_hash_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    snapshot["job_config"]["base_table"] = "customers"

    with pytest.raises(ValueError, match="plan hash"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_tampered_output_spec_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    snapshot["output_spec"]["primary_artifact"]["name"] = "额外结果"

    with pytest.raises(ValueError, match="plan hash"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_tampered_planner_version_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    snapshot["planner_metadata"]["planner_prompt_version"] = "tampered"

    with pytest.raises(ValueError, match="plan hash"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_tampered_compiler_version_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    snapshot["compiler_version"] = "tampered"

    with pytest.raises(ValueError, match="plan hash"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_tampered_task_spec_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs).model_dump(mode="json")
    snapshot["goal_plan"]["task_spec"]["objective"] = "被替换的目标"

    with pytest.raises(ValueError, match="TaskSpec hash"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_snapshot_captures_task_local_llm_usage(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    with capture_llm_usage():
        record_llm_usage(
            operation="planning",
            model="test-model",
            latency_ms=12.5,
            attempts=1,
            status="succeeded",
            usage={"prompt_tokens": 10, "completion_tokens": 4},
        )
        snapshot = build_plan_snapshot(plan, inputs)

    assert snapshot.llm_usage[0]["model"] == "test-model"
    assert snapshot.llm_usage[0]["total_tokens"] == 14


def test_snapshot_is_rejected_when_input_schema_changes(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs)

    pd.DataFrame(
        {
            "order_id": ["O1"],
            "customer_id": ["C1"],
            "amount": [100],
            "new_field": ["changed"],
        }
    ).to_csv(inputs[0], index=False)

    with pytest.raises(ValueError, match="数据结构已变化"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")


def test_snapshot_is_rejected_when_values_change_but_schema_does_not(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    plan = plan_input_paths(inputs, tmp_path / "job", config={"goal": "清洗订单"})
    snapshot = build_plan_snapshot(plan, inputs)

    pd.DataFrame(
        {
            "order_id": ["O1", "O2"],
            "customer_id": ["C1", "C2"],
            "amount": [999, 200],
        }
    ).to_csv(inputs[0], index=False)

    with pytest.raises(ValueError, match="输入文件内容已变化"):
        hydrate_plan_snapshot(snapshot, tmp_path / "job")
