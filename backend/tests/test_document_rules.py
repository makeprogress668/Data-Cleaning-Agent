import zipfile
from pathlib import Path

import pandas as pd

from data_agent.tools.document_rules import extract_document_insights


def test_extract_document_insights_builds_generic_validation_rules(tmp_path: Path) -> None:
    rules = tmp_path / "cleaning_rules.md"
    rules.write_text(
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
    tables = {
        "orders": pd.DataFrame(
            columns=["order_id", "customer_id", "amount", "status", "email"]
        )
    }

    insights = extract_document_insights([rules], tables=tables)

    rule_names = {rule["name"] for rule in insights.generated_rules}
    assert "doc_rule_customer_id_required" in rule_names
    assert "doc_rule_amount_min_value" in rule_names
    assert "doc_rule_status_enum" in rule_names
    assert "doc_rule_email_format" in rule_names
    assert set(insights.import_requirements["field"]) == {
        "order_id",
        "customer_id",
        "amount",
        "status",
        "email",
    }


def test_document_rules_apply_tiered_severity(tmp_path: Path) -> None:
    """Only record-invalidating checks block delivery; soft checks stay advisory."""

    rules = tmp_path / "cleaning_rules.md"
    rules.write_text(
        "\n".join(
            [
                "customer_id 必填，不能为空",
                "amount 必须为数值",
                "amount 必须 >= 0",
                "status 允许值: paid, unpaid",
                "email 格式为邮箱",
            ]
        ),
        encoding="utf-8",
    )
    tables = {"orders": pd.DataFrame(columns=["customer_id", "amount", "status", "email"])}

    insights = extract_document_insights([rules], tables=tables)
    severity_by_name = {rule["name"]: rule["severity"] for rule in insights.generated_rules}

    # Blocking: a required field is missing, or a value cannot be parsed as its type.
    assert severity_by_name["doc_rule_customer_id_required"] == "error"
    assert severity_by_name["doc_rule_amount_type_numeric"] == "error"
    # Advisory: value is present and parseable but imperfect (range/enum/format).
    assert severity_by_name["doc_rule_amount_min_value"] == "warn"
    assert severity_by_name["doc_rule_status_enum"] == "warn"
    assert severity_by_name["doc_rule_email_format"] == "warn"


def test_extract_document_insights_reads_markdown_json_docx_and_template(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text(
        "\n".join(
            [
                "| 字段 | 类型 | 必填 | 允许值 | 说明 |",
                "| --- | --- | --- | --- | --- |",
                "| amount | number | 是 | | 订单金额口径 |",
                "| paid_at | date | 否 | | 支付日期 |",
                "| status | string | 是 | paid, unpaid | 订单状态 |",
            ]
        ),
        encoding="utf-8",
    )
    schema = tmp_path / "schema.json"
    schema.write_text(
        """
        {
          "required": ["order_id"],
          "properties": {
            "order_id": {"type": "string", "description": "订单主键"},
            "email": {"type": "string", "format": "email"},
            "score": {"type": "number", "minimum": 0, "maximum": 100}
          }
        }
        """,
        encoding="utf-8",
    )
    docx = tmp_path / "dictionary.docx"
    _write_docx(docx, "customer_id 必填，不能为空")
    template = tmp_path / "import_template.xlsx"
    pd.DataFrame(columns=["order_id", "amount", "status", "email", "missing_target"]).to_excel(
        template,
        index=False,
    )

    tables = {
        "orders": pd.DataFrame(columns=["order_id", "customer_id", "amount", "status", "email"]),
        "import_template": pd.DataFrame(
            columns=["order_id", "amount", "status", "email", "missing_target"]
        ),
    }
    source_inventory = pd.DataFrame(
        [
            {
                "table": "import_template",
                "file": str(template),
                "file_name": template.name,
                "sheet": "",
                "file_type": "xlsx",
            }
        ]
    )

    insights = extract_document_insights(
        [tmp_path],
        tables=tables,
        source_inventory=source_inventory,
    )

    rule_names = {rule["name"] for rule in insights.generated_rules}
    assert "doc_rule_amount_type_numeric" in rule_names
    assert "doc_rule_paid_at_type_date" in rule_names
    assert "doc_rule_status_enum" in rule_names
    assert "doc_rule_email_format" in rule_names
    assert "doc_rule_score_min_value" in rule_names
    assert "doc_rule_score_max_value" in rule_names
    assert "doc_rule_customer_id_required" in rule_names
    assert "missing_target" in set(insights.import_requirements["field"])


def _write_docx(path: Path, text: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
