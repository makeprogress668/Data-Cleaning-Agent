import pandas as pd

from data_agent.planning.goal_interpreter import interpret_goal


def test_interpret_goal_builds_location_evidence_and_execution_steps() -> None:
    tables = {
        "orders": pd.DataFrame(
            {
                "order_id": ["O1", "O2"],
                "customer_id": ["C001", "C002"],
                "amount": [100, 200],
                "status": ["paid", "unpaid"],
            }
        ),
        "customers": pd.DataFrame(
            {
                "customer_id": ["C001", "C002"],
                "customer_name": ["Acme", "Beta"],
                "customer_level": ["A", "B"],
            }
        ),
    }

    result = interpret_goal(
        "清洗订单数据，按 customer_id 匹配客户信息，分析 amount 并输出异常复核清单",
        tables,
    )

    assert result["suggested_base_table"] == "orders"
    assert "数据清洗" in result["focus"]
    assert "多表匹配" in result["focus"]
    assert "异常识别与复核" in result["focus"]
    assert "异常复核清单" in result["requested_outputs"]
    assert {"table": "orders", "field": "customer_id"} in result["matched_fields"]
    assert {"table": "orders", "field": "amount"} in result["matched_fields"]

    assert result["data_location"]["primary_table"] == "orders"
    assert result["data_location"]["coverage"]["matched_field_count"] >= 2
    assert result["table_matches"][0]["table"] == "orders"
    assert result["field_matches"][0]["confidence"] in {"high", "medium"}
    assert any(step["name"] == "跨表匹配" for step in result["execution_steps"])
    assert result["execution_steps"][-1]["name"] == "交付输出"
