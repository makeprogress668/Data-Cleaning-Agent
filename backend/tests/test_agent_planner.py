import json
from pathlib import Path

import pandas as pd

from data_agent.agent import plan_from_goal, write_plan_artifacts
from data_agent.agent.planner import build_llm_planner_messages


def test_plan_from_goal_builds_reviewable_business_job_config(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": ["O-1", "O-2", "O-3"],
            "customer_id": ["C001", "C002", "C404"],
            "amount": [100, 200, -5],
            "status": ["paid", "unpaid", "paid"],
        }
    ).to_excel(input_dir / "orders.xlsx", index=False)
    pd.DataFrame(
        {
            "customer_id": ["C001", "C002"],
            "customer_name": ["Acme", "Beta"],
            "customer_level": ["A", "B"],
        }
    ).to_excel(input_dir / "customers.xlsx", index=False)

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据，关联客户表补齐客户信息，输出可导入系统的数据，并列出需要人工复核的问题",
        output_file=tmp_path / "output" / "planned_result.xlsx",
        output_job_path=tmp_path / "configs" / "planned_job.json",
    )

    assert (
        result.job_config["goal"]
        == "清洗订单数据，关联客户表补齐客户信息，输出可导入系统的数据，并列出需要人工复核的问题"
    )
    assert result.job_config["output_mode"] == "business_answer"
    assert "system_import_view" in result.job_config["business_views"]
    assert result.job_config["dirty_data"]["enabled"] is True
    assert result.job_config["matching"]["duplicate_key_strategy"] == "review"
    assert result.job_config["export"]["output_file"].endswith("planned_result.xlsx")
    assert {"order_id", "customer_id", "amount", "status"}.intersection(
        set(result.job_config["quality_score"]["critical_fields"])
    )
    assert len(result.job_config["lookups"]) >= 1
    assert not result.plan_summary.empty
    assert not result.clarification_questions.empty
    assert "plan_summary" in result.to_sheets()


def test_write_plan_artifacts_exports_workbook_and_job_config(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame({"record_id": [1, 2], "customer_id": ["C001", "C002"]}).to_csv(
        input_dir / "orders.csv",
        index=False,
    )
    pd.DataFrame({"customer_id": ["C001"], "customer_name": ["Acme"]}).to_csv(
        input_dir / "customers.csv",
        index=False,
    )

    result = plan_from_goal([input_dir], goal="识别客户维表匹配关系并输出异常复核清单")
    files = write_plan_artifacts(
        result,
        report_output=tmp_path / "planning_review.xlsx",
        job_output=tmp_path / "planned_job.json",
    )

    assert files["planning_review"].exists()
    assert files["job_config"].exists()
    payload = json.loads(files["job_config"].read_text(encoding="utf-8"))
    assert payload["goal"] == "识别客户维表匹配关系并输出异常复核清单"
    assert "exception_review_view" in payload["business_views"]
    assert {"plan_summary", "clarification_questions"}.issubset(
        set(pd.ExcelFile(files["planning_review"]).sheet_names)
    )


def test_plan_from_goal_accepts_valid_llm_candidate_after_validation(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": ["O-1", "O-2"],
            "customer_id": ["C001", "C404"],
            "amount": [100, -5],
        }
    ).to_excel(input_dir / "orders.xlsx", index=False)
    pd.DataFrame({"customer_id": ["C001"], "customer_name": ["Acme"]}).to_excel(
        input_dir / "customers.xlsx",
        index=False,
    )

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据，按 customer_id 合并客户名称并输出可导入系统的数据",
        llm_candidate={
            "job_config": {
                "business_views": [
                    "standardized_dataset_view",
                    "exception_review_view",
                    "summary_analysis_view",
                    "system_import_view",
                ],
                "quality_score": {"critical_fields": ["order_id", "amount"]},
                "lookups": [
                    {
                        "name": "customers_by_customer_id",
                        "source_table": "customers",
                        "left_key": "customer_id",
                        "right_key": "customer_id",
                        "fields": ["customer_name"],
                        "match_mode": "normalized_exact",
                    }
                ],
            }
        },
    )

    assert result.llm_attempts.iloc[0]["status"] == "accepted"
    assert result.job_config["lookups"][0]["name"] == "customers_by_customer_id"
    assert result.job_config["lookups"][0]["match_field"] == (
        "_lookup_matched_customers_by_customer_id"
    )
    assert result.job_config["quality_score"]["critical_fields"] == ["order_id", "amount"]
    assert "system_import_view" in result.job_config["business_views"]
    assert "llm_planner_attempts" in result.to_sheets()


def test_semantic_planner_can_complete_lookup_before_it_is_marked_unsupported(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": ["O1", "O2"],
            "customer_id": ["C1", "C2"],
            "amount": [100, 200],
        }
    ).to_csv(input_dir / "orders.csv", index=False)
    pd.DataFrame(
        {"customer_id": ["C1", "C2"], "customer_name": ["Acme", "Beta"]}
    ).to_csv(input_dir / "customers.csv", index=False)

    def semantic_planner(messages):
        if "goal-understanding" in messages[0]["content"]:
            return json.dumps(
                {
                    "suggested_base_table": "orders",
                    "capabilities": {"needs_lookup": True},
                    "target_fields": [
                        {"table": "orders", "field": "order_id"},
                        {"table": "orders", "field": "customer_id"},
                        {"table": "orders", "field": "amount"},
                        {"table": "customers", "field": "customer_id"},
                        {"table": "customers", "field": "customer_name"},
                    ],
                }
            )
        return json.dumps(
                {
                    "job_config": {
                        "quality_score": {
                            "critical_fields": ["order_id", "customer_id", "amount"]
                        },
                        "lookups": [
                        {
                            "name": "customers_by_customer_id",
                            "source_table": "customers",
                            "left_key": "customer_id",
                            "right_key": "customer_id",
                            "fields": ["customer_name"],
                            "match_mode": "normalized_exact",
                        }
                    ]
                }
            }
        )

    result = plan_from_goal(
        [input_dir],
        goal=(
            "orders 做主表，通过 customer_id 补上 customers 里的 customer_name，"
            "并输出结果"
        ),
        llm_planner=semantic_planner,
    )

    assert "lookup_fields" in {
        step.capability_id for step in result.execution_plan.steps
    }, str(result.llm_attempts.iloc[0]["message"])
    assert result.job_config["lookups"][0]["fields"] == ["customer_name"]


def test_plan_from_goal_rejects_unsafe_llm_candidate_and_falls_back(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame({"record_id": [1], "amount": [100]}).to_csv(
        input_dir / "orders.csv",
        index=False,
    )

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据",
        llm_candidate={"job_config": {"quality_score": {"critical_fields": ["missing_field"]}}},
    )

    assert result.llm_attempts["status"].tolist() == ["rejected", "accepted"]
    assert result.llm_attempts.iloc[-1]["source"] == "deterministic_fallback"
    assert "missing_field" not in result.job_config["quality_score"]["critical_fields"]


def test_plan_from_goal_falls_back_when_llm_adds_undeclared_action(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "customers.csv"
    pd.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "customer_name": [" Acme ", "Beta"],
        }
    ).to_csv(input_file, index=False)

    def fake_llm(messages):
        if "goal-understanding" in messages[0]["content"]:
            return json.dumps({"capabilities": {}})
        return json.dumps(
            {
                "job_config": {
                    "formulas": [
                        {
                            "output": "ai_extra_label",
                            "op": "literal",
                            "value": "not requested",
                        }
                    ]
                }
            }
        )

    result = plan_from_goal(
        [input_file],
        goal="清洗 customers 客户数据并输出结果",
        llm_planner=fake_llm,
    )

    assert all(
        formula.get("output") != "ai_extra_label"
        for formula in result.job_config["formulas"]
    )
    assert "task_contract_fallback" in result.llm_attempts["source"].tolist()


def test_plan_from_goal_can_repair_llm_candidate_with_second_round(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame({"record_id": [1], "amount": [100]}).to_csv(
        input_dir / "orders.csv",
        index=False,
    )
    responses = [
        json.dumps({"job_config": {"quality_score": {"critical_fields": ["bad_field"]}}}),
        json.dumps({"job_config": {"quality_score": {"critical_fields": ["amount"]}}}),
    ]

    def fake_llm(messages):
        # The same planner is now asked twice per plan: first for goal understanding,
        # then for the planning repair loop. Answer the understanding call with a
        # neutral overlay so the scripted planning sequence is preserved.
        if "goal-understanding" in messages[0]["content"]:
            return json.dumps({"capabilities": {}})
        return responses.pop(0)

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据并评估金额质量",
        llm_planner=fake_llm,
        max_llm_rounds=2,
    )

    assert result.llm_attempts["status"].tolist() == ["rejected", "accepted"]
    assert result.job_config["quality_score"]["critical_fields"] == ["amount"]


def test_llm_planner_messages_expose_policy_controlled_column_context(tmp_path: Path) -> None:
    """The planner gets field-level signals while identifier samples stay masked."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": ["O-1", "O-2"],
            "customer_id": ["C001", "C002"],
            "amount": [100, 200],
        }
    ).to_excel(input_dir / "orders.xlsx", index=False)
    pd.DataFrame(
        {"customer_id": ["C001"], "customer_name": ["Acme"]}
    ).to_excel(input_dir / "customers.xlsx", index=False)

    captured: dict[str, list] = {}

    def capturing_planner(messages):
        # Capture only the planning prompt (skip the goal-understanding call, which
        # now also uses this planner) so the assertions target the planner context.
        if "goal-understanding" not in messages[0]["content"]:
            captured["messages"] = messages
        return json.dumps({"job_config": {}})

    plan_from_goal([input_dir], goal="按订单汇总客户金额", llm_planner=capturing_planner)

    context = json.loads(
        captured["messages"][1]["content"].split("\n", 1)[1]
    )
    available_columns = context["available_columns"]
    assert set(available_columns) == {"orders", "customers"}
    orders_fields = {column["name"] for column in available_columns["orders"]}
    assert {"order_id", "customer_id", "amount"} <= orders_fields
    # Column-level signals the LLM needs for field location are present, but the
    # default masked_samples policy does not expose the raw order identifier.
    sample_column = next(
        column for column in available_columns["orders"] if column["name"] == "order_id"
    )
    assert {"name", "dtype", "sample_values"} <= set(sample_column)
    assert "O-1" not in sample_column["sample_values"]
    assert context["data_access"]["mode"] == "masked_samples"


def test_build_llm_planner_messages_tolerates_missing_column_profile() -> None:
    """When no column profile is available the prompt still builds (empty columns)."""
    messages = build_llm_planner_messages(
        goal="清洗订单数据",
        deterministic_config={"name": "job"},
        profile_sheets={"table_profile": pd.DataFrame([{"table": "orders", "row_count": 2}])},
        recommendation_sheets={},
    )
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert context["available_columns"] == {}
    assert context["available_tables"][0]["table"] == "orders"


def test_llm_draft_that_drops_validation_rules_falls_back_instead_of_failing_the_job(
    tmp_path: Path,
) -> None:
    """A weakened draft must be rejected by the contract, not crash the pipeline.

    Rule preservation was only checked *after* the contract accepted the draft, so a
    model that quietly removed an anomaly rule produced a hard job failure
    ("候选计划删除或修改了任务所需的校验规则") instead of a silent, safe fallback.
    """

    from data_agent.agent.planner import enforce_planner_task_contract

    baseline = {
        "base_table": "orders",
        "sources": {"orders": {"path": str(tmp_path / "orders.csv")}},
        "analysis": {"enabled": False},
        "charts": {"enabled": False},
        "report": {"formats": []},
        "audit": {"enabled": False},
        "anomaly_rules": [
            {
                "name": "amount_required",
                "condition": {"field": "amount", "op": "is_blank"},
                "severity": "error",
            }
        ],
    }
    weakened = {**baseline, "anomaly_rules": []}
    goal_plan = {
        "task_spec": {
            "objective": "清洗订单并列出异常",
            "primary_entity": "orders",
            "actions": ["clean", "validate", "review", "export"],
        }
    }

    config, error = enforce_planner_task_contract(weakened, baseline, goal_plan)

    assert error, "dropping a required rule must be reported as a contract violation"
    assert [rule["name"] for rule in config["anomaly_rules"]] == ["amount_required"]


def test_llm_formula_the_executor_cannot_run_falls_back_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """一条模型写坏的公式必须在规划期被挡下，而不是执行到一半把任务炸掉。

    ``FormulaConfig`` 只校验类型，所以「ifs 但没有 conditions」——模型真的会写出来的
    形状——能通过规划，然后在执行期抛
    ``必需公式 订单类型（ifs）执行失败：ifs formula requires conditions``：
    用户拿到的是 traceback，不是工作簿。
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame({"record_id": [1], "amount": [100]}).to_csv(input_dir / "orders.csv", index=False)

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据",
        llm_candidate={
            "job_config": {
                "formulas": [
                    {"output": "订单类型", "op": "ifs", "values": ["大额"], "default": "普通"}
                ]
            }
        },
    )

    assert result.llm_attempts["status"].tolist() == ["rejected", "accepted"]
    assert result.llm_attempts.iloc[-1]["source"] == "deterministic_fallback"
    outputs = [formula["output"] for formula in result.job_config.get("formulas", [])]
    assert "订单类型" not in outputs, "无法执行的公式不该进入最终计划"
