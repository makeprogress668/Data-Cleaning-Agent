from __future__ import annotations

from data_agent.schemas import BusinessAnswer


def render_business_answer_markdown(answer: BusinessAnswer) -> str:
    """Render a lean, goal-adaptive business report.

    Only sections that carry real content are emitted, so a plain cleaning job
    does not ship empty "analysis" / "charts" / "audit" headings. The result is a
    concise report that mirrors exactly what the goal asked for.
    """

    lines: list[str] = [
        "# 数据处理结果",
        "",
        "## 本次目标",
        answer.goal,
        "",
        "## 结论",
        answer.result_summary,
        "",
        "## 关键数据",
        f"- 可直接使用：{answer.usable_records_count} 条",
        f"- 需要复核：{answer.exception_records_count} 条",
        f"- 数据质量评分：**{answer.data_quality_score}/100**",
    ]

    key_findings = _bullet_list(answer.key_findings, empty_text="")
    if key_findings:
        lines += ["", "## 主要发现", key_findings]

    if answer.exception_impact:
        impact_columns = ["异常类型", "影响行数", "是否影响最终结果", "建议处理"]
        lines += [
            "",
            "## 需要关注的问题",
            _table(answer.exception_impact, impact_columns),
        ]

    # Analysis and charts only appear when the goal actually produced them.
    if answer.analysis_results:
        lines += [
            "",
            "## 统计分析",
            _table(
                answer.analysis_results[:12],
                ["分析类型", "维度", "指标", "值", "占比", "业务解读"],
            ),
        ]
    if answer.charts:
        lines += ["", "## 图表", _chart_list(answer)]

    if answer.review_tasks:
        lines += [
            "",
            "## 待人工确认",
            _table(answer.review_tasks[:20], ["源行号", "问题类型", "严重级别", "建议处理"]),
        ]

    recommended = _bullet_list(answer.recommended_actions, empty_text="")
    if recommended:
        lines += ["", "## 建议动作", recommended]

    if answer.output_files:
        lines += ["", "## 输出文件", _file_list(answer.output_files)]

    notes = answer.assumptions + answer.limitations
    if notes:
        lines += ["", "## 假设与限制", _bullet_list(notes, empty_text="")]

    lines.append("")
    return "\n".join(lines)


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "暂无数据。"

    output = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        output.append(
            "| "
            + " | ".join(_escape_cell(_display_value(row.get(column))) for column in columns)
            + " |"
        )
    return "\n".join(output)


def _bullet_list(items: list[str], empty_text: str) -> str:
    if not items:
        return empty_text
    return "\n".join(f"- {item}" for item in items)


def _chart_list(answer: BusinessAnswer) -> str:
    if not answer.charts:
        return "暂无图表。"
    return "\n".join(
        f"- [{chart.title}]({chart.path})：{chart.description}" for chart in answer.charts
    )


def _file_list(files) -> str:
    if not files:
        return "暂无文件。"
    return "\n".join(f"- `{file.name}`：{file.path}。{file.description}" for file in files)


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _display_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)
