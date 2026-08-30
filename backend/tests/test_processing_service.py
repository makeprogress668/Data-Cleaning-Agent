from pathlib import Path

import pandas as pd

from data_agent.services import process_input_paths


def test_process_input_paths_returns_simplified_business_result(tmp_path: Path) -> None:
    raw = tmp_path / "raw_sitecode.csv"
    buildings = tmp_path / "buildings.csv"
    owners = tmp_path / "owners.csv"
    work_dir = tmp_path / "api_job"

    pd.DataFrame(
        {
            "record_id": [1, 2, 3],
            "sitecode": ["S001", "S002", "S404"],
            "owner_code": ["O001", "O002", "O999"],
            "amount": [100, 200, 300],
        }
    ).to_csv(raw, index=False)
    pd.DataFrame(
        {
            "sitecode": ["S001", "S002"],
            "building_id": ["B001", "B002"],
            "building_status": ["active", "inactive"],
            "city": ["北京", "上海"],
        }
    ).to_csv(buildings, index=False)
    pd.DataFrame(
        {
            "owner_code": ["O001", "O002"],
            "owner_name": ["张三", "李四"],
            "owner_dept": ["销售", "运营"],
        }
    ).to_csv(owners, index=False)

    response = process_input_paths(
        [raw, buildings, owners],
        work_dir=work_dir,
        config={
            "result_limit": 2,
            "goal": "以 raw_sitecode 为主表，匹配关联补齐 building_status 和 owner_name",
        },
    )

    assert response["status"] == "success"
    assert response["input"]["table_count"] == 3
    # The goal asks for two columns to be filled in. S404/O999 match nothing, but
    # nobody asked for those records to be held back, so all three are delivered
    # with the lookup columns left blank. Withholding them is a decision about the
    # user's data that the goal never authorised.
    assert response["counts"] == {"total": 3, "valid": 3, "abnormal": 0}
    assert response["result_row_count"] == 3
    # result_limit=2 caps the inline preview only; the workbook still holds all three.
    assert response["result_truncated"] is True
    assert len(response["result_rows"]) == 2
    assert response["files"]["excel_path"].endswith("final_result.xlsx")
    assert Path(response["files"]["excel_path"]).exists()

    summary_items = {row["项目"] for row in response["summary"]}
    assert "需处理行数" in summary_items
    # The goal says nothing about problems, so the summary reports what was done and
    # stops there. Volunteering a findings list about data nobody asked to inspect is
    # the same overreach as editing it uninvited.
    assert "关联数据未匹配" not in summary_items

    first_row = response["result_rows"][0]
    assert {
        "处理状态",
        "问题类型",
        "建议处理",
        "源行号",
        "影响字段",
        "当前值",
    }.isdisjoint(first_row)
    assert "_source_table" not in first_row


def test_process_input_paths_supports_config_overrides(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    work_dir = tmp_path / "api_job"

    pd.DataFrame({"record_id": [1, 2], "amount": [None, 10]}).to_csv(raw, index=False)

    response = process_input_paths(
        [raw],
        work_dir=work_dir,
        config={
            "anomaly_rules": [
                {
                    "name": "amount_missing",
                    "condition": {"field": "amount", "op": "is_blank"},
                    "severity": "error",
                    "reason": "amount_missing",
                }
            ],
            "include_job_config": True,
            "result_limit": 0,
        },
    )

    assert response["counts"] == {"total": 2, "valid": 1, "abnormal": 1}
    assert response["result_truncated"] is False
    assert len(response["result_rows"]) == 1
    assert "active_building" not in response["job_config"]
    assert response["job_config"]["anomaly_rules"][0]["name"] == "amount_missing"


def test_explicit_filter_and_deduplication_change_the_delivered_rows(
    tmp_path: Path,
) -> None:
    orders = tmp_path / "orders.csv"
    pd.DataFrame(
        {
            "order_id": ["O1", "O1", "O2"],
            "amount": [100, 120, -5],
        }
    ).to_csv(orders, index=False)

    response = process_input_paths(
        [orders],
        work_dir=tmp_path / "api_job",
        config={
            "goal": "按 order_id 去重，保留第一条，并过滤 amount < 0 的记录",
            "include_job_config": True,
            "result_limit": 10,
        },
    )

    assert response["status"] == "success"
    assert response["counts"] == {"total": 3, "valid": 1, "abnormal": 0}
    assert [row["order_id"] for row in response["result_rows"]] == ["O1"]
    assert response["result_rows"][0]["amount"] == 100
    assert [
        step["capability_id"] for step in response["execution_plan"]["steps"]
    ].count("filter_rows") == 1
    assert [
        step["capability_id"] for step in response["execution_plan"]["steps"]
    ].count("deduplicate") == 1
    assert not any(
        column.startswith(("_filter_rows_", "_deduplicate_rows_"))
        for column in response["result_rows"][0]
    )


def test_plain_clean_goal_applies_safe_text_normalization(tmp_path: Path) -> None:
    customers = tmp_path / "customers.csv"
    pd.DataFrame(
        {
            "customer_id": ["C1", "C2"],
            "customer_name": ["  Acme\u200b  ", "Globex"],
        }
    ).to_csv(customers, index=False)

    response = process_input_paths(
        [customers],
        work_dir=tmp_path / "api_job",
        config={
            "goal": "清洗客户数据，去除文本前后空格和不可见字符",
            "result_limit": 10,
        },
    )

    assert [row["customer_name"] for row in response["result_rows"]] == [
        "Acme",
        "Globex",
    ]
    assert any(
        step["capability_id"] == "detect_anomalies"
        for step in response["execution_plan"]["steps"]
    )


def test_process_input_paths_returns_goal_location_plan(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    work_dir = tmp_path / "api_job"

    pd.DataFrame(
        {
            "order_id": ["O1", "O2"],
            "customer_id": ["C001", "C404"],
            "amount": [100, 200],
        }
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {
            "customer_id": ["C001"],
            "customer_name": ["Acme"],
        }
    ).to_csv(customers, index=False)

    response = process_input_paths(
        [orders, customers],
        work_dir=work_dir,
        config={
            "goal": "清洗订单数据，按 customer_id 合并客户信息，并输出异常复核清单",
            "result_limit": 10,
        },
    )

    goal_plan = response["goal_plan"]
    assert goal_plan["suggested_base_table"] == "orders"
    assert "多表匹配" in goal_plan["focus"]
    assert {"table": "orders", "field": "customer_id"} in goal_plan["matched_fields"]
    assert goal_plan["data_location"]["primary_table"] == "orders"
    assert goal_plan["data_location"]["coverage"]["matched_field_count"] >= 1
    assert goal_plan["table_matches"]
    assert goal_plan["field_matches"]
    assert any(step["name"] == "跨表匹配" for step in goal_plan["execution_steps"])


def test_process_input_paths_exposes_discovery_and_plan_views(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    customers = tmp_path / "customers.csv"
    rules = tmp_path / "rules.md"
    work_dir = tmp_path / "api_job"

    pd.DataFrame(
        {
            "order_id": ["O1", "O2", "O3"],
            "customer_id": ["C001", "", "C003"],
            "amount": [100, -5, 200],
            "status": ["paid", "cancelled", "paid"],
        }
    ).to_csv(orders, index=False)
    pd.DataFrame(
        {
            "customer_id": ["C001", "C003"],
            "customer_name": ["Acme", "Globex"],
        }
    ).to_csv(customers, index=False)
    rules.write_text("amount 必须 >= 0\nstatus 允许值: paid, unpaid\n", encoding="utf-8")

    response = process_input_paths(
        [orders, customers, rules],
        work_dir=work_dir,
        config={
            "goal": "清洗订单数据，合并客户信息，列出异常",
            "include_diagnostics": True,
        },
    )

    discovery = response["discovery"]
    assert "orders.csv" in discovery["files"]
    assert any(table["name"] == "orders" for table in discovery["tables"])
    assert any(key["field"] == "customer_id" for key in discovery["keys"])
    assert discovery["issues"]  # empty customer_id should be surfaced

    plan = response["plan"]
    assert plan["main_table"]
    assert plan["anomaly_rules"]
    # The plan must never leak internal rule labels to the console.
    joined = " ".join(plan["anomaly_rules"])
    assert "doc_rule_" not in joined
    assert "_not_found" not in joined
