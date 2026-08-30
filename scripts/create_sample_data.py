from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "input"


def main() -> None:
    INPUT.mkdir(parents=True, exist_ok=True)

    orders = pd.DataFrame(
        [
            {
                "order_id": "O-1001",
                "customer_id": "C001",
                "product_id": "P001",
                "amount": 199.9,
                "status": "paid",
                "email": "buyer1@example.com",
            },
            {
                "order_id": "O-1002",
                "customer_id": "C002",
                "product_id": "P002",
                "amount": -20,
                "status": "paid",
                "email": "buyer2@example.com",
            },
            {
                "order_id": "O-1003",
                "customer_id": "C404",
                "product_id": "P003",
                "amount": 80,
                "status": "cancelled",
                "email": "bad-email",
            },
        ]
    )
    customers = pd.DataFrame(
        [
            {"customer_id": "C001", "customer_name": "Acme Ltd", "customer_level": "A"},
            {"customer_id": "C002", "customer_name": "Beta Co", "customer_level": "B"},
        ]
    )
    products = pd.DataFrame(
        [
            {"product_id": "P001", "product_name": "Analytics Suite", "category": "software"},
            {"product_id": "P002", "product_name": "Data Service", "category": "service"},
            {"product_id": "P003", "product_name": "Support Package", "category": "service"},
        ]
    )
    import_template = pd.DataFrame(
        columns=[
            "order_id",
            "customer_id",
            "customer_name",
            "product_id",
            "product_name",
            "amount",
            "status",
            "email",
        ]
    )

    tables = {
        "orders": orders,
        "customers": customers,
        "products": products,
        "import_template": import_template,
    }
    for name, table in tables.items():
        table.to_excel(INPUT / f"{name}.xlsx", index=False)
        table.to_csv(INPUT / f"{name}.csv", index=False, encoding="utf-8-sig")

    (INPUT / "cleaning_rules.md").write_text(
        """# 订单导入规则

customer_id 必填，不能为空
amount 必须 >= 0
status 允许值: paid, unpaid, refunded
email 格式为邮箱
导入字段: order_id, customer_id, customer_name, product_id, product_name, amount, status, email""",
        encoding="utf-8",
    )
    (INPUT / "order_schema.json").write_text(
        json.dumps(
            {
                "required": ["order_id", "customer_id", "amount", "status"],
                "properties": {
                    "order_id": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "amount": {"type": "number", "minimum": 0},
                    "status": {"type": "string", "enum": ["paid", "unpaid", "refunded"]},
                    "email": {"type": "string", "format": "email"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Sample input package written to: {INPUT}")


if __name__ == "__main__":
    main()
