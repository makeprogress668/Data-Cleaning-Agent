from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from data_agent.services import answer_input_paths, discover_input_paths

DEMO_BASE_ROOT = ROOT / "data" / "demo"


@dataclass(frozen=True)
class DemoScenario:
    name: str
    goal: str
    builder: Callable[[Path], None]


SCENARIOS = [
    DemoScenario(
        name="phase4_unstructured_rules_demo",
        goal="验证非结构化说明文档、JSON Schema 和导入模板规则能指导订单数据清洗",
        builder=lambda input_dir: create_order_demo_inputs(input_dir),
    ),
    DemoScenario(
        name="order_delivery_demo",
        goal="清洗订单数据，输出可导入系统的最终结果，并列出不能使用的数据原因",
        builder=lambda input_dir: create_order_demo_inputs(input_dir),
    ),
    DemoScenario(
        name="supplier_master_demo",
        goal="清洗供应商主数据，输出可导入 ERP 的供应商清单和异常原因",
        builder=lambda input_dir: create_supplier_demo_inputs(input_dir),
    ),
    DemoScenario(
        name="asset_register_demo",
        goal="清洗资产台账数据，输出可盘点和可导入系统的资产清单",
        builder=lambda input_dir: create_asset_demo_inputs(input_dir),
    ),
]


def main(argv: list[str] | None = None) -> None:
    scenario_names = set(argv or ["all"])
    selected = SCENARIOS if "all" in scenario_names else [
        scenario for scenario in SCENARIOS if scenario.name in scenario_names
    ]
    if not selected:
        names = ", ".join(["all", *(scenario.name for scenario in SCENARIOS)])
        raise ValueError(f"Unknown scenario. Available: {names}")

    summaries = [run_scenario(scenario, DEMO_BASE_ROOT) for scenario in selected]
    if "all" in scenario_names:
        write_phase5_acceptance_summary(summaries, DEMO_BASE_ROOT / "phase5_acceptance_suite")

    print("End-to-end demo suite completed.")
    for summary in summaries:
        print(
            f"- {summary['scenario']}: score={summary['score']}/100, "
            f"usable={summary['usable_records']}, exceptions={summary['exception_records']}"
        )
        print(f"  report: {summary['business_report']}")
    if "all" in scenario_names:
        print(f"Phase 5 acceptance summary: {DEMO_BASE_ROOT / 'phase5_acceptance_suite'}")


def run_scenario(scenario: DemoScenario, demo_base_root: Path) -> dict[str, object]:
    scenario_root = demo_base_root / scenario.name
    input_dir = scenario_root / "input"
    discovery_dir = scenario_root / "discovery"
    output_dir = scenario_root / "output"
    reset_demo_dir(scenario_root, input_dir, discovery_dir, output_dir)
    scenario.builder(input_dir)

    discover_files = discover_input_paths([input_dir], output_dir=discovery_dir)
    delivery_goal = f"{scenario.goal}，生成一张处理状态饼图和简要报告"
    result = answer_input_paths(
        [input_dir],
        goal=delivery_goal,
        output_dir=output_dir,
        include_audit=True,
        # Non-interactive acceptance harness: pre-approve high-risk steps the way a
        # user would on the confirmation page, so a plan that filters rows does not
        # stop the demo.
        confirm_destructive=True,
    )
    cleanup_office_lock_files(scenario_root)
    return {
        "scenario": scenario.name,
        "input_dir": input_dir,
        "discovery_report": discover_files["business_discovery_report_html"],
        "business_report": result.output_files["business_answer_html"],
        "final_result": result.output_files["final_result"],
        "score": result.answer.data_quality_score,
        "usable_records": result.answer.usable_records_count,
        "exception_records": result.answer.exception_records_count,
    }


def write_phase5_acceptance_summary(
    summaries: list[dict[str, object]],
    output_dir: Path,
) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for summary in summaries:
        rows.append(
            {
                "场景": summary["scenario"],
                "数据质量评分": summary["score"],
                "可用记录数": summary["usable_records"],
                "需复核记录数": summary["exception_records"],
                "业务报告": str(summary["business_report"]),
                "最终结果": str(summary["final_result"]),
            }
        )
    summary_df = pd.DataFrame(rows)
    summary_df.to_excel(output_dir / "acceptance_summary.xlsx", index=False)
    (output_dir / "acceptance_summary.md").write_text(
        _render_acceptance_summary(summary_df),
        encoding="utf-8",
    )
    cleanup_office_lock_files(output_dir)


def _render_acceptance_summary(summary_df: pd.DataFrame) -> str:
    lines = [
        "# Phase 5 验收汇总",
        "",
        "该目录用于显式汇总 Phase 5 的通用业务样例和集成验收结果。",
        "",
        "| 场景 | 数据质量评分 | 可用记录数 | 需复核记录数 |",
        "| --- | --- | --- | --- |",
    ]
    for row in summary_df.to_dict(orient="records"):
        lines.append(
            f"| {row['场景']} | {row['数据质量评分']} | "
            f"{row['可用记录数']} | {row['需复核记录数']} |"
        )
    lines.extend(
        [
            "",
            (
                "每个场景目录均包含 discovery 和 final_result.xlsx；问题说明、"
                "business answer、charts 与 audit package 仅在目标或参数明确要求时生成。"
            ),
        ]
    )
    return "\n".join(lines)


def reset_demo_dir(
    demo_root: Path,
    input_dir: Path,
    discovery_dir: Path,
    output_dir: Path,
) -> None:
    if demo_root.exists():
        shutil.rmtree(demo_root)
    input_dir.mkdir(parents=True, exist_ok=True)
    discovery_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)


def cleanup_office_lock_files(root: Path) -> None:
    for path in root.rglob("~$*"):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def create_order_demo_inputs(input_dir: Path) -> None:
    orders = pd.DataFrame(
        [
            {
                "order_id": "O-1001",
                "customer_id": "C001",
                "product_id": "P001",
                "amount": 199.9,
                "status": "paid",
                "email": "buyer1@example.com",
                "order_date": "2026-06-01",
            },
            {
                "order_id": "O-1002",
                "customer_id": "C002",
                "product_id": "P002",
                "amount": -20,
                "status": "paid",
                "email": "buyer2@example.com",
                "order_date": "2026-06-02",
            },
            {
                "order_id": "O-1003",
                "customer_id": "C404",
                "product_id": "P003",
                "amount": 80,
                "status": "unpaid",
                "email": "missing-customer@example.com",
                "order_date": "2026-06-03",
            },
            {
                "order_id": "O-1004",
                "customer_id": "",
                "product_id": "P004",
                "amount": 120,
                "status": "refunded",
                "email": "blank-customer@example.com",
                "order_date": "2026-06-04",
            },
            {
                "order_id": "O-1005",
                "customer_id": "C003",
                "product_id": "P404",
                "amount": 300,
                "status": "cancelled",
                "email": "bad-email",
                "order_date": "2026-06-05",
            },
            {
                "order_id": "O-1006",
                "customer_id": "C006",
                "product_id": "P006",
                "amount": " 260 ",
                "status": "paid",
                "email": "buyer6@example.com",
                "order_date": "not-a-date",
            },
        ]
    )
    customers = pd.DataFrame(
        [
            {"customer_id": "C001", "customer_name": "Acme Ltd", "customer_level": "A"},
            {"customer_id": "C002", "customer_name": "Beta Co", "customer_level": "B"},
            {"customer_id": "C003", "customer_name": "Core Retail", "customer_level": "A"},
            {"customer_id": "C005", "customer_name": "Delta Mall", "customer_level": "C"},
            {"customer_id": "C006", "customer_name": "Echo Shop", "customer_level": "B"},
        ]
    )
    products = pd.DataFrame(
        [
            {"product_id": "P001", "product_name": "Analytics Suite", "category": "software"},
            {"product_id": "P002", "product_name": "Data Service", "category": "service"},
            {"product_id": "P003", "product_name": "Support Package", "category": "service"},
            {"product_id": "P004", "product_name": "Training Seat", "category": "service"},
            {"product_id": "P006", "product_name": "Workflow Add-on", "category": "software"},
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
            "target_system_code",
        ]
    )

    orders.to_excel(input_dir / "orders.xlsx", index=False)
    customers.to_excel(input_dir / "customers.xlsx", index=False)
    products.to_excel(input_dir / "products.xlsx", index=False)
    import_template.to_excel(input_dir / "import_template.xlsx", index=False)
    write_order_cleaning_rules(input_dir)
    write_order_json_schema(input_dir)


def write_order_cleaning_rules(input_dir: Path) -> None:
    (input_dir / "cleaning_rules.md").write_text(
        """# 订单导入规则

| 字段 | 类型 | 必填 | 允许值 | 说明 |
| --- | --- | --- | --- | --- |
| order_id | string | 是 | | 订单唯一标识 |
| customer_id | string | 是 | | 客户主数据编码 |
| product_id | string | 是 | | 商品主数据编码 |
| amount | number | 是 | | 订单金额，必须非负 |
| status | string | 是 | paid, unpaid, refunded | 订单状态 |
| email | string | 否 | | 客户邮箱，必须符合邮箱格式 |
| order_date | date | 是 | | 下单日期 |

amount 必须 >= 0
email 格式为邮箱
导入字段: order_id, customer_id, customer_name, product_id, product_name, amount, status, email, target_system_code""",
        encoding="utf-8",
    )


def write_order_json_schema(input_dir: Path) -> None:
    schema = {
        "required": ["order_id", "customer_id", "product_id", "amount", "status"],
        "properties": {
            "order_id": {"type": "string", "description": "订单唯一标识"},
            "customer_id": {"type": "string", "description": "客户编码"},
            "product_id": {"type": "string", "description": "商品编码"},
            "amount": {"type": "number", "minimum": 0, "description": "订单金额"},
            "status": {"type": "string", "enum": ["paid", "unpaid", "refunded"]},
            "email": {"type": "string", "format": "email"},
            "order_date": {"type": "string", "format": "date"},
        },
    }
    (input_dir / "order_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def create_supplier_demo_inputs(input_dir: Path) -> None:
    suppliers = pd.DataFrame(
        [
            {
                "supplier_id": "S-001",
                "supplier_name": "North Supply",
                "country_code": "US",
                "risk_score": 20,
                "status": "active",
                "contact_email": "north@example.com",
                "onboarding_date": "2026-05-01",
            },
            {
                "supplier_id": "S-002",
                "supplier_name": "Blue Parts",
                "country_code": "CN",
                "risk_score": 120,
                "status": "active",
                "contact_email": "blue@example.com",
                "onboarding_date": "2026-05-03",
            },
            {
                "supplier_id": "S-003",
                "supplier_name": "Missing Country",
                "country_code": "XX",
                "risk_score": 60,
                "status": "inactive",
                "contact_email": "country@example.com",
                "onboarding_date": "2026-05-04",
            },
            {
                "supplier_id": "",
                "supplier_name": "No ID Vendor",
                "country_code": "DE",
                "risk_score": 50,
                "status": "blocked",
                "contact_email": "noid@example.com",
                "onboarding_date": "2026-05-05",
            },
            {
                "supplier_id": "S-005",
                "supplier_name": "Bad Email Vendor",
                "country_code": "FR",
                "risk_score": 40,
                "status": "pending",
                "contact_email": "bad-email",
                "onboarding_date": "not-a-date",
            },
        ]
    )
    countries = pd.DataFrame(
        [
            {"country_code": "US", "country_name": "United States", "region": "NA"},
            {"country_code": "CN", "country_name": "China", "region": "APAC"},
            {"country_code": "DE", "country_name": "Germany", "region": "EMEA"},
            {"country_code": "FR", "country_name": "France", "region": "EMEA"},
        ]
    )
    import_template = pd.DataFrame(
        columns=[
            "supplier_id",
            "supplier_name",
            "country_code",
            "country_name",
            "risk_score",
            "status",
            "contact_email",
            "erp_supplier_code",
        ]
    )
    suppliers.to_excel(input_dir / "suppliers.xlsx", index=False)
    countries.to_excel(input_dir / "countries.xlsx", index=False)
    import_template.to_excel(input_dir / "import_template.xlsx", index=False)
    write_supplier_rules(input_dir)
    write_supplier_schema(input_dir)


def write_supplier_rules(input_dir: Path) -> None:
    (input_dir / "supplier_rules.md").write_text(
        """# 供应商导入规则

| 字段 | 类型 | 必填 | 允许值 | 说明 |
| --- | --- | --- | --- | --- |
| supplier_id | string | 是 | | 供应商唯一编码 |
| supplier_name | string | 是 | | 供应商名称 |
| country_code | string | 是 | | 国家编码，必须能匹配国家主数据 |
| risk_score | number | 是 | | 风险评分，范围 0 到 100 |
| status | string | 是 | active, inactive, blocked | 供应商状态 |
| contact_email | string | 否 | | 供应商邮箱，必须符合邮箱格式 |
| onboarding_date | date | 是 | | 准入日期 |

risk_score 必须 >= 0
risk_score 必须 <= 100
contact_email 格式为邮箱
导入字段: supplier_id, supplier_name, country_code, country_name, risk_score, status, contact_email, erp_supplier_code""",
        encoding="utf-8",
    )


def write_supplier_schema(input_dir: Path) -> None:
    schema = {
        "required": ["supplier_id", "supplier_name", "country_code", "risk_score", "status"],
        "properties": {
            "supplier_id": {"type": "string", "description": "供应商编码"},
            "supplier_name": {"type": "string", "description": "供应商名称"},
            "country_code": {"type": "string", "description": "国家编码"},
            "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "status": {"type": "string", "enum": ["active", "inactive", "blocked"]},
            "contact_email": {"type": "string", "format": "email"},
            "onboarding_date": {"type": "string", "format": "date"},
        },
    }
    (input_dir / "supplier_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def create_asset_demo_inputs(input_dir: Path) -> None:
    assets = pd.DataFrame(
        [
            {
                "asset_id": "A-001",
                "emp_id": "E001",
                "category_code": "LAPTOP",
                "purchase_amount": 8000,
                "asset_status": "in_use",
                "purchase_date": "2026-01-02",
                "serial_number": "SN-001",
            },
            {
                "asset_id": "A-002",
                "emp_id": "E002",
                "category_code": "MONITOR",
                "purchase_amount": -300,
                "asset_status": "in_use",
                "purchase_date": "2026-01-03",
                "serial_number": "SN-002",
            },
            {
                "asset_id": "A-003",
                "emp_id": "E404",
                "category_code": "PHONE",
                "purchase_amount": 3000,
                "asset_status": "idle",
                "purchase_date": "2026-01-04",
                "serial_number": "SN-003",
            },
            {
                "asset_id": "",
                "emp_id": "E003",
                "category_code": "PRINTER",
                "purchase_amount": 2500,
                "asset_status": "retired",
                "purchase_date": "2026-01-05",
                "serial_number": "SN-004",
            },
            {
                "asset_id": "A-005",
                "emp_id": "E005",
                "category_code": "SERVER",
                "purchase_amount": 15000,
                "asset_status": "unknown",
                "purchase_date": "not-a-date",
                "serial_number": "SN-005",
            },
        ]
    )
    employees = pd.DataFrame(
        [
            {"emp_id": "E001", "employee_name": "Alice", "department": "Finance"},
            {"emp_id": "E002", "employee_name": "Bob", "department": "IT"},
            {"emp_id": "E003", "employee_name": "Cindy", "department": "Admin"},
            {"emp_id": "E005", "employee_name": "Evan", "department": "R&D"},
        ]
    )
    categories = pd.DataFrame(
        [
            {"category_code": "LAPTOP", "category_name": "Laptop", "asset_class": "IT"},
            {"category_code": "MONITOR", "category_name": "Monitor", "asset_class": "IT"},
            {"category_code": "PHONE", "category_name": "Phone", "asset_class": "IT"},
            {"category_code": "PRINTER", "category_name": "Printer", "asset_class": "Office"},
            {"category_code": "SERVER", "category_name": "Server", "asset_class": "IT"},
        ]
    )
    import_template = pd.DataFrame(
        columns=[
            "asset_id",
            "employee_name",
            "department",
            "category_name",
            "purchase_amount",
            "asset_status",
            "asset_system_code",
        ]
    )
    assets.to_excel(input_dir / "assets.xlsx", index=False)
    employees.to_excel(input_dir / "employees.xlsx", index=False)
    categories.to_excel(input_dir / "categories.xlsx", index=False)
    import_template.to_excel(input_dir / "import_template.xlsx", index=False)
    write_asset_rules(input_dir)
    write_asset_schema(input_dir)


def write_asset_rules(input_dir: Path) -> None:
    (input_dir / "asset_rules.md").write_text(
        """# 资产台账导入规则

| 字段 | 类型 | 必填 | 允许值 | 说明 |
| --- | --- | --- | --- | --- |
| asset_id | string | 是 | | 资产唯一编号 |
| emp_id | string | 是 | | 资产责任人员工号 |
| category_code | string | 是 | | 资产分类编码 |
| purchase_amount | number | 是 | | 采购金额，必须非负 |
| asset_status | string | 是 | in_use, idle, retired | 资产状态 |
| purchase_date | date | 是 | | 采购日期 |

purchase_amount 必须 >= 0
导入字段: asset_id, employee_name, department, category_name, purchase_amount, asset_status, asset_system_code""",
        encoding="utf-8",
    )


def write_asset_schema(input_dir: Path) -> None:
    schema = {
        "required": ["asset_id", "emp_id", "category_code", "purchase_amount", "asset_status"],
        "properties": {
            "asset_id": {"type": "string", "description": "资产编号"},
            "emp_id": {"type": "string", "description": "员工号"},
            "category_code": {"type": "string", "description": "资产分类编码"},
            "purchase_amount": {"type": "number", "minimum": 0},
            "asset_status": {"type": "string", "enum": ["in_use", "idle", "retired"]},
            "purchase_date": {"type": "string", "format": "date"},
        },
    }
    (input_dir / "asset_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(sys.argv[1:])
