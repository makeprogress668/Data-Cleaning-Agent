import json

import pytest

from data_agent.agent.llm_client import LLMClient, LLMConfig
from data_agent.capabilities import build_execution_plan
from data_agent.observability import (
    ExecutionRecorder,
    StageRecord,
    bind_execution_events,
    capture_llm_usage,
)
from data_agent.schemas import JobConfig


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_llm_usage_captures_tokens_latency_and_operation() -> None:
    def opener(_request, timeout=None):  # noqa: ANN001
        return _Response(
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "total_tokens": 17,
                },
            }
        )

    client = LLMClient(
        LLMConfig(base_url="https://example.test/v1", api_key="test", model="model-v1"),
        opener=opener,
    )
    with capture_llm_usage() as usage:
        client.complete(
            [
                {
                    "role": "system",
                    "content": "goal-understanding structured extraction",
                }
            ]
        )

    assert len(usage) == 1
    assert usage[0]["operation"] == "goal_understanding"
    assert usage[0]["model"] == "model-v1"
    assert usage[0]["prompt_tokens"] == 12
    assert usage[0]["completion_tokens"] == 5
    assert usage[0]["total_tokens"] == 17
    assert usage[0]["latency_ms"] >= 0
    assert usage[0]["status"] == "succeeded"


def _plan():
    return build_execution_plan(
        JobConfig.model_validate(
            {
                "sources": {"orders": {"path": "orders.csv"}},
                "base_table": "orders",
                "analysis": {"enabled": False},
                "charts": {"enabled": False},
                "report": {"formats": []},
                "audit": {"enabled": False},
            }
        )
    )


def test_recorded_stages_bind_to_their_plan_steps_with_real_numbers() -> None:
    plan = _plan()
    recorder = ExecutionRecorder()
    with recorder.stage("read_tables") as stage:
        stage.output_rows = 10
    with recorder.stage("profile_dataset", input_rows=10):
        pass
    with recorder.stage("export_table", input_rows=10) as stage:
        stage.output_rows = 8

    events = bind_execution_events(plan, recorder.stages)

    assert [event.step_id for event in events] == [step.step_id for step in plan.steps]
    assert all(event.capability_version == "1.0.0" for event in events)
    export_event = next(
        event for event in events if event.capability_id == "export_table"
    )
    assert export_event.status == "succeeded"
    assert export_event.output_rows == 8
    # Real observed timing, not a placeholder.
    assert export_event.duration_ms is not None and export_event.duration_ms >= 0


def test_unexecuted_plan_steps_are_reported_as_skipped_not_invented_successes() -> None:
    plan = _plan()
    recorder = ExecutionRecorder()
    with recorder.stage("read_tables") as stage:
        stage.output_rows = 10

    events = bind_execution_events(plan, recorder.stages)

    by_capability = {event.capability_id: event for event in events}
    assert by_capability["read_tables"].status == "succeeded"
    # Nothing else ran, so nothing else may claim to have succeeded or to have
    # processed any rows.
    skipped = [event for event in events if event.capability_id != "read_tables"]
    assert skipped and all(event.status == "skipped" for event in skipped)
    assert all(event.input_rows is None and event.output_rows is None for event in skipped)


def test_a_failing_stage_is_recorded_as_failed() -> None:
    recorder = ExecutionRecorder()
    with pytest.raises(ValueError):
        with recorder.stage("export_table", input_rows=3):
            raise ValueError("disk full")

    stage = recorder.stages[0]
    assert stage.status == "failed"
    assert stage.error_code == "ValueError"


def test_v2_plan_rejects_an_unregistered_runtime_stage() -> None:
    with pytest.raises(ValueError, match="能力注册表之外"):
        bind_execution_events(_plan(), [StageRecord(capability_id="python_eval")])


def test_v2_recorder_blocks_an_unplanned_stage_before_it_runs() -> None:
    recorder = ExecutionRecorder(_plan())
    implementation_ran = False

    with pytest.raises(ValueError, match="未在计划中声明"):
        with recorder.stage("summarize"):
            implementation_ran = True

    assert implementation_ran is False
    assert recorder.stages == []


def test_v2_plan_rejects_an_unplanned_registered_stage() -> None:
    with pytest.raises(ValueError, match="未在计划中声明"):
        bind_execution_events(_plan(), [StageRecord(capability_id="summarize")])


def test_v2_plan_rejects_runtime_stages_out_of_order() -> None:
    stages = [
        StageRecord(capability_id="export_table"),
        StageRecord(capability_id="read_tables"),
    ]
    with pytest.raises(ValueError, match="执行顺序与计划不一致"):
        bind_execution_events(_plan(), stages)


def test_v1_plan_keeps_legacy_permissive_event_binding() -> None:
    legacy_plan = _plan().model_copy(update={"version": 1})

    events = bind_execution_events(
        legacy_plan,
        [StageRecord(capability_id="summarize")],
    )

    assert all(event.status == "skipped" for event in events)


def test_registry_gate_rejects_row_budget_and_undeclared_engine() -> None:
    recorder = ExecutionRecorder(_plan())
    with pytest.raises(ValueError, match="row budget"):
        with recorder.stage("read_tables", input_rows=5_000_001):
            pass

    recorder = ExecutionRecorder(_plan())
    with pytest.raises(ValueError, match="undeclared engine"):
        with recorder.stage("read_tables", metrics={"engine": "polars"}):
            pass


def test_registry_gate_enforces_runtime_acceptance_rules() -> None:
    job = JobConfig.model_validate(
        {
            "sources": {"orders": {"path": "orders.csv"}},
            "base_table": "orders",
            "formulas": [{"output": "name", "op": "trim", "source": "name"}],
            "analysis": {"enabled": False},
            "charts": {"enabled": False},
            "report": {"formats": []},
            "audit": {"enabled": False},
        }
    )
    recorder = ExecutionRecorder(build_execution_plan(job))

    with pytest.raises(ValueError, match="row count is unchanged"):
        with recorder.stage("trim", input_rows=2) as stage:
            stage.output_rows = 1
