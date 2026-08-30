import json
from pathlib import Path

import pytest

from data_agent.evaluation import (
    compare_replay_reports,
    load_golden_tasks,
    merge_golden_reports,
    rescore_golden_report,
    run_golden_suite,
)
from data_agent.evaluation.metrics import aggregate_replay_metrics


def test_golden_suite_covers_multiple_domains() -> None:
    tasks = load_golden_tasks()

    assert 100 <= len(tasks) <= 200
    assert len({task.domain for task in tasks}) >= 8
    assert len({task.scenario_id for task in tasks}) >= 12
    tags = {tag for task in tasks for tag in task.tags}
    assert {
        "paraphrase",
        "negative_operation",
        "delete_ratio_01",
        "delete_ratio_50",
        "delete_ratio_99",
        "two_level_header",
        "numeric_strings",
        "formula_injection",
        "reshape",
        "union",
    } <= tags
    assert any(task.expectations.charts for task in tasks)
    assert any(task.expectations.report_enabled for task in tasks)


def test_golden_suite_replays_production_pipeline(tmp_path: Path) -> None:
    report = run_golden_suite(tmp_path, run_id="test-run")

    assert report["metrics"]["pass_rate"] == 1.0
    assert report["metrics"]["intent_coverage"] == 1.0
    assert report["metrics"]["capability_coverage"] == 1.0
    assert report["metrics"]["output_compliance"] == 1.0
    assert report["metrics"]["result_semantics"] == 1.0
    assert report["metrics"]["task_count"] >= 100
    assert report["metrics"]["evaluated_count"] >= 40
    assert report["metrics"]["skipped_count"] > 0
    assert len(report["versions"]["capability_registry"]) == 64
    assert report["versions"]["plan_compiler"]
    assert len(report["versions"]["source_tree"]) == 64
    report_path = Path(report["report_path"])
    assert report_path.exists()
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["run_id"] == "test-run"
    assert all(
        item["actual"]["plan_hash"]
        for item in persisted["results"]
        if not item.get("skipped")
    )


def test_golden_suite_suffixes_repeated_run_id_without_overwrite(
    tmp_path: Path,
) -> None:
    tasks = load_golden_tasks()[:1]

    first = run_golden_suite(tmp_path, tasks=tasks, run_id="repeatable")
    second = run_golden_suite(tmp_path, tasks=tasks, run_id="repeatable")

    assert first["run_id"] == "repeatable"
    assert second["run_id"] == "repeatable-2"
    assert Path(first["report_path"]).parent.name == "repeatable"
    assert Path(second["report_path"]).parent.name == "repeatable-2"
    assert json.loads(Path(first["report_path"]).read_text(encoding="utf-8"))[
        "run_id"
    ] == "repeatable"


def test_saved_artifacts_can_be_rescored_without_reexecution(tmp_path: Path) -> None:
    task = next(item for item in load_golden_tasks() if not item.requires_llm)
    source = run_golden_suite(tmp_path, tasks=[task], run_id="source")

    rescored = rescore_golden_report(
        source["report_path"],
        tmp_path,
        run_id="rescored",
    )

    assert rescored["metrics"]["pass_rate"] == 1.0
    assert rescored["rescore"] == {"mode": "artifact_only", "llm_calls": 0}
    assert rescored["rescored_from"] == str(Path(source["report_path"]).resolve())
    assert len(rescored["versions"]["golden_suite"]) == 64
    assert Path(rescored["report_path"]).is_file()


def test_targeted_rerun_can_replace_one_result_in_a_full_report(tmp_path: Path) -> None:
    tasks = [item for item in load_golden_tasks() if not item.requires_llm][:2]
    base = run_golden_suite(tmp_path, tasks=tasks, run_id="merge-base")
    replacement = run_golden_suite(
        tmp_path,
        tasks=[tasks[1]],
        run_id="merge-target",
    )

    merged = merge_golden_reports(
        base["report_path"],
        [replacement["report_path"]],
        tmp_path,
        run_id="merged",
    )

    assert merged["metrics"]["task_count"] == 2
    assert merged["metrics"]["pass_rate"] == 1.0
    assert merged["merge"]["llm_calls"] == 0
    assert merged["merge"]["replaced_task_ids"] == [tasks[1].task_id]
    by_task = {item["task_id"]: item for item in merged["results"]}
    assert Path(by_task[tasks[1].task_id]["artifact_source"]).parent.name == "merge-target"


def test_golden_suite_rejects_unsafe_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        run_golden_suite(
            tmp_path,
            tasks=load_golden_tasks()[:1],
            run_id="../outside",
        )


def test_deterministic_replay_explicitly_skips_model_only_phrasings(
    tmp_path: Path,
) -> None:
    task = next(item for item in load_golden_tasks() if item.requires_llm)

    report = run_golden_suite(tmp_path, tasks=[task], run_id="model-skip")

    assert report["metrics"]["task_count"] == 1
    assert report["metrics"]["evaluated_count"] == 0
    assert report["metrics"]["skipped_count"] == 1
    assert report["results"][0]["skip_reason"] == "requires configured LLM"
    assert report["metrics"]["llm_calls"] == 0


def test_empty_replay_metrics_keep_the_release_report_schema() -> None:
    metrics = aggregate_replay_metrics([])

    assert metrics["evaluated_count"] == 0
    assert metrics["skipped_count"] == 0
    assert metrics["scenario_count"] == 0
    assert metrics["tag_coverage"] == []
    assert metrics["llm_calls"] == 0


def test_replay_comparison_reports_task_regressions() -> None:
    baseline = {
        "metrics": {
            "pass_rate": 1.0,
            "intent_coverage": 1.0,
            "capability_coverage": 1.0,
            "output_compliance": 1.0,
        },
        "results": [{"task_id": "orders", "passed": True}],
    }
    current = {
        "metrics": {
            "pass_rate": 0.0,
            "intent_coverage": 0.5,
            "capability_coverage": 1.0,
            "output_compliance": 1.0,
        },
        "results": [{"task_id": "orders", "passed": False}],
    }

    comparison = compare_replay_reports(current, baseline)

    assert comparison["passed"] is False
    assert comparison["regressions"] == ["orders"]
    assert comparison["metric_deltas"]["pass_rate"] == -1.0


def test_replay_comparison_rejects_removed_baseline_task() -> None:
    baseline = {
        "metrics": {"pass_rate": 1.0},
        "results": [{"task_id": "orders", "passed": True}],
    }
    current = {
        "metrics": {"pass_rate": 1.0},
        "results": [],
    }

    comparison = compare_replay_reports(current, baseline)

    assert comparison["passed"] is False
    assert comparison["missing_tasks"] == ["orders"]
    assert comparison["regressions"] == ["orders"]
