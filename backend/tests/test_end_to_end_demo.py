from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


def test_end_to_end_demo_script_generates_complete_delivery_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_demo_module()
    demo_base_root = tmp_path / "demo"
    monkeypatch.setattr(module, "DEMO_BASE_ROOT", demo_base_root)

    module.main()

    for scenario in (
        "phase4_unstructured_rules_demo",
        "order_delivery_demo",
        "supplier_master_demo",
        "asset_register_demo",
    ):
        _assert_complete_delivery_package(demo_base_root / scenario)
    assert (demo_base_root / "phase5_acceptance_suite" / "acceptance_summary.md").exists()
    assert (demo_base_root / "phase5_acceptance_suite" / "acceptance_summary.xlsx").exists()


def _assert_complete_delivery_package(demo_root: Path) -> None:
    output_dir = demo_root / "output"
    assert (demo_root / "discovery" / "business_discovery_report.html").exists()
    assert (demo_root / "discovery" / "data_inventory.xlsx").exists()
    assert (output_dir / "business_answer.html").exists()
    assert (output_dir / "business_answer.md").exists()
    assert (output_dir / "final_result.xlsx").exists()
    assert (output_dir / "audit_package" / "document_rules.xlsx").exists()
    assert (output_dir / "audit_package" / "lineage.json").exists()
    assert (output_dir / "charts" / "record_status_distribution.html").exists()
    # P2: no redundant exceptions workbook; problems (when the goal asked for them)
    # live in the final_result 问题说明 sheet.
    assert not (output_dir / "exceptions_for_review.xlsx").exists()

    final_excel = pd.ExcelFile(output_dir / "final_result.xlsx")
    # The result table is always delivered; the problem sheet and the target-system
    # import projection are goal-driven, declared OutputSpec artifacts. Nothing else —
    # in particular no internal engineering sheet — may appear in the deliverable.
    assert final_excel.sheet_names[0] == "处理结果"
    assert set(final_excel.sheet_names) <= {"处理结果", "问题说明", "可导入数据"}

    final_result = pd.read_excel(output_dir / "final_result.xlsx", "处理结果")
    template_validation = pd.read_excel(
        output_dir / "audit_package" / "document_rules.xlsx",
        "template_validation",
    )

    assert len(final_result) >= 1
    if "问题说明" in final_excel.sheet_names:
        needs_review = pd.read_excel(output_dir / "final_result.xlsx", "问题说明")
        assert len(needs_review) >= 1
    assert "missing" in set(template_validation["status"])
    assert not list(demo_root.rglob("~$*"))


def _load_demo_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_end_to_end_demo.py"
    spec = importlib.util.spec_from_file_location("run_end_to_end_demo", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load demo script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
