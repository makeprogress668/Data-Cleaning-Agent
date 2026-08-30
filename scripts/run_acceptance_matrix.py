"""Run a folder of real business files against written-down expectations.

The point is not to check that the agent produced *something plausible*. A plausible
answer is the dangerous case: asked to 「按城市统计」 against a table with both
发货城市 and 客户城市, it quietly picks one and hands back a summary that looks
completely reasonable. Asked to merge two months, it can hand back one month's rows
with no warning at all. Neither is visible unless you wrote down the right answer
first.

So each case carries the answer you worked out by hand, and this script reports the
difference. Three failure shapes it is built to catch, because they are the ones a
person reading the workbook will not notice:

  静默少给数据   delivered fewer rows than went in    -> rows_unchanged / delivered_rows
  静默不做       asked for a summary, got no sheet     -> summary_rows / summary_contains
  静默做选择     an ambiguity resolved without asking  -> asks_clarification

Usage::

    python scripts/run_acceptance_matrix.py cases.json            # 真实模型（默认）
    python scripts/run_acceptance_matrix.py cases.json --no-llm   # 确定性底线
    python scripts/run_acceptance_matrix.py cases.json -k 纵向     # 只跑名字含「纵向」的

Exits non-zero when any case fails, so it can gate a commit or CI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


def _load_dotenv() -> None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


class Case:
    def __init__(self, raw: dict[str, Any], data_dir: Path) -> None:
        self.name = str(raw.get("name") or "(未命名)")
        self.goal = str(raw.get("goal") or "")
        self.note = str(raw.get("note") or "")
        self.files = [data_dir / str(name) for name in raw.get("files") or []]
        self.expect = dict(raw.get("expect") or {})
        # Cases whose expected behaviour is the model's contribution — reading an
        # ambiguity, understanding a phrasing no keyword list contains. Skipped rather
        # than failed under --no-llm, so the deterministic floor is a real pass/fail
        # signal that CI can gate on instead of a number someone has to interpret.
        self.requires_llm = bool(raw.get("requires_llm"))


def run_case(case: Case, workspace: Path) -> tuple[list[str], dict[str, Any]]:
    """Execute one case. Returns (failures, observed facts)."""

    from data_agent.agent.goal_understanding import understand_goal
    from data_agent.services import answer_input_paths
    from data_agent.tools import read_tables_from_paths

    missing = [path for path in case.files if not path.exists()]
    if missing:
        return [f"找不到数据文件：{', '.join(p.name for p in missing)}"], {}

    inbox = workspace / "in"
    inbox.mkdir(parents=True, exist_ok=True)
    for path in case.files:
        shutil.copy2(path, inbox / path.name)

    tables, _inventory = read_tables_from_paths([inbox], recursive=True)
    input_rows = sum(len(frame) for frame in tables.values())

    # Understanding is read separately so a question the agent *would* have asked is
    # visible even on the CLI path, which answers straight through.
    plan = understand_goal(case.goal, tables)
    questions = [
        str(slot.get("question") or "")
        for slot in (plan.get("task_spec") or {}).get("missing_slots") or []
    ]

    result = answer_input_paths([inbox], goal=case.goal, output_dir=workspace / "out")
    workbook = Path(result.output_dir) / "final_result.xlsx"
    sheets = _read_sheets(workbook)

    observed = {
        "input_rows": input_rows,
        "sheets": list(sheets),
        "delivered_rows": len(sheets.get("处理结果", [])),
        "summary_rows": len(sheets["汇总结果"]) if "汇总结果" in sheets else None,
        "questions": questions,
        "understanding_source": plan.get("understanding_source", ""),
    }
    return _check(case.expect, sheets, observed), observed


def _check(
    expect: dict[str, Any],
    sheets: dict[str, Any],
    observed: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    result = sheets.get("处理结果")
    summary = sheets.get("汇总结果")

    if (
        expect.get("rows_unchanged") is True
        and observed["delivered_rows"] != observed["input_rows"]
    ):
        failures.append(
            f"行数变了：输入 {observed['input_rows']} 行，"
            f"交付 {observed['delivered_rows']} 行（本组不该增减记录）"
        )

    if "delivered_rows" in expect:
        want = int(expect["delivered_rows"])
        if observed["delivered_rows"] != want:
            failures.append(
                f"交付行数应为 {want}，实际 {observed['delivered_rows']}"
            )

    if "summary_rows" in expect:
        want = expect["summary_rows"]
        if want is None:
            if observed["summary_rows"] is not None:
                failures.append("本组不该产出汇总结果，却产出了")
        elif observed["summary_rows"] is None:
            failures.append(f"应产出 {want} 行汇总结果，实际没有汇总表")
        elif observed["summary_rows"] != int(want):
            failures.append(
                f"汇总行数应为 {want}，实际 {observed['summary_rows']}"
            )

    for key, frame, label in (
        ("result_contains", result, "处理结果"),
        ("summary_contains", summary, "汇总结果"),
    ):
        for wanted in expect.get(key) or []:
            if frame is None:
                failures.append(f"{label} 不存在，无法核对 {wanted}")
                continue
            if not _has_row(frame, wanted):
                failures.append(f"{label} 里找不到这一行：{wanted}")

    if result is not None:
        columns = {str(column) for column in result.columns}
        for column in expect.get("columns_present") or []:
            if str(column) not in columns:
                failures.append(f"处理结果缺少列「{column}」")
        for column in expect.get("columns_absent") or []:
            if str(column) in columns:
                failures.append(f"处理结果不该出现列「{column}」")

    if "asks_clarification" in expect:
        asked = bool(observed["questions"])
        if bool(expect["asks_clarification"]) != asked:
            failures.append(
                "本组存在歧义，应当先问用户，却直接给了结果"
                if expect["asks_clarification"]
                else f"本组不该发问，却问了：{observed['questions']}"
            )

    for sheet in expect.get("sheets_present") or []:
        if str(sheet) not in sheets:
            failures.append(f"缺少 sheet「{sheet}」")

    return failures


def _has_row(frame: Any, wanted: dict[str, Any]) -> bool:
    """Whether some row matches every key in ``wanted``, compared as text.

    Text comparison on purpose: 2500 and 2500.0 are the same answer to a business
    user, and a test that fails on the difference trains people to ignore it.
    """

    for column in wanted:
        if str(column) not in {str(name) for name in frame.columns}:
            return False
    for _index, row in frame.iterrows():
        if all(_same(row.get(column), value) for column, value in wanted.items()):
            return True
    return False


def _same(actual: Any, expected: Any) -> bool:
    try:
        return abs(float(actual) - float(expected)) < 1e-9
    except (TypeError, ValueError):
        return str(actual).strip() == str(expected).strip()


def _read_sheets(workbook: Path) -> dict[str, Any]:
    import pandas as pd

    if not workbook.exists():
        return {}
    return pd.read_excel(workbook, sheet_name=None)


def main() -> int:
    parser = argparse.ArgumentParser(description="按写好的期望跑一组真实业务数据")
    parser.add_argument("manifest", type=Path, help="用例清单 JSON")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="关闭语义理解，测确定性底线（不配模型时也必须守住的部分）",
    )
    parser.add_argument("-k", dest="keyword", default="", help="只跑名字包含该关键词的用例")
    parser.add_argument("--keep", action="store_true", help="保留产出目录以便查看工作簿")
    args = parser.parse_args()

    warnings.filterwarnings("ignore")
    _load_dotenv()
    if args.no_llm:
        os.environ["DATA_AGENT_LLM_ENABLED"] = "0"
    # A cached understanding would hide exactly what this script exists to measure.
    os.environ["DATA_AGENT_UNDERSTANDING_CACHE"] = "0"

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    data_dir = (args.manifest.parent / str(manifest.get("data_dir") or ".")).resolve()
    cases = [Case(raw, data_dir) for raw in manifest.get("cases") or []]
    if args.keyword:
        cases = [case for case in cases if args.keyword in case.name]
    if not cases:
        print("没有匹配的用例。")
        return 0

    root = Path(args.keep and (REPO_ROOT / "data" / "acceptance_runs") or tempfile.mkdtemp())
    mode = "确定性底线（无模型）" if args.no_llm else "真实模型"
    print(f"数据目录：{data_dir}\n运行模式：{mode}\n用例数：{len(cases)}\n")

    passed, failed, skipped = 0, [], 0
    for case in cases:
        if args.no_llm and case.requires_llm:
            skipped += 1
            print(f"– {case.name}　（需要语义理解，本次跳过）")
            continue
        workspace = root / case.name
        try:
            failures, observed = run_case(case, workspace)
        except Exception as exc:  # noqa: BLE001 - a crash is a result, not a stop
            failures, observed = [f"运行异常 {type(exc).__name__}: {exc}"], {}

        if failures:
            failed.append(case.name)
            print(f"✗ {case.name}　{case.goal}")
            if case.note:
                print(f"    这组在测：{case.note}")
            for line in failures:
                print(f"    - {line}")
            if observed:
                print(
                    f"    实测：输入 {observed['input_rows']} 行 → 交付 "
                    f"{observed['delivered_rows']} 行，sheets={observed['sheets']}，"
                    f"理解来源={observed['understanding_source']}"
                )
            print()
        else:
            passed += 1
            print(f"✓ {case.name}　{case.goal}")

    ran = len(cases) - skipped
    print(f"\n通过 {passed} / {ran}" + (f"（跳过 {skipped} 条需要模型的用例）" if skipped else ""))
    if failed:
        print("未通过：" + "、".join(failed))
        if args.keep:
            print(f"产出留在：{root}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
