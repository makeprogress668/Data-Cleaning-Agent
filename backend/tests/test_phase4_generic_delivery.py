from pathlib import Path

import pandas as pd

from data_agent.services import answer_input_paths, process_input_paths
from data_agent.utils.target_schema import is_blank_column


def _write_generic_order_inputs(input_dir: Path) -> None:
    pd.DataFrame(
        {
            "order_id": [1, 2, 3],
            "customer_id": ["C001", "C002", ""],
            "amount": [100, -5, 30],
            "status": ["paid", "cancelled", "unpaid"],
            "email": ["a@example.com", "bad-email", "c@example.com"],
        }
    ).to_csv(input_dir / "orders.csv", index=False)
    (input_dir / "cleaning_rules.md").write_text(
        "\n".join(
            [
                "customer_id 必填，不能为空",
                "amount 必须 >= 0",
                "status 允许值: paid, unpaid",
                "email 格式为邮箱",
                "导入字段: order_id, customer_id, amount, status, email",
            ]
        ),
        encoding="utf-8",
    )
    template_columns = ["order_id", "customer_id", "amount", "status", "email", "target_note"]
    pd.DataFrame(columns=template_columns).to_excel(
        input_dir / "import_template.xlsx",
        index=False,
    )


def test_process_input_paths_applies_document_rules_to_generic_data(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_generic_order_inputs(input_dir)

    response = process_input_paths(
        [input_dir],
        work_dir=tmp_path / "work",
        config={"include_job_config": True, "include_diagnostics": True, "result_limit": 0},
    )

    rule_names = {rule["name"] for rule in response["job_config"]["anomaly_rules"]}
    # Two independent sources of exceptions, one policy. Row 3 (blank customer_id)
    # is withheld by the required-field document rule. Row 2 is withheld because the
    # dirty-data engine rates amount=-5 as an error that affects the final result —
    # the same verdict the CLI has always produced for this fixture. Callers that
    # want the document-rule tiering alone can pass dirty_data.enabled=False (see
    # test_answer_input_paths_exports_document_rule_audit_for_generic_delivery).
    assert response["counts"] == {"total": 3, "valid": 1, "abnormal": 2}
    assert response["processing"]["document_rule_count"] == 4
    assert response["processing"]["missing_import_field_count"] == 1
    assert "active_building" not in response["job_config"]
    assert "doc_rule_amount_min_value" in rule_names
    assert "doc_rule_status_enum" in rule_names
    assert "doc_rule_email_format" in rule_names
    assert response["diagnostics"]["document_business_rules"]
    assert any(
        row["field"] == "target_note" and row["status"] == "missing"
        for row in response["diagnostics"]["template_validation"]
    )


def test_answer_input_paths_exports_document_rule_audit_for_generic_delivery(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    _write_generic_order_inputs(input_dir)

    result = answer_input_paths(
        [input_dir],
        goal="清洗订单数据，输出可用于系统导入的数据和异常原因",
        output_dir=output_dir,
        include_audit=True,
        config={"dirty_data": {"enabled": False}},
    )

    # Tiered document-rule policy: only the blank required field (row 3) is withheld
    # for review; the soft violations on row 2 (negative amount / bad enum / bad
    # email) are delivered with advisory tags instead of blocking the record.
    assert result.answer.usable_records_count == 2
    assert result.answer.exception_records_count == 1
    assert any("说明文档" in finding for finding in result.answer.key_findings)
    assert any("导入模板校验" in finding for finding in result.answer.key_findings)
    assert (output_dir / "audit_package" / "document_rules.xlsx").exists()

    final_sheets = pd.ExcelFile(output_dir / "final_result.xlsx").sheet_names
    final_result = pd.read_excel(output_dir / "final_result.xlsx", "处理结果")
    problems = pd.read_excel(output_dir / "final_result.xlsx", "问题说明")
    document_rules = pd.read_excel(
        output_dir / "audit_package" / "document_rules.xlsx",
        "template_validation",
    )

    # P2: the goal asked for "异常原因", so problems ship as the final_result 问题说明
    # sheet; the redundant standalone exceptions workbook is gone.
    assert not (output_dir / "exceptions_for_review.xlsx").exists()
    # The goal also asked for 系统导入 data, so the import projection is its own
    # declared sheet. It no longer silently *replaces* 处理结果, which is what made the
    # CLI and the console hand back different primary tables for the same goal.
    # 上传了空模板，处理结果本身就是模板的形状，不需要再投影一张。
    assert final_sheets == ["处理结果", "问题说明"]
    assert len(final_result) == 2
    assert "导入校验状态" not in final_result.columns
    # 模板里有 target_note，输入数据里没有对应来源。那一列留在表里、留空 ——
    # 用户要的就是这个形状，少一列会让下游接不上；而空着本身就是「没填上」的证据，
    # 不需要再加一列状态去说明它。
    assert list(final_result.columns) == [
        "order_id", "customer_id", "amount", "status", "email", "target_note",
    ]
    assert is_blank_column(final_result["target_note"])
    # Internal engineering sheets never travel inside the business deliverable.
    assert not {"record_audit", "cleaned_data"} & set(final_sheets)
    assert "target_note" in set(document_rules["field"])
    assert "missing" in set(document_rules["status"])
    # 被扣留复核的只剩必填缺失这条硬性问题。
    assert len(problems) >= 1
    assert "customer_id 为空" in ";".join(problems["问题类型"].astype(str))
    # 软性违规照常交付，但仍要以中文业务说明打标（而非内部规则名 doc_rule_*）。
    advisory_findings = ";".join(result.answer.key_findings)
    assert "amount 小于允许的最小值" in advisory_findings
    assert "doc_rule_" not in advisory_findings


def test_cli_and_api_agree_on_rows_and_quality_score(tmp_path: Path) -> None:
    """Both entry points must deliver the same rows and the same quality score.

    The API path used to skip the dirty-data axis entirely while delivery ran its own
    second engine, so the same input and goal produced different row counts and a
    systematically higher Web score. Both now read one in-executor dirty run.
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_generic_order_inputs(input_dir)
    goal = "清洗订单数据，列出不能使用的数据原因"

    api = process_input_paths(
        [input_dir],
        work_dir=tmp_path / "api",
        config={"result_limit": 0, "goal": goal},
    )
    cli = answer_input_paths([input_dir], goal=goal, output_dir=tmp_path / "cli")

    assert api["counts"]["valid"] == cli.answer.usable_records_count
    assert api["counts"]["abnormal"] == cli.answer.exception_records_count
    assert api["quality_score"] == cli.answer.data_quality_score


def test_annotation_only_goal_never_rewrites_cell_values(tmp_path: Path) -> None:
    """"标记异常，不要修改原始数据" must not trigger the auto-fixing dirty engine.

    Delivery used to build its dirty config straight from JobConfig defaults, ignoring
    the goal-derived capability gate, so it auto-fixed and pre-wrote "fixed" input
    files even when the user explicitly asked for annotation only.
    """

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": [1, 2],
            "customer_name": ["  张三  ", "李四"],
            "amount": [100, 200],
        }
    ).to_csv(input_dir / "orders.csv", index=False)

    answer_input_paths(
        [input_dir],
        goal="标记异常记录，不要修改原始数据",
        output_dir=output_dir,
    )

    # No scratch copies of "fixed" inputs are left in the user's delivery directory.
    assert not (output_dir / "_dirty_fixed_input").exists()
    delivered = pd.read_excel(output_dir / "final_result.xlsx", sheet_name="处理结果")
    assert "  张三  " in set(delivered["customer_name"].astype(str))


def test_cli_and_api_deliver_the_same_workbook(tmp_path: Path) -> None:
    """The primary workbook must not depend on which entry point produced it.

    Delivery used to assemble its own sheets — compacting columns and swapping the
    whole 处理结果 sheet for the system-import projection whenever an import template
    was uploaded — while the API shipped the executor's raw projection.
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_generic_order_inputs(input_dir)
    goal = "清洗订单数据，输出可用于系统导入的数据和异常原因"

    api = process_input_paths(
        [input_dir],
        work_dir=tmp_path / "api",
        config={"result_limit": 0, "goal": goal},
    )
    answer_input_paths([input_dir], goal=goal, output_dir=tmp_path / "cli")

    api_book = pd.ExcelFile(api["files"]["excel_path"])
    cli_book = pd.ExcelFile(tmp_path / "cli" / "final_result.xlsx")
    # 上传了空模板，处理结果本身就是模板的形状 —— 再给一张「可导入数据」
    # 是同一张表的第二份，只会让交付物更难读。
    assert api_book.sheet_names == cli_book.sheet_names == ["处理结果", "问题说明"]
    for sheet in api_book.sheet_names:
        pd.testing.assert_frame_equal(
            pd.read_excel(api_book, sheet_name=sheet),
            pd.read_excel(cli_book, sheet_name=sheet),
        )


def test_cli_and_api_reports_carry_the_same_conclusion(tmp_path: Path) -> None:
    """The report is built once, so a Web user reads what a CLI user reads.

    The API path used to hand-roll a four-number BusinessAnswer for the report while
    delivery produced findings, exception impact and recommended actions.
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_generic_order_inputs(input_dir)
    goal = "清洗订单数据，列出不能使用的数据原因，并生成简要报告"

    api = process_input_paths(
        [input_dir],
        work_dir=tmp_path / "api",
        config={"result_limit": 0, "goal": goal},
    )
    cli = answer_input_paths([input_dir], goal=goal, output_dir=tmp_path / "cli")

    api_report = Path(api["files"]["report_markdown"]).read_text(encoding="utf-8")
    cli_report = (tmp_path / "cli" / "business_answer.md").read_text(encoding="utf-8")
    # Both reports state the same conclusion and surface the same findings.
    assert cli.answer.result_summary in api_report
    for finding in cli.answer.key_findings:
        assert finding in api_report
    assert f"{cli.answer.data_quality_score}/100" in api_report
    assert f"{cli.answer.data_quality_score}/100" in cli_report
