from pathlib import Path

import pandas as pd

from data_agent.schemas import JobConfig
from data_agent.services import answer_input_paths
from data_agent.tools import analyze_dirty_data, analyze_exception_impact, compute_quality_score


def test_job_config_accepts_phase2_product_options() -> None:
    job = JobConfig.model_validate(
        {
            "goal": "清洗 sitecode 并输出可用数据",
            "output_mode": "business_answer",
            "business_views": ["standardized_dataset_view", "exception_review_view"],
            "input": {"path": "data/input", "include_unstructured_docs": True},
            "dirty_data": {"enabled": True, "auto_fix_safe_issues": True},
            "matching": {"duplicate_key_strategy": "review"},
            "quality_score": {"critical_fields": ["sitecode", "building_status"]},
            "audit": {"enabled": True, "default_visible": False},
        }
    )

    assert job.goal == "清洗 sitecode 并输出可用数据"
    assert job.output_mode == "business_answer"
    assert job.input.include_unstructured_docs is True
    assert job.dirty_data.auto_fix_safe_issues is True
    assert job.matching.duplicate_key_strategy == "review"
    assert job.quality_score.critical_fields == ["sitecode", "building_status"]


def test_dirty_data_engine_detects_and_safely_fixes_common_issues() -> None:
    df = pd.DataFrame(
        {
            "sitecode": [" S001 ", "s001", "Ｓ002", None],
            "amount": ["100", "-5", "9999", ""],
            "start_date": ["2024-01-01", "2024/01/02", "bad-date", ""],
        }
    )

    result = analyze_dirty_data({"raw": df}, auto_fix_safe_issues=True)

    issue_types = set(result.issue_summary["issue_type"])
    assert "leading_trailing_spaces" in issue_types
    assert "fullwidth_characters" in issue_types
    assert "null_values" in issue_types
    assert "negative_amount" in issue_types
    assert "date_parse_failed" in issue_types
    assert "mixed_date_formats" in issue_types
    assert result.tables["raw"].loc[0, "sitecode"] == "S001"
    assert result.tables["raw"].loc[2, "sitecode"] == "S002"
    assert not result.fix_audit.empty


def test_dirty_data_engine_detects_generic_negative_numeric_values() -> None:
    df = pd.DataFrame(
        {
            "sku": ["A1", "A2"],
            "stock_qty": [10, -3],
            "temperature_delta": [-5, 2],
        }
    )

    result = analyze_dirty_data({"inventory": df}, auto_fix_safe_issues=False)

    issues = result.issue_summary.set_index("issue_type")
    assert "negative_numeric_value" in issues.index
    assert issues.loc["negative_numeric_value", "field"] == "stock_qty"
    assert "negative_amount" not in set(result.issue_summary["issue_type"])
    assert "temperature_delta" not in set(result.record_issues["field"])


def test_dirty_data_engine_detects_structural_table_issues() -> None:
    df = pd.DataFrame(
        {
            "order_id": ["order_id", "O-1", "合计", "备注：以下为手工补录", "O-2"],
            "amount": ["amount", 100, 100, "", 200],
            "Unnamed: 2": ["extra", "", "", "", ""],
        }
    )

    result = analyze_dirty_data({"orders": df}, auto_fix_safe_issues=False)

    issue_types = set(result.issue_summary["issue_type"])
    record_issue_types = set(result.record_issues["issue_type"])
    assert "blank_or_unnamed_columns" in issue_types
    assert "repeated_header_rows" in issue_types
    assert "summary_rows_mixed_in_data" in record_issue_types
    assert "comment_rows_mixed_in_data" in record_issue_types


def test_quality_score_and_exception_impact_include_dirty_issues() -> None:
    exception_summary = pd.DataFrame(
        [
            {
                "异常类型": "未找到楼宇",
                "影响行数": 2,
                "严重级别": "错误",
                "建议处理": "补充主数据",
            }
        ]
    )
    dirty_issues = pd.DataFrame(
        [
            {
                "issue_type": "leading_trailing_spaces",
                "field": "sitecode",
                "severity": "warn",
                "affected_rows": 3,
                "affects_final_result": False,
                "auto_fixed": True,
                "requires_review": False,
                "recommended_action": "自动去除前后空格",
            }
        ]
    )

    score = compute_quality_score(
        total_records=10,
        usable_records=7,
        exception_records=3,
        exception_summary=exception_summary,
        dirty_issue_summary=dirty_issues,
    )
    impact = analyze_exception_impact(exception_summary, dirty_issues)

    assert score.score < 100
    assert "数据质量评分" in set(score.summary["指标"])
    assert "脏数据问题数" in set(score.summary["指标"])
    assert "未找到楼宇" in set(impact["异常类型"])
    assert "前后空格" in set(impact["异常类型"])
    assert impact[impact["异常类型"].eq("前后空格")].iloc[0]["是否已自动修复"] == "是"


def test_answer_delivery_exports_phase2_audit_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    pd.DataFrame(
        {
            "record_id": [1, 2, 3],
            "sitecode": [" S001 ", "S002", "S404"],
            "amount": [100, -5, 300],
        }
    ).to_excel(input_dir / "raw_sitecode.xlsx", index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
        }
    ).to_excel(input_dir / "buildings.xlsx", index=False)

    result = answer_input_paths(
        [input_dir],
        goal="清洗 sitecode 数据并输出活跃楼宇清单，并列出不能使用的异常数据",
        output_dir=output_dir,
        include_audit=True,
    )

    assert result.answer.data_quality_score < 100
    assert (output_dir / "audit_package" / "dirty_data_issues.xlsx").exists()
    assert (output_dir / "audit_package" / "data_quality_deductions.xlsx").exists()

    # P2: no separate exceptions workbook; the problem view is a sheet in
    # final_result because this goal asked to list unusable/abnormal data.
    assert not (output_dir / "exceptions_for_review.xlsx").exists()
    final_sheets = pd.ExcelFile(output_dir / "final_result.xlsx").sheet_names
    exceptions = pd.read_excel(output_dir / "final_result.xlsx", "问题说明")

    # 目标要了「不能使用的异常数据」，所以有「问题说明」；没提改动清单，就不多给一张。
    # 每多一张 sheet，都在结果和读它的人之间多放一层东西。
    assert final_sheets == ["处理结果", "问题说明"]
    assert "金额为负" in set(exceptions["问题类型"])
    assert "备注" not in exceptions.columns


def test_negative_values_are_not_flagged_when_the_column_is_negative_by_design() -> None:
    """A refund column is negative on purpose; flagging it buries the user in noise.

    "amount must not be negative" is inferred from the column *name*, so a refund /
    discount / adjustment column used to have every one of its rows withheld for
    review — the opposite of a usable deliverable.
    """

    refunds = pd.DataFrame({"refund_id": [1, 2, 3], "amount": [-100, -50, -20]})
    orders = pd.DataFrame({"order_id": [1, 2, 3], "amount": [100, -5, 30]})

    refund_issues = analyze_dirty_data({"refunds": refunds}, auto_fix_safe_issues=False)
    order_issues = analyze_dirty_data({"orders": orders}, auto_fix_safe_issues=False)

    assert "negative_amount" not in set(refund_issues.issue_summary["issue_type"])
    # A stray negative among positives is still bad data and still gets flagged.
    assert "negative_amount" in set(order_issues.issue_summary["issue_type"])
