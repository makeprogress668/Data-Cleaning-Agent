from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from data_agent.agent import validate_reflection_overrides
from data_agent.agent.llm_client import LLMError
from data_agent.agent.reflection import (
    ReflectionSettings,
    build_reflection_messages,
    load_reflection_settings,
    propose_reflection_overrides,
    run_with_bounded_reflection,
)
from data_agent.tools.quality_score import QualityScoreResult


@dataclass(frozen=True)
class _StubRun:
    """Minimal ScoredRun for exercising the keep-best loop in isolation.

    The loop is generic over :class:`~data_agent.agent.reflection.ScoredRun`, so the
    test declares its own implementation rather than reaching into a caller's
    internals.
    """

    response: dict
    quality_result: QualityScoreResult

    @property
    def job_config(self) -> dict:
        return self.response.get("job_config", {})

    @property
    def score(self) -> int:
        return int(self.quality_result.score)

    @property
    def quality_summary(self) -> pd.DataFrame:
        return self.quality_result.summary

    @property
    def quality_deductions(self) -> pd.DataFrame:
        return self.quality_result.deductions


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "orders": pd.DataFrame(
            {
                "order_id": ["O-1", "O-2"],
                "customer_id": ["C001", "C002"],
                "amount": [100, 200],
            }
        )
    }


def _job_config() -> dict:
    return {
        "base_table": "orders",
        "quality_score": {"critical_fields": ["amount"]},
    }


# --- validate_reflection_overrides (the shared safety gate) --------------------


def test_validate_reflection_overrides_accepts_known_fields() -> None:
    merged, error = validate_reflection_overrides(
        _job_config(),
        {"quality_score": {"critical_fields": ["amount", "order_id"]}},
        _tables(),
    )
    assert error == ""
    assert merged["quality_score"]["critical_fields"] == ["amount", "order_id"]


def test_validate_reflection_overrides_rejects_unknown_field() -> None:
    merged, error = validate_reflection_overrides(
        _job_config(),
        {"quality_score": {"critical_fields": ["ghost_field"]}},
        _tables(),
    )
    assert error != ""
    # On rejection the original config is returned unchanged.
    assert merged["quality_score"]["critical_fields"] == ["amount"]


def test_validate_reflection_overrides_rejects_empty_proposal() -> None:
    _merged, error = validate_reflection_overrides(_job_config(), {}, _tables())
    assert error != ""


def test_validate_reflection_overrides_rejects_base_table_change() -> None:
    tables = {
        **_tables(),
        "customers": pd.DataFrame({"customer_id": ["C001"]}),
    }
    merged, error = validate_reflection_overrides(
        _job_config(),
        {"base_table": "customers"},
        tables,
    )

    assert error != ""
    assert merged["base_table"] == "orders"


# --- propose_reflection_overrides (LLM-facing, but LLM injected) ---------------


def _quality_result(score: int) -> QualityScoreResult:
    summary = pd.DataFrame(
        [{"类型": "指标", "指标": "数据质量评分", "值": score, "说明": ""}]
    )
    deductions = pd.DataFrame([{"扣分项": "异常率", "扣分": 100 - score, "原因": "demo"}])
    return QualityScoreResult(score=score, summary=summary, deductions=deductions)


def test_propose_skips_when_no_llm_configured() -> None:
    overrides, attempt = propose_reflection_overrides(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
        tables=_tables(),
        llm_planner=None,  # and no env LLM in test environment
    )
    assert overrides == {}
    assert attempt["status"] == "skipped"


def test_propose_accepts_validated_override() -> None:
    def fake_planner(_messages):
        return json.dumps(
            {
                "diagnosis": "critical fields incomplete",
                "overrides": {"quality_score": {"critical_fields": ["amount", "order_id"]}},
                "expected_effect": "better coverage",
            }
        )

    overrides, attempt = propose_reflection_overrides(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
        tables=_tables(),
        llm_planner=fake_planner,
    )
    assert attempt["status"] == "accepted"
    assert overrides == {"quality_score": {"critical_fields": ["amount", "order_id"]}}


def test_propose_rejects_override_referencing_unknown_field() -> None:
    def fake_planner(_messages):
        return json.dumps(
            {"overrides": {"quality_score": {"critical_fields": ["ghost"]}}}
        )

    overrides, attempt = propose_reflection_overrides(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
        tables=_tables(),
        llm_planner=fake_planner,
    )
    assert overrides == {}
    assert attempt["status"] == "rejected"


def test_propose_handles_empty_overrides_as_no_change() -> None:
    def fake_planner(_messages):
        return json.dumps({"diagnosis": "already good", "overrides": {}})

    overrides, attempt = propose_reflection_overrides(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(95).summary,
        quality_deductions=_quality_result(95).deductions,
        tables=_tables(),
        llm_planner=fake_planner,
    )
    assert overrides == {}
    assert attempt["status"] == "no_change"


def test_propose_falls_back_when_llm_raises() -> None:
    def broken_planner(_messages):
        raise LLMError("endpoint down")

    overrides, attempt = propose_reflection_overrides(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
        tables=_tables(),
        llm_planner=broken_planner,
    )
    assert overrides == {}
    assert attempt["status"] == "unavailable"


def test_build_reflection_messages_carries_quality_and_plan_context() -> None:
    messages = build_reflection_messages(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
    )
    assert messages[0]["role"] == "system"
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert context["executed_job_config"]["base_table"] == "orders"
    assert any(row["指标"] == "数据质量评分" for row in context["quality_metrics"])


# --- load_reflection_settings -------------------------------------------------


def test_load_reflection_settings_defaults(monkeypatch) -> None:
    for name in (
        "DATA_AGENT_REFLECTION_ENABLED",
        "DATA_AGENT_REFLECTION_MAX_ROUNDS",
        "DATA_AGENT_REFLECTION_TARGET_SCORE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_reflection_settings()
    assert settings.enabled is True
    assert settings.max_rounds == 1
    assert settings.target_score == 90


def test_load_reflection_settings_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AGENT_REFLECTION_ENABLED", "0")
    monkeypatch.setenv("DATA_AGENT_REFLECTION_MAX_ROUNDS", "3")
    settings = load_reflection_settings()
    assert settings.enabled is False
    assert settings.max_rounds == 3


# --- run_with_bounded_reflection (the keep-best loop, the crux) ----------------


def _bundle(score: int, job_config: dict) -> _StubRun:
    return _StubRun(
        response={"job_config": job_config, "files": {"excel_path": "x.xlsx"}},
        quality_result=_quality_result(score),
    )


def _valid_override_planner(_messages):
    return json.dumps(
        {
            "diagnosis": "improve critical field coverage",
            "overrides": {"quality_score": {"critical_fields": ["amount", "order_id"]}},
            "expected_effect": "fewer missed issues",
        }
    )


def test_loop_runs_once_when_reflection_disabled(tmp_path: Path) -> None:
    calls: list[dict] = []

    def execute(run_config, _work_dir):
        calls.append(run_config)
        return _bundle(60, _job_config())

    settings = ReflectionSettings(enabled=False, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert len(calls) == 1  # no re-run
    assert best.score == 60
    assert attempts == []


def test_loop_skips_reflection_when_target_already_met(tmp_path: Path) -> None:
    calls: list[dict] = []

    def execute(run_config, _work_dir):
        calls.append(run_config)
        return _bundle(95, _job_config())

    settings = ReflectionSettings(enabled=True, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert len(calls) == 1
    assert best.score == 95
    assert attempts[-1]["status"] == "skipped"


def test_loop_keeps_reflected_result_when_score_improves(tmp_path: Path) -> None:
    def execute(run_config, _work_dir):
        # The re-run carries job_overrides; reward it with a higher score.
        if run_config.get("job_overrides"):
            return _bundle(88, run_config["job_overrides"])
        return _bundle(70, _job_config())

    settings = ReflectionSettings(enabled=True, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert best.score == 88  # improved result kept
    assert attempts[-1]["status"] == "accepted"


def test_loop_reverts_when_reflection_does_not_improve(tmp_path: Path) -> None:
    def execute(run_config, _work_dir):
        # The re-run scores worse; the loop must keep the original.
        if run_config.get("job_overrides"):
            return _bundle(55, run_config["job_overrides"])
        return _bundle(70, _job_config())

    settings = ReflectionSettings(enabled=True, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert best.score == 70  # reflection never degrades the delivered result
    assert attempts[-1]["status"] == "reverted"


# --- Phase C: multi-round, weakest-axis targeting, verifiable tie-break --------


def test_loop_runs_multiple_rounds_and_keeps_final_improvement(tmp_path: Path) -> None:
    """max_rounds=2 with each reflected round scoring higher keeps the last run."""

    calls: list[int] = []

    def execute(run_config, _work_dir):
        calls.append(1)
        n = len(calls)
        if run_config.get("job_overrides"):
            # Reward each reflected round with a strictly higher score: 75, then 88.
            return _bundle(
                75 if n == 2 else 88,
                {**_job_config(), **run_config["job_overrides"]},
            )
        return _bundle(60, _job_config())

    settings = ReflectionSettings(enabled=True, max_rounds=2, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert len(calls) == 3  # baseline + two reflection rounds
    assert best.score == 88  # final, best-scoring run kept
    assert len(attempts) == 2
    assert attempts[-1]["status"] == "accepted"


def test_build_reflection_messages_carries_weakest_axis() -> None:
    """The reflection prompt must name the single biggest deduction to target it."""

    messages = build_reflection_messages(
        job_config=_job_config(),
        goal="清洗订单",
        quality_summary=_quality_result(70).summary,
        quality_deductions=_quality_result(70).deductions,
    )
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert context["weakest_axis"]["axis"] == "异常率"
    assert context["weakest_axis"]["points"] == 30


@dataclass
class _FakeRun:
    """A minimal ScoredRun for tie-break tests where match_rate/missing matter.

    ``_bundle`` derives its signals from empty frames (match_rate=0), so it cannot
    express a score tie with differing match rates. This lets each test set the
    verifiable signals directly.
    """

    score: int
    match_rate_value: float = 0.0
    missing_field_count: int = 0
    _config: dict | None = None

    @property
    def job_config(self) -> dict:
        return self._config if self._config is not None else _job_config()

    @property
    def quality_summary(self) -> pd.DataFrame:
        return _quality_result(self.score).summary

    @property
    def quality_deductions(self) -> pd.DataFrame:
        return _quality_result(self.score).deductions


def test_loop_breaks_tie_on_higher_match_rate(tmp_path: Path) -> None:
    """Equal scores: the run with the higher lookup match rate wins."""

    def execute(run_config, _work_dir):
        if run_config.get("job_overrides"):
            return _FakeRun(score=70, match_rate_value=0.9)
        return _FakeRun(score=70, match_rate_value=0.5)

    settings = ReflectionSettings(enabled=True, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert best.match_rate_value == 0.9  # tie broken toward the better match rate
    assert attempts[-1]["status"] == "accepted"


def test_loop_never_accepts_lower_score_despite_better_match_rate(tmp_path: Path) -> None:
    """Score is the absolute primary judge: a higher match rate can't rescue a
    lower score, so reflection still reverts."""

    def execute(run_config, _work_dir):
        if run_config.get("job_overrides"):
            return _FakeRun(score=65, match_rate_value=0.99)
        return _FakeRun(score=70, match_rate_value=0.5)

    settings = ReflectionSettings(enabled=True, max_rounds=1, target_score=90)
    best, attempts = run_with_bounded_reflection(
        execute,
        base_config={},
        output_path=tmp_path,
        validation_tables=_tables(),
        relationship_candidates=pd.DataFrame(),
        goal="清洗订单",
        settings=settings,
        llm_planner=_valid_override_planner,
    )
    assert best.score == 70  # never degrade the delivered score
    assert attempts[-1]["status"] == "reverted"
