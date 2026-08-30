from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

from data_agent.business_report import write_business_report
from data_agent.schemas import (
    BusinessAnswer,
    ChartRef,
    OutputFileRef,
    OutputSpec,
)
from data_agent.services.business_answer import (
    build_business_answer,
    recommended_actions_from_impact,
    records,
)
from data_agent.services.processing import (
    plan_input_paths,
    process_planned_job_with_reflection,
    require_plan_confirmation,
)
from data_agent.tools import (
    exception_summary_from_business_summary,
    export_workbook,
    extract_document_insights,
    generate_business_charts,
    lint_output,
    profile_dataset,
    read_tables_from_paths,
)
from data_agent.tools.delivery_view import (
    delivered_rows,
    missing_template_field_count,
    withheld_rows,
)
from data_agent.utils.collections import as_frame, deep_merge


@dataclass(frozen=True)
class BusinessDeliveryResult:
    answer: BusinessAnswer
    output_dir: Path
    output_files: dict[str, Path]
    reflection_attempts: list[dict[str, Any]] = dataclass_field(default_factory=list)


def answer_input_paths(
    input_paths: list[Union[str, Path]],
    goal: str,
    output_dir: Union[str, Path],
    include_audit: bool = False,
    debug: bool = False,
    recursive: bool = True,
    config: Optional[dict[str, Any]] = None,
    confirm_destructive: bool = False,
) -> BusinessDeliveryResult:
    """Plan once, apply the risk gate, execute, then publish the deliverables.

    ``confirm_destructive`` is the CLI/sync equivalent of the console's plan
    confirmation page. The setting name remains for API compatibility; it now approves
    any recorded risk escalation, including high-impact deletion and fuzzy matching.
    """

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    charts_dir = output_path / "charts"
    audit_dir = output_path / "audit_package"
    # Scratch space lives under a dotted directory so the delivery folder contains
    # products only. It used to sit next to the deliverables as ``_work`` (holding a
    # second, different final_result.xlsx) and ``_dirty_fixed_input``.
    work_dir = output_path / ".data-agent-work"

    base_processing_config = deep_merge(
        {
            "recursive": recursive,
            "result_limit": 0,
            "include_job_config": True,
            "include_diagnostics": include_audit or debug,
            "include_internal_sheets": include_audit or debug,
            "include_result_frames": True,
            # The goal must reach the recommender so capabilities (lookup / analysis
            # / annotation) are gated by what the user actually asked for. Without
            # this the delivery path would run a goal-less, capability-off plan.
            "goal": goal,
        },
        config or {},
    )
    # A caller-supplied config with an empty/absent goal must not blank out the
    # delivery goal that gates capabilities.
    if not base_processing_config.get("goal"):
        base_processing_config["goal"] = goal

    # Plan exactly once. Reflection then re-executes *this* plan with validated
    # overrides; it used to call the whole planner again per attempt, which re-ran
    # goal understanding and the LLM planner and meant the two runs being compared
    # could differ for reasons unrelated to the proposed change.
    plan = plan_input_paths(input_paths, work_dir=work_dir, config=base_processing_config)
    require_plan_confirmation(plan, confirmed=confirm_destructive)
    document_insights = plan.document_insights

    response = process_planned_job_with_reflection(plan, input_paths, work_dir=work_dir)
    reflection_attempts = response.get("reflection_attempts", [])

    result_workbook = Path(response["files"]["excel_path"])
    frames = response.get("result_frames", {})
    business_summary = as_frame(frames.get("business_summary"))
    business_result = as_frame(frames.get("business_result"))
    final_result = delivered_rows(business_result)
    # Keep business summary counts aligned with the executor's abnormal count. Rows
    # merely reported for annotation still appear in 问题说明 and in the review API,
    # but are not described as withheld from the usable result.
    needs_review = withheld_rows(business_result)
    exception_summary = as_frame(frames.get("exception_summary"))
    exception_impact = as_frame(frames.get("exception_impact"))
    template_validation = as_frame(frames.get("template_validation"))
    data_quality_summary = as_frame(frames.get("quality_summary"))
    quality_deductions = as_frame(frames.get("quality_deductions"))
    dirty_issue_summary = as_frame(frames.get("dirty_issue_summary"))
    dirty_record_issues = as_frame(frames.get("dirty_record_issues"))
    dirty_fix_audit = as_frame(frames.get("dirty_fix_audit"))
    analysis_details = frames.get("analysis_details")
    output_spec = OutputSpec.model_validate(response["output_spec"])
    # P2 goal-driven delivery: only emit the problem sheet when the goal actually
    # asked to surface exceptions/problems. The problem view is always available in
    # the report and exceptions download only when requested, so a plain "clean this
    # table" goal delivers exactly one sheet.
    wants_exception_review = output_spec.include_review_view
    recommended_actions = recommended_actions_from_impact(exception_impact)

    # The executor already wrote the OutputSpec-linted workbook (primary result,
    # 问题说明 and 可导入数据 as declared). Delivery publishes that exact file instead of
    # assembling a second, divergent one — this is what used to make the CLI and the
    # Web console hand back different Excels for the same goal.
    final_result_path = output_path / "final_result.xlsx"
    final_result_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result_workbook, final_result_path)

    chart_refs: list[ChartRef] = []
    if output_spec.charts and analysis_details is not None:
        requested_chart_ids = {chart.chart_id for chart in output_spec.charts}
        chart_refs = [
            ChartRef(
                title=chart.title,
                path=str(chart.path),
                chart_type=chart.chart_type,
                description=chart.description,
            )
            for chart in generate_business_charts(
                analysis_details,
                charts_dir=charts_dir,
                output_dir=output_path,
                requested_chart_ids=requested_chart_ids,
            )
        ]
    lint_output(
        output_spec,
        chart_ids=[Path(chart.path).stem for chart in chart_refs],
    )

    audit_refs: list[OutputFileRef] = []
    if output_spec.include_audit:
        audit_refs = _write_audit_package(
            audit_dir=audit_dir,
            input_paths=input_paths,
            response=response,
            result_workbook=result_workbook,
            internal_workbook=(
                Path(response["files"]["internal_workbook_path"])
                if response.get("files", {}).get("internal_workbook_path")
                else None
            ),
            include_audit=True,
            dirty_issue_summary=dirty_issue_summary,
            dirty_record_issues=dirty_record_issues,
            dirty_fix_audit=dirty_fix_audit,
            quality_deductions=quality_deductions,
            document_inventory=document_insights.document_inventory,
            document_field_definitions=document_insights.field_definitions,
            document_business_rules=document_insights.business_rules,
            document_import_requirements=document_insights.import_requirements,
            template_validation=template_validation,
        )
    delivery_files = [
        OutputFileRef(
            name="final_result.xlsx",
            path=str(final_result_path),
            description=(
                "业务可直接使用的处理结果和问题说明"
                if wants_exception_review
                else "业务可直接使用的处理结果"
            ),
        )
    ]
    if output_spec.report.enabled:
        if "html" in output_spec.report.formats:
            delivery_files.append(
                OutputFileRef(
                    name="business_answer.html",
                    path=str(output_path / "business_answer.html"),
                    description="业务用户阅读的简要结果报告",
                )
            )
        if "markdown" in output_spec.report.formats:
            delivery_files.append(
                OutputFileRef(
                    name="business_answer.md",
                    path=str(output_path / "business_answer.md"),
                    description="Markdown 版简要结果报告",
                )
            )
    lint_output(
        output_spec,
        artifact_names=[item.name for item in delivery_files],
    )
    task_actions = set(
        response.get("goal_plan", {}).get("task_spec", {}).get("actions", [])
    )
    wants_analysis = bool(task_actions & {"analyze", "chart"})
    wants_analysis_findings = bool(
        task_actions & {"analyze", "chart", "review"}
    )
    answer = build_business_answer(
        goal=goal,
        input_tables=response["input"]["tables"],
        total_records=int(response["counts"].get("total", 0)),
        summary=business_summary,
        final_result=final_result,
        needs_review=needs_review,
        data_quality_summary=data_quality_summary,
        exception_summary=exception_summary,
        exception_impact=exception_impact,
        dirty_issue_summary=dirty_issue_summary,
        analysis_summary=(
            as_frame(frames.get("analysis_summary")) if wants_analysis else pd.DataFrame()
        ),
        analysis_findings=(
            list(frames.get("analysis_findings") or []) if wants_analysis_findings else []
        ),
        document_rule_count=len(document_insights.generated_rules),
        missing_template_field_count=missing_template_field_count(template_validation),
        recommended_actions=recommended_actions,
        unmet_actions=list(frames.get("unmet_actions") or []),
        output_files=delivery_files,
        charts=chart_refs,
        audit_refs=audit_refs,
    )
    output_files = {
        "final_result": final_result_path,
    }
    if output_spec.report.enabled:
        report_paths = write_business_report(
            answer,
            output_path,
            formats=output_spec.report.formats,
        )
        output_files.update(
            {f"business_answer_{name}": path for name, path in report_paths.items()}
        )
    return BusinessDeliveryResult(
        answer=answer,
        output_dir=output_path,
        output_files=output_files,
        reflection_attempts=reflection_attempts,
    )


def discover_input_paths(
    input_paths: list[Union[str, Path]],
    output_dir: Union[str, Path],
    recursive: bool = True,
) -> dict[str, Path]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    tables, source_inventory = read_tables_from_paths(input_paths, recursive=recursive)
    profile_sheets = profile_dataset(tables, source_inventory=source_inventory)
    document_insights = extract_document_insights(
        input_paths,
        tables=tables,
        source_inventory=source_inventory,
        recursive=recursive,
    )
    if not document_insights.document_inventory.empty:
        profile_sheets["supporting_docs"] = document_insights.document_inventory
    if not document_insights.field_definitions.empty:
        profile_sheets["document_field_definitions"] = document_insights.field_definitions
    if not document_insights.business_rules.empty:
        profile_sheets["document_business_rules"] = document_insights.business_rules
    if not document_insights.import_requirements.empty:
        profile_sheets["document_import_requirements"] = document_insights.import_requirements
    if not document_insights.template_validation.empty:
        profile_sheets["document_template_validation"] = document_insights.template_validation

    inventory_path = export_workbook(output_path / "data_inventory.xlsx", profile_sheets)
    markdown_path = output_path / "business_discovery_report.md"
    html_path = output_path / "business_discovery_report.html"
    markdown = _render_discovery_markdown(profile_sheets)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(_render_discovery_html(markdown), encoding="utf-8")
    return {
        "business_discovery_report_md": markdown_path,
        "business_discovery_report_html": html_path,
        "data_inventory": inventory_path,
    }


def _exception_summary(summary: pd.DataFrame) -> pd.DataFrame:
    # Delegate to the shared extractor so the delivery flow and the API path feed
    # identical exception inputs into compute_quality_score (single scoring口径).
    return exception_summary_from_business_summary(summary)


def _recommended_actions(exception_summary: pd.DataFrame) -> list[str]:
    if exception_summary.empty:
        return []
    actions = []
    for column in ("建议处理", "推荐处理", "recommended_action"):
        if column not in exception_summary.columns:
            continue
        for action in exception_summary[column].dropna().astype(str).tolist():
            if action and action not in actions:
                actions.append(action)
    return actions


def _write_audit_package(
    audit_dir: Path,
    input_paths: list[Union[str, Path]],
    response: dict[str, Any],
    result_workbook: Path,
    internal_workbook: Optional[Path],
    include_audit: bool,
    dirty_issue_summary: pd.DataFrame,
    dirty_record_issues: pd.DataFrame,
    dirty_fix_audit: pd.DataFrame,
    quality_deductions: pd.DataFrame,
    document_inventory: pd.DataFrame,
    document_field_definitions: pd.DataFrame,
    document_business_rules: pd.DataFrame,
    document_import_requirements: pd.DataFrame,
    template_validation: pd.DataFrame,
) -> list[OutputFileRef]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    cleaning_log = audit_dir / "cleaning_log.json"
    lineage = audit_dir / "lineage.json"
    cleaning_log.write_text(
        json.dumps(
            {
                "input_paths": [str(path) for path in input_paths],
                "counts": response["counts"],
                "processing": response["processing"],
                "generated_files": response["files"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lineage.write_text(
        json.dumps(
            {
                "input_tables": response["input"]["tables"],
                "result_workbook": str(result_workbook),
                "job_config_path": response["files"].get("job_config_path", ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    refs = [
        OutputFileRef(name="cleaning_log.json", path=str(cleaning_log), description="本次执行日志"),
        OutputFileRef(name="lineage.json", path=str(lineage), description="输入表和输出文件追溯"),
    ]
    if not dirty_issue_summary.empty or not dirty_record_issues.empty or not dirty_fix_audit.empty:
        dirty_path = export_workbook(
            audit_dir / "dirty_data_issues.xlsx",
            {
                "issue_summary": dirty_issue_summary,
                "record_issues": dirty_record_issues,
                "fix_audit": dirty_fix_audit,
            },
        )
        refs.append(
            OutputFileRef(
                name="dirty_data_issues.xlsx",
                path=str(dirty_path),
                description="Dirty Data Engine 识别和安全修复明细",
            )
        )
    if not quality_deductions.empty:
        quality_path = export_workbook(
            audit_dir / "data_quality_deductions.xlsx",
            {"deductions": quality_deductions},
        )
        refs.append(
            OutputFileRef(
                name="data_quality_deductions.xlsx",
                path=str(quality_path),
                description="数据质量评分扣分明细",
            )
        )
    if not document_inventory.empty:
        document_path = export_workbook(
            audit_dir / "document_rules.xlsx",
            {
                "document_inventory": document_inventory,
                "field_definitions": document_field_definitions,
                "business_rules": document_business_rules,
                "import_requirements": document_import_requirements,
                "template_validation": template_validation,
            },
        )
        refs.append(
            OutputFileRef(
                name="document_rules.xlsx",
                path=str(document_path),
                description="说明文档抽取的字段定义、业务规则和导入要求",
            )
        )
    if include_audit and internal_workbook and Path(internal_workbook).exists():
        refs.extend(_export_internal_audit_sheets(Path(internal_workbook), audit_dir))
    return refs


def _export_internal_audit_sheets(internal_workbook: Path, audit_dir: Path) -> list[OutputFileRef]:
    """Split the executor's internal workbook into per-topic audit files."""

    excel = pd.ExcelFile(internal_workbook)
    sheet_to_file = {
        "record_audit": "record_audit.xlsx",
        "field_change_audit": "field_change_audit.xlsx",
        "rule_impact_summary": "rule_impact.xlsx",
        "relationship_candidates": "rule_impact.xlsx",
        "matching_diagnostics": "rule_impact.xlsx",
        "recommended_lookups": "rule_impact.xlsx",
        "recommended_formulas": "rule_impact.xlsx",
    }
    grouped: dict[str, dict[str, pd.DataFrame]] = {}
    for sheet_name, file_name in sheet_to_file.items():
        if sheet_name in excel.sheet_names:
            grouped.setdefault(file_name, {})[sheet_name] = pd.read_excel(
                internal_workbook,
                sheet_name=sheet_name,
            )

    refs = []
    for file_name, sheets in grouped.items():
        path = export_workbook(audit_dir / file_name, sheets)
        refs.append(OutputFileRef(name=file_name, path=str(path), description="内部审计材料"))
    return refs


def _render_discovery_markdown(profile_sheets: dict[str, pd.DataFrame]) -> str:
    overview = records(profile_sheets["overview"])
    tables = records(profile_sheets["table_profile"])
    relationships = records(profile_sheets["relationship_candidates"].head(10))
    issues = records(profile_sheets["profile_issues"].head(10))
    key_candidates = records(profile_sheets["key_candidates"].head(10))
    docs = records(profile_sheets.get("supporting_docs", pd.DataFrame()))
    doc_rules = records(profile_sheets.get("document_business_rules", pd.DataFrame()).head(10))
    import_requirements = records(
        profile_sheets.get("document_import_requirements", pd.DataFrame()).head(10)
    )
    template_validation = records(
        profile_sheets.get("document_template_validation", pd.DataFrame()).head(10)
    )
    base_table = _suggest_base_table(profile_sheets["table_profile"])
    return "\n".join(
        [
            "# 业务数据发现报告",
            "",
            "## 数据包总览",
            _markdown_rows(overview, ["metric", "value", "explanation"]),
            "",
            "## 识别到的业务对象",
            _markdown_rows(tables, ["table", "row_count", "column_count", "duplicate_row_count"]),
            "",
            "## 可能的主表和维表",
            f"- 推荐主表：`{base_table}`",
            "- 维表候选：行数较少且包含 key 字段的表，可用于 lookup / mapping。",
            "",
            "## 关键字段",
            _markdown_rows(key_candidates, ["table", "field", "score", "recommendation"]),
            "",
            "## 可能的跨表关系",
            _markdown_rows(
                relationships,
                ["left_table", "left_field", "right_table", "right_field", "left_match_rate"],
            ),
            "",
            "## 数据质量问题",
            _markdown_rows(issues, ["table", "field", "issue_type", "count"]),
            "",
            "## 说明文档",
            _markdown_rows(docs, ["file_name", "file_type", "parse_status"]),
            "",
            "## 文档抽取规则",
            _markdown_rows(doc_rules, ["field", "rule_type", "description"]),
            "",
            "## 导入要求",
            _markdown_rows(import_requirements, ["requirement_type", "field", "description"]),
            "",
            "## 导入模板校验",
            _markdown_rows(template_validation, ["field", "status", "requirement_type"]),
            "",
            "## 可以生成的业务结果",
            "- 标准化数据集：清洗字段、补齐 lookup 属性，输出可使用记录。",
            "- 异常复核清单：按异常类型、严重级别和建议动作组织复核任务。",
            "- 数据质量报告：说明异常率、匹配率、重复率和关键问题。",
            "",
            "## 推荐下一步处理目标",
            (
                "运行 `data-agent answer <input_path> --goal "
                f'"清洗 {base_table}，输出可用数据和异常原因" --output data/output`。'
            ),
            "",
        ]
    )


def _render_discovery_html(markdown: str) -> str:
    body = _markdown_to_html_blocks(markdown)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>业务数据发现报告</title><style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:980px;margin:32px auto;line-height:1.7;color:#1f2937;}"
        "h1,h2{color:#0f766e;} p{margin:8px 0;}"
        "table{border-collapse:collapse;width:100%;margin:12px 0;}"
        "th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;font-size:14px;}"
        "th{background:#f0fdfa;color:#0f766e;}"
        "tr:nth-child(even){background:#f8fafc;}"
        "code{background:#f1f5f9;padding:2px 6px;border-radius:6px;}"
        "</style></head><body>"
        f"{body}</body></html>"
    )


def _markdown_to_html_blocks(markdown: str) -> str:
    """Render the discovery markdown into HTML, converting pipe tables to real tables.

    Previously every non-heading line was wrapped in <p>, so markdown tables showed
    up as raw pipe text. This parses heading / table / paragraph blocks properly.
    """

    lines = markdown.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("#"):
            blocks.append(_heading(stripped))
            index += 1
            continue
        if _is_table_row(stripped):
            table_lines = []
            while index < len(lines) and _is_table_row(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            blocks.append(_render_markdown_table(table_lines))
            continue
        blocks.append(f"<p>{_escape_html(stripped)}</p>")
        index += 1
    return "\n".join(block for block in blocks if block)


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _is_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell.strip()) <= {"-", ":"} and cell.strip() for cell in cells)


def _split_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_markdown_table(table_lines: list[str]) -> str:
    if not table_lines:
        return ""
    header_cells = _split_table_cells(table_lines[0])
    body_lines = table_lines[1:]
    if body_lines and _is_table_separator(_split_table_cells(body_lines[0])):
        body_lines = body_lines[1:]

    head = "".join(f"<th>{_escape_html(cell)}</th>" for cell in header_cells)
    rows_html = []
    for row_line in body_lines:
        cells = _split_table_cells(row_line)
        rows_html.append("".join(f"<td>{_escape_html(cell)}</td>" for cell in cells))
    body = "".join(f"<tr>{row}</tr>" for row in rows_html)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _heading(line: str) -> str:
    if line.startswith("# "):
        return f"<h1>{_escape_html(line.removeprefix('# '))}</h1>"
    if line.startswith("## "):
        return f"<h2>{_escape_html(line.removeprefix('## '))}</h2>"
    return ""


def _markdown_rows(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "暂无数据。"
    output = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join(output)


def _suggest_base_table(table_profile: pd.DataFrame) -> str:
    if table_profile.empty:
        return ""
    return str(table_profile.sort_values(by="row_count", ascending=False).iloc[0]["table"])


def _fields_from_frames(frames: list[pd.DataFrame]) -> set[str]:
    fields = set()
    for frame in frames:
        fields.update(str(column) for column in frame.columns)
    return fields
