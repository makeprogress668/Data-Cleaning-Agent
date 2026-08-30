from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from data_agent.cli import main as cli_main
from data_agent.services import answer_input_paths, discover_input_paths


def test_answer_input_paths_generates_business_delivery_package(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    pd.DataFrame(
        {
            "record_id": [1, 2, 3],
            "sitecode": ["S001", "S002", "S404"],
            "owner_code": ["O001", "O002", "O999"],
            "source_system": ["ERP", "CRM", "ERP"],
            "amount": [100, 200, None],
        }
    ).to_excel(input_dir / "raw_sitecode.xlsx", index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
            "city": ["北京", "上海"],
        }
    ).to_excel(input_dir / "buildings.xlsx", index=False)
    pd.DataFrame(
        {
            "owner_code": ["O001", "O002"],
            "owner_name": ["张三", "李四"],
            "owner_dept": ["销售", "运营"],
        }
    ).to_excel(input_dir / "owners.xlsx", index=False)

    result = answer_input_paths(
        [input_dir],
        goal=(
            "清洗 sitecode 数据，输出可用的活跃楼宇清单，列出不能使用的数据原因，"
            "生成一张异常分布图和简要报告"
        ),
        output_dir=output_dir,
        include_audit=True,
    )

    assert result.answer.data_quality_score < 100
    assert result.answer.usable_records_count == 2
    assert result.answer.exception_records_count == 1
    assert (output_dir / "business_answer.html").exists()
    assert (output_dir / "business_answer.md").exists()
    assert (output_dir / "final_result.xlsx").exists()
    assert (output_dir / "charts" / "exception_distribution.html").exists()
    assert not (output_dir / "charts" / "record_status_distribution.html").exists()
    assert (output_dir / "audit_package" / "cleaning_log.json").exists()
    assert (output_dir / "audit_package" / "lineage.json").exists()
    assert (output_dir / "audit_package" / "record_audit.xlsx").exists()
    # P2: the redundant exceptions_for_review.xlsx is gone; problems live in the
    # final_result 问题说明 sheet (this goal asked to "列出不能使用的数据原因").
    assert not (output_dir / "exceptions_for_review.xlsx").exists()

    final_sheets = pd.ExcelFile(output_dir / "final_result.xlsx").sheet_names

    assert final_sheets == ["处理结果", "问题说明"]

    final_result = pd.read_excel(output_dir / "final_result.xlsx", sheet_name="处理结果")
    needs_review = pd.read_excel(output_dir / "final_result.xlsx", sheet_name="问题说明")

    assert len(final_result) == 2
    # One row per record, and one label per problem. The rule engine and the
    # dirty-data engine both notice the blank amount; the user sees it stated once,
    # in the wording of the rule that was actually declared — not
    # "金额为空；空值" from two subsystems describing the same cell.
    assert len(needs_review) == 1
    assert needs_review.loc[0, "问题类型"] == "金额为空"
    assert {
        "处理状态",
        "问题类型",
        "建议处理",
        "源行号",
        "影响字段",
        "当前值",
    }.isdisjoint(final_result.columns)
    assert {"处理状态", "问题类型", "建议处理"}.issubset(needs_review.columns)
    assert "source_system" in final_result.columns
    assert "备注" not in final_result.columns
    assert "严重级别" not in needs_review.columns
    assert result.answer.data_quality_score < 100
    assert result.answer.analysis_results
    assert len(result.answer.charts) == 1
    assert "<iframe" in (output_dir / "business_answer.html").read_text(encoding="utf-8")


def test_plain_clean_goal_delivers_only_the_result_sheet(tmp_path: Path) -> None:
    """P2: a goal that does not ask to list exceptions yields exactly one sheet.

    Products are decided by the goal, not by how many operators ran — a plain
    "clean this table" goal delivers just the result table and no problem sheet or
    redundant exceptions workbook.
    """
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    pd.DataFrame(
        {
            "record_id": [1, 2, 3],
            "sitecode": ["S001", "S002", "S003"],
            "amount": [100, 200, 300],
            "optional_note": [None, "已确认", ""],
        }
    ).to_excel(input_dir / "raw_sitecode.xlsx", index=False)

    result = answer_input_paths(
        [input_dir],
        goal="清洗 sitecode 数据并输出规范化清单",  # no exception/review ask
        output_dir=output_dir,
    )

    assert (output_dir / "final_result.xlsx").exists()
    assert not (output_dir / "exceptions_for_review.xlsx").exists()
    assert not (output_dir / "business_answer.html").exists()
    assert not (output_dir / "business_answer.md").exists()
    assert not (output_dir / "charts").exists()
    assert not (output_dir / "audit_package").exists()
    final_sheets = pd.ExcelFile(output_dir / "final_result.xlsx").sheet_names
    assert final_sheets == ["处理结果"]  # single deliverable, no redundant sheets
    delivered = pd.read_excel(output_dir / "final_result.xlsx", sheet_name="处理结果")
    assert len(delivered) == 3
    # The delivered file list also omits any separate exceptions product.
    names = {ref.name for ref in result.answer.output_files}
    assert "exceptions_for_review.xlsx" not in names


def test_discover_input_paths_generates_discovery_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    pd.DataFrame({"record_id": [1, 2], "sitecode": ["S001", "S002"]}).to_csv(
        input_dir / "raw.csv",
        index=False,
    )
    pd.DataFrame({"sitecode": ["S001"], "building_status": ["active"]}).to_csv(
        input_dir / "buildings.csv",
        index=False,
    )
    (input_dir / "cleaning_rules.md").write_text("sitecode 必须属于活跃楼宇", encoding="utf-8")

    files = discover_input_paths([input_dir], output_dir=output_dir)

    assert files["business_discovery_report_html"].exists()
    assert files["business_discovery_report_md"].exists()
    assert files["data_inventory"].exists()

    # Discovery HTML must render markdown tables as real <table>, not raw pipe text.
    html = files["business_discovery_report_html"].read_text(encoding="utf-8")
    assert "<table>" in html
    assert "<th>" in html
    assert "| table |" not in html
    assert "| --- |" not in html

    inventory_sheets = pd.ExcelFile(files["data_inventory"]).sheet_names
    assert "table_profile" in inventory_sheets
    assert "relationship_candidates" in inventory_sheets
    assert "supporting_docs" in inventory_sheets


def test_answer_input_paths_handles_generic_inventory_delivery(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    pd.DataFrame(
        {
            "sku_id": ["SKU-1", "SKU-2", "SKU-3"],
            "category_code": ["C01", "C404", "C02"],
            "stock_qty": [10, 5, -3],
            "warehouse": ["WH-A", "WH-B", "WH-A"],
        }
    ).to_csv(input_dir / "inventory.csv", index=False)
    pd.DataFrame(
        {
            "category_code": ["C01", "C02"],
            "category_name": ["食品", "日用品"],
        }
    ).to_csv(input_dir / "category_ref.csv", index=False)

    result = answer_input_paths(
        [input_dir],
        goal="清洗库存数据，按 category_code 合并分类名称，输出可直接使用的数据和需要复核的问题",
        output_dir=output_dir,
        include_audit=False,
    )

    assert result.answer.usable_records_count >= 1
    # A requested problem list reports findings without silently subtracting rows
    # from the primary delivery. exception_records_count is deliberately the count
    # withheld from that delivery, not the size of the 问题说明 sheet.
    assert result.answer.exception_records_count == 0
    assert pd.ExcelFile(output_dir / "final_result.xlsx").sheet_names == ["处理结果", "问题说明"]
    final_result = pd.read_excel(output_dir / "final_result.xlsx", "处理结果")
    problems = pd.read_excel(output_dir / "final_result.xlsx", "问题说明")

    assert len(final_result) == 3
    assert {"sku_id", "category_code"}.issubset(final_result.columns)
    assert {"处理状态", "问题类型", "建议处理"}.isdisjoint(final_result.columns)
    assert "category_name" in final_result.columns
    assert "SiteCode" not in final_result.columns
    assert "楼宇状态" not in final_result.columns
    assert "负责人编码" not in final_result.columns
    assert "数值为负" in set(problems["问题类型"])


def test_cli_answer_prints_only_current_delivery_files(tmp_path: Path, monkeypatch) -> None:
    output_files = {
        "business_answer_html": tmp_path / "business_answer.html",
        "business_answer_md": tmp_path / "business_answer.md",
        "final_result": tmp_path / "final_result.xlsx",
        "data_quality_scorecard": tmp_path / "scorecard.html",
    }
    monkeypatch.setattr(
        cli_main,
        "answer_input_paths",
        lambda *args, **kwargs: SimpleNamespace(output_files=output_files),
    )

    # Regression: the CLI used to read the removed ``exceptions_for_review`` key
    # after a successful run and crash while printing its output summary.
    cli_main.answer(
        input_path=tmp_path,
        goal="清洗数据",
        output=tmp_path / "output",
        include_audit=False,
        debug=False,
        recursive=True,
    )
