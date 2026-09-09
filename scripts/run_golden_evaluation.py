from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Imported after the sys.path bootstrap above: that is what lets this script
# run straight from a clone, without installing the package first.
from data_agent.evaluation import (  # noqa: E402
    compare_replay_reports,
    load_golden_tasks,
    merge_golden_reports,
    rescore_golden_report,
    run_golden_suite,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay the versioned Data Alchemist golden task suite."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "evaluation",
        help="Directory that receives a timestamped replay report.",
    )
    parser.add_argument("--run-id", default=None, help="Optional stable run identifier.")
    parser.add_argument(
        "--mode",
        choices=("deterministic", "configured"),
        default="deterministic",
        help="Use the deterministic floor or the configured live model.",
    )
    parser.add_argument(
        "--cache-mode",
        choices=("off", "cold", "warm"),
        default="cold",
        help="Run with an isolated disabled, cold, or pre-warmed understanding cache.",
    )
    parser.add_argument("-k", default="", help="Only run task ids/goals containing text.")
    parser.add_argument(
        "--requires-llm-only",
        action="store_true",
        help="Run only cases whose phrasing requires semantic model understanding.",
    )
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument(
        "--rescore-report",
        type=Path,
        default=None,
        help="Reapply current expectations to saved artifacts without LLM calls.",
    )
    parser.add_argument(
        "--merge-base-report",
        type=Path,
        default=None,
        help="Full report whose targeted failures should be replaced.",
    )
    parser.add_argument(
        "--replace-report",
        type=Path,
        action="append",
        default=[],
        help="Targeted rerun report; may be supplied more than once.",
    )
    args = parser.parse_args()

    if args.merge_base_report:
        if not args.replace_report:
            parser.error("--merge-base-report requires at least one --replace-report")
        report = merge_golden_reports(
            args.merge_base_report,
            args.replace_report,
            args.output,
            run_id=args.run_id,
        )
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        print(f"report: {report['report_path']}")
        return 0 if report["metrics"]["pass_rate"] == 1.0 else 1

    if args.rescore_report:
        report = rescore_golden_report(
            args.rescore_report,
            args.output,
            run_id=args.run_id,
        )
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        print(f"report: {report['report_path']}")
        return 0 if report["metrics"]["pass_rate"] == 1.0 else 1

    tasks = load_golden_tasks()
    if args.requires_llm_only:
        tasks = [task for task in tasks if task.requires_llm]
    if args.k:
        tasks = [
            task for task in tasks
            if args.k in task.task_id or args.k in task.goal or args.k in task.tags
        ]
    if args.max_tasks > 0:
        tasks = tasks[: args.max_tasks]
    if not tasks:
        parser.error("no golden tasks matched")
    report = run_golden_suite(
        args.output,
        tasks=tasks,
        run_id=args.run_id,
        planning_mode=args.mode,
        cache_mode=args.cache_mode,
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"report: {report['report_path']}")
    passed = report["metrics"]["pass_rate"] == 1.0
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        comparison = compare_replay_reports(report, baseline)
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
        passed = passed and comparison["passed"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
