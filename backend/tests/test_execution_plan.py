from pathlib import Path

import pytest

from data_agent.capabilities import (
    build_execution_plan,
    validate_execution_plan,
    validate_job_config_binding,
    validate_task_action_coverage,
)
from data_agent.schemas import JobConfig


def _job(**overrides) -> JobConfig:
    payload = {
        "goal": "清洗订单数据",
        "sources": {"orders": {"path": Path("orders.csv")}},
        "base_table": "orders",
        "analysis": {"enabled": False},
        "charts": {"enabled": False},
        "report": {"formats": []},
        "audit": {"enabled": False},
    }
    payload.update(overrides)
    return JobConfig.model_validate(payload)


def test_execution_plan_uses_only_registered_versioned_capabilities() -> None:
    plan = build_execution_plan(_job())

    capability_ids = [step.capability_id for step in plan.steps]
    assert plan.version == 3
    assert len(plan.config_hash) == 64
    assert capability_ids[:2] == ["extract_document_rules", "read_tables"]
    assert capability_ids[-2:] == ["export_table", "score_quality"]
    assert all(step.capability_version == "1.0.0" for step in plan.steps)
    assert all(
        not step.depends_on or step.depends_on == [plan.steps[index - 1].step_id]
        for index, step in enumerate(plan.steps)
    )
    assert validate_execution_plan(plan) is plan


def test_delivery_only_stages_are_declared_in_runtime_order() -> None:
    plan = build_execution_plan(
        _job(
            target_schema=["订单号", "客户名称"],
            formula_output=True,
            lookups=[
                {
                    "name": "customer_lookup",
                    "source_table": "customers",
                    "left_key": "客户名称",
                    "right_key": "客户名称",
                    "fields": ["客户编号"],
                }
            ],
        )
    )

    capability_ids = [step.capability_id for step in plan.steps]
    assert capability_ids.index("lookup_fields") < capability_ids.index(
        "project_schema"
    )
    assert capability_ids.index("project_schema") < capability_ids.index(
        "write_formulas"
    )
    assert capability_ids.index("write_formulas") < capability_ids.index(
        "export_table"
    )


def test_destructive_formula_requires_plan_confirmation() -> None:
    plan = build_execution_plan(
        _job(
            formulas=[
                {
                    "output": "_filtered",
                    "op": "filter_rows",
                    "condition": {"field": "status", "op": "equals", "value": "active"},
                }
            ]
        )
    )

    filter_step = next(
        step for step in plan.steps if step.capability_id == "filter_rows"
    )
    assert filter_step.risk_level == "high"
    assert filter_step.requires_confirmation is True
    assert filter_step.confirmation_reasons == [
        "missing_authorization",
        "impact_unknown",
    ]
    assert plan.requires_confirmation is True


def test_stated_destructive_formula_with_unknown_impact_requires_confirmation() -> None:
    plan = build_execution_plan(
        _job(
            formulas=[
                {
                    "output": "_filtered",
                    "op": "filter_rows",
                    "authorization": "stated",
                    "condition": {"field": "status", "op": "equals", "value": "active"},
                }
            ]
        )
    )

    step = next(item for item in plan.steps if item.capability_id == "filter_rows")
    assert step.confirmation_reasons == ["impact_unknown"]


def test_fuzzy_lookup_requires_confirmation_even_without_row_deletion() -> None:
    plan = build_execution_plan(
        _job(
            lookups=[
                {
                    "name": "customer_lookup",
                    "source_table": "customers",
                    "left_key": "customer_name",
                    "right_key": "customer_name",
                    "fields": ["customer_id"],
                    "match_mode": "fuzzy",
                }
            ]
        )
    )

    step = next(item for item in plan.steps if item.capability_id == "lookup_fields")
    assert step.confirmation_reasons == ["fuzzy_matching"]
    assert plan.requires_confirmation


def test_execution_plan_rejects_unknown_capability_and_policy_downgrade() -> None:
    payload = build_execution_plan(_job()).model_dump(mode="json")
    payload["steps"][0]["capability_id"] = "python_eval"
    with pytest.raises(ValueError, match="unregistered capability"):
        validate_execution_plan(payload)

    payload = build_execution_plan(
        _job(
            formulas=[
                {
                    "output": "_filtered",
                    "op": "filter_rows",
                    "condition": {"field": "status", "op": "equals", "value": "active"},
                }
            ]
        )
    ).model_dump(mode="json")
    filter_step = next(
        step for step in payload["steps"] if step["capability_id"] == "filter_rows"
    )
    filter_step["risk_level"] = "low"
    filter_step["requires_confirmation"] = False
    with pytest.raises(ValueError, match="risk metadata"):
        validate_execution_plan(payload)


def test_execution_plan_rejects_parameter_and_acceptance_contract_tampering() -> None:
    payload = build_execution_plan(_job()).model_dump(mode="json")
    read_step = next(
        step for step in payload["steps"] if step["capability_id"] == "read_tables"
    )
    read_step["parameters"]["python_callable"] = "arbitrary"
    with pytest.raises(ValueError, match="undeclared parameters"):
        validate_execution_plan(payload)

    payload = build_execution_plan(_job()).model_dump(mode="json")
    payload["steps"][0]["acceptance_rules"] = ["always pass"]
    with pytest.raises(ValueError, match="acceptance rules"):
        validate_execution_plan(payload)


def test_execution_plan_seals_the_runtime_job_config() -> None:
    job = _job()
    plan = build_execution_plan(job)
    changed = job.model_copy(deep=True)
    changed.exception_policy.exclude_critical_from_final = False

    with pytest.raises(ValueError, match="JobConfig"):
        validate_job_config_binding(plan, changed)


def test_task_action_coverage_rejects_silently_omitted_filter() -> None:
    plan = build_execution_plan(_job())

    with pytest.raises(ValueError, match="filter"):
        validate_task_action_coverage(
            {
                "objective": "过滤负金额订单",
                "actions": ["filter", "export"],
            },
            plan,
        )


def test_task_action_coverage_rejects_undeclared_derived_column() -> None:
    plan = build_execution_plan(
        _job(
            formulas=[
                {
                    "output": "ai_extra_label",
                    "op": "literal",
                    "value": "not requested",
                }
            ]
        )
    )

    with pytest.raises(ValueError, match="计划包含用户未要求的动作"):
        validate_task_action_coverage(
            {
                "objective": "清洗订单并输出原字段",
                "actions": ["clean", "export"],
            },
            plan,
        )
