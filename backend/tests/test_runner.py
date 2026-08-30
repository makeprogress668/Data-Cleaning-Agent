from pathlib import Path

import pandas as pd

from data_agent.pipelines import run_job
from data_agent.schemas.job import (
    AnomalyRuleConfig,
    ConditionConfig,
    ExceptionPolicyConfig,
    FormulaConfig,
    JobConfig,
    LookupConfig,
    MatchingConfig,
    SourceConfig,
)
from data_agent.schemas.output import build_output_spec
from data_agent.schemas.task import TaskSpec
from data_agent.tools.delivery_view import delivery_problem_sheet, delivery_result_sheet


def _status_job(
    raw_path,
    output_path,
    *,
    severity: str,
    policy: ExceptionPolicyConfig,
) -> JobConfig:
    return JobConfig(
        main_source=SourceConfig(path=raw_path),
        anomaly_rules=[
            AnomalyRuleConfig(
                name="status_not_active",
                condition=ConditionConfig(field="status", op="not_equals", value="active"),
                reason="状态非活跃",
                severity=severity,
            )
        ],
        exception_policy=policy,
        export={"output_file": output_path},
    )


def test_exception_policy_keeps_minor_issues_in_final(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    pd.DataFrame({"id": ["A", "B"], "status": ["active", "paused"]}).to_csv(raw, index=False)

    # warn severity + include_minor_in_final -> the flagged row stays in the final result.
    job = _status_job(
        raw,
        tmp_path / "minor_in.xlsx",
        severity="warn",
        policy=ExceptionPolicyConfig(exclude_critical_from_final=True, include_minor_in_final=True),
    )
    result = run_job(job)
    assert result.cleaned_count == 2
    assert result.abnormal_count == 0
    # Reporting a requested problem must not imply withholding it. The same row can
    # remain in the primary result and appear in the review view.
    assert len(delivery_result_sheet(result.business_result, {"id", "status"})) == 2
    problems = delivery_problem_sheet(result.business_result, {"id", "status"})
    assert problems["id"].tolist() == ["B"]


def test_exception_policy_routes_minor_issues_to_review_when_disabled(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    pd.DataFrame({"id": ["A", "B"], "status": ["active", "paused"]}).to_csv(raw, index=False)

    # include_minor_in_final=False -> even a warn-only row is routed to review.
    job = _status_job(
        raw,
        tmp_path / "minor_out.xlsx",
        severity="warn",
        policy=ExceptionPolicyConfig(
            exclude_critical_from_final=True,
            include_minor_in_final=False,
        ),
    )
    result = run_job(job)
    assert result.cleaned_count == 1
    assert result.abnormal_count == 1


def test_exception_policy_excludes_critical_from_final(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    pd.DataFrame({"id": ["A", "B"], "status": ["active", "paused"]}).to_csv(raw, index=False)

    # error severity is always routed to review under the default policy.
    job = _status_job(
        raw,
        tmp_path / "critical_out.xlsx",
        severity="error",
        policy=ExceptionPolicyConfig(exclude_critical_from_final=True, include_minor_in_final=True),
    )
    result = run_job(job)
    assert result.cleaned_count == 1
    assert result.abnormal_count == 1


def test_typed_validate_action_authorizes_declared_rule_without_word_list(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "suppliers.csv"
    pd.DataFrame({"supplier_id": ["S1", None, "S3"]}).to_csv(raw, index=False)
    job = JobConfig(
        goal="请整理 suppliers，把缺少 supplier_id 的记录及原因列出来",
        main_source=SourceConfig(path=raw),
        anomaly_rules=[
            AnomalyRuleConfig(
                name="supplier_id_null_values",
                condition=ConditionConfig(field="supplier_id", op="is_blank"),
                reason="supplier_id_null_values",
                severity="error",
            )
        ],
        exception_policy=ExceptionPolicyConfig(exclude_critical_from_final=True),
        export={"output_file": tmp_path / "result.xlsx"},
    )

    result = run_job(
        job,
        task_spec={
            "actions": ["validate", "review", "export"],
            "target_fields": [{"table": "suppliers", "field": "supplier_id"}],
        },
    )

    assert result.cleaned_count == 2
    assert result.abnormal_count == 1


def test_run_job_exports_report(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    buildings = tmp_path / "buildings.csv"
    output = tmp_path / "result.xlsx"

    pd.DataFrame({"sitecode": ["S001", "S002"]}).to_csv(raw, index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
        }
    ).to_csv(buildings, index=False)

    job = JobConfig(
        main_source=SourceConfig(path=raw),
        lookup=LookupConfig(
            source=SourceConfig(path=buildings),
            left_key="sitecode",
            right_key="sitecode",
            fields=["building_id", "building_status"],
        ),
        # Generic, config-driven anomaly rule (no domain-specific classifier).
        anomaly_rules=[
            AnomalyRuleConfig(
                name="status_not_active",
                condition=ConditionConfig(
                    field="building_status",
                    op="not_equals",
                    value="active",
                ),
                reason="状态非活跃",
                severity="error",
            )
        ],
        export={"output_file": output},
    )

    result = run_job(job)

    assert result.output_file.exists()
    assert result.cleaned_count == 1
    assert result.abnormal_count == 1

    sheets = pd.ExcelFile(output).sheet_names
    assert sheets == ["处理结果"]  # P2: default deliverable is the result table only

    result_data = pd.read_excel(output, sheet_name="处理结果")

    assert "处理状态" in result_data.columns
    assert "问题类型" in result_data.columns
    assert "建议处理" in result_data.columns
    assert "sitecode" in result_data.columns
    assert "building_status" in result_data.columns
    assert "备注" not in result_data.columns
    assert "严重级别" not in result_data.columns
    assert "确认状态" not in result_data.columns
    assert "_source_table" not in result_data.columns
    assert not any(str(column).startswith("_lookup_matched") for column in result_data.columns)
    assert result_data.set_index("sitecode").loc["S002", "问题类型"] == "状态非活跃"


def test_output_spec_accepts_lookup_suffix_for_same_named_field(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    output = tmp_path / "same_name_lookup.xlsx"
    pd.DataFrame(
        {"customer_id": ["C1"], "amount": [100]},
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {"customer_id": ["C1"], "amount": [120]},
    ).to_csv(customers, index=False)
    job = JobConfig(
        sources={
            "orders": SourceConfig(path=orders),
            "customers": SourceConfig(path=customers),
        },
        base_table="orders",
        lookups=[
            LookupConfig(
                name="customer_amount",
                source_table="customers",
                left_key="customer_id",
                right_key="customer_id",
                fields=["amount"],
            )
        ],
        export={"output_file": output},
    )

    run_job(
        job,
        output_spec=build_output_spec(
            TaskSpec(objective="关联客户金额", actions=["lookup", "export"])
        ),
    )

    result = pd.read_excel(output, sheet_name="处理结果")
    assert result.loc[0, "amount"] == 100
    assert result.loc[0, "amount_lookup"] == 120
    assert {
        "处理状态",
        "问题类型",
        "建议处理",
        "源行号",
        "影响字段",
        "当前值",
    }.isdisjoint(result.columns)


def test_lookup_uses_global_duplicate_strategy_when_not_overridden(
    tmp_path: Path,
) -> None:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    output = tmp_path / "duplicate_review.xlsx"
    pd.DataFrame({"customer_id": ["C1"]}).to_csv(orders, index=False)
    pd.DataFrame(
        {"customer_id": ["C1", "C1"], "customer_name": ["Acme A", "Acme B"]}
    ).to_csv(customers, index=False)

    job = JobConfig(
        sources={
            "orders": SourceConfig(path=orders),
            "customers": SourceConfig(path=customers),
        },
        base_table="orders",
        lookups=[
            LookupConfig(
                source_table="customers",
                left_key="customer_id",
                right_key="customer_id",
                fields=["customer_name"],
                ambiguity_field="匹配有歧义",
            )
        ],
        matching=MatchingConfig(duplicate_key_strategy="review"),
        export={"output_file": output},
    )

    run_job(job)

    result = pd.read_excel(output, sheet_name="处理结果")
    assert bool(result.loc[0, "匹配有歧义"]) is True


def test_run_job_can_export_summary_sheet_on_demand(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    buildings = tmp_path / "buildings.csv"
    output = tmp_path / "with_summary.xlsx"

    pd.DataFrame({"sitecode": ["S001", "S002"]}).to_csv(raw, index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
        }
    ).to_csv(buildings, index=False)

    job = JobConfig(
        main_source=SourceConfig(path=raw),
        lookup=LookupConfig(
            source=SourceConfig(path=buildings),
            left_key="sitecode",
            right_key="sitecode",
            fields=["building_id", "building_status"],
        ),
        anomaly_rules=[
            AnomalyRuleConfig(
                name="status_not_active",
                condition=ConditionConfig(field="building_status", op="not_equals", value="active"),
                reason="状态非活跃",
                severity="error",
            )
        ],
        export={"output_file": output, "include_summary_sheet": True},
    )

    run_job(job)

    sheets = pd.ExcelFile(output).sheet_names
    assert sheets == ["问题汇总", "处理结果"]  # summary re-added on demand
    summary = pd.read_excel(output, sheet_name="问题汇总")
    assert summary.set_index("项目").loc["需处理行数", "数量"] == 1


def test_run_job_can_export_internal_audit_sheets(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    buildings = tmp_path / "buildings.csv"
    output = tmp_path / "internal_result.xlsx"

    pd.DataFrame({"sitecode": ["S001", "S002"]}).to_csv(raw, index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
        }
    ).to_csv(buildings, index=False)

    job = JobConfig(
        main_source=SourceConfig(path=raw),
        lookup=LookupConfig(
            source=SourceConfig(path=buildings),
            left_key="sitecode",
            right_key="sitecode",
            fields=["building_id", "building_status"],
        ),
        anomaly_rules=[
            AnomalyRuleConfig(
                name="status_not_active",
                condition=ConditionConfig(field="building_status", op="not_equals", value="active"),
                reason="状态非活跃",
                severity="error",
            )
        ],
        export={"output_file": output, "include_internal_sheets": True},
    )

    run_job(job)

    sheets = pd.ExcelFile(output).sheet_names
    assert "问题汇总" in sheets
    assert "处理结果" in sheets
    assert "record_audit" in sheets
    assert "field_change_audit" in sheets
    assert "row_action_audit" in sheets
    assert "annotation_tasks" in sheets

    record_audit = pd.read_excel(output, sheet_name="record_audit")
    field_change_audit = pd.read_excel(output, sheet_name="field_change_audit")

    # The abnormal record is labelled by the generic anomaly-rule reason.
    assert "状态非活跃" in set(record_audit["label"])
    assert "building_status" in set(field_change_audit["field"])


def test_internal_audit_records_rows_removed_by_filter(tmp_path: Path) -> None:
    raw = tmp_path / "orders.csv"
    output = tmp_path / "filtered.xlsx"
    pd.DataFrame(
        {"order_id": ["O1", "O2"], "amount": [100, -5]}
    ).to_csv(raw, index=False)
    job = JobConfig(
        main_source=SourceConfig(path=raw),
        formulas=[
            FormulaConfig(
                output="_filter",
                op="filter_rows",
                condition=ConditionConfig(field="amount", op="gte", value=0),
            )
        ],
        export={"output_file": output, "include_internal_sheets": True},
    )

    run_job(job)

    audit = pd.read_excel(output, sheet_name="row_action_audit")
    assert len(audit) == 1
    assert audit.loc[0, "source_row_index"] == 1
    assert audit.loc[0, "action"] == "filter_rows"
    assert audit.loc[0, "reason_code"] == "ROW_REMOVED_BY_TASK_RULE"


def test_run_job_supports_multi_key_lookup_review_strategy(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    owners = tmp_path / "owners.csv"
    output = tmp_path / "multi_key_result.xlsx"

    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "owner_code": ["O01", "O02"],
        }
    ).to_csv(raw, index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S001", "S002"],
            "owner_code": ["O01", "O01", "O02"],
            "owner_name": ["Alice", "Alice Backup", "Bob"],
        }
    ).to_csv(owners, index=False)

    job = JobConfig(
        main_source=SourceConfig(path=raw),
        lookup=LookupConfig(
            source=SourceConfig(path=owners),
            left_keys=["sitecode", "owner_code"],
            right_keys=["sitecode", "owner_code"],
            fields=["owner_name"],
            duplicate_strategy="review",
            confidence_field="_lookup_confidence_owner",
            explanation_field="_lookup_explanation_owner",
            ambiguity_field="_lookup_ambiguous_owner",
        ),
        export={"output_file": output, "include_internal_sheets": True},
    )

    run_job(job)

    cleaned = pd.read_excel(output, sheet_name="cleaned_data")
    assert "owner_name" in cleaned.columns
    assert "_lookup_confidence_owner" not in cleaned.columns
    field_audit = pd.read_excel(output, sheet_name="field_change_audit")
    assert "_lookup_explanation_owner" not in set(field_audit["field"])
