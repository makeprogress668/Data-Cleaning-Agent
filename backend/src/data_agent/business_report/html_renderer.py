from __future__ import annotations

from html import escape

from data_agent.schemas import BusinessAnswer


def render_business_answer_html(answer: BusinessAnswer) -> str:
    metric_cards = "\n".join(
        _metric_card(str(row.get("指标", "")), str(row.get("值", "")), str(row.get("说明", "")))
        for row in answer.key_metrics
    )
    findings = _list_items(answer.key_findings, "暂无明显异常分布。")
    actions = _list_items(answer.recommended_actions, "暂无额外处理动作。")
    exception_rows = _table_rows(
        answer.exception_impact,
        ["异常类型", "影响行数", "是否影响最终结果", "建议处理"],
    )
    analysis_rows = _table_rows(
        answer.analysis_results[:12],
        ["分析类型", "维度", "指标", "值", "占比", "业务解读"],
    )
    review_rows = _table_rows(
        answer.review_tasks[:20],
        ["源行号", "问题类型", "严重级别", "建议处理", "备注"],
    )
    output_files = _file_items(answer.output_files)
    audit_files = _file_items(answer.audit_refs)
    chart_blocks = _chart_blocks(answer)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>业务数据处理结果</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1f2937; background: #f5f7fb; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px 48px; }}
    .hero {{ padding: 28px; border-radius: 22px;
      background: linear-gradient(135deg, #0f766e, #2563eb);
      color: white; box-shadow: 0 18px 45px rgba(37, 99, 235, 0.18); }}
    .hero h1 {{ margin: 0 0 12px; font-size: 30px; }}
    .hero p {{ margin: 8px 0 0; line-height: 1.7; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 14px; margin: 18px 0; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 18px; padding: 18px;
      box-shadow: 0 10px 28px rgba(15, 23, 42, 0.06); }}
    .metric strong {{ display: block; font-size: 28px; color: #0f766e; margin: 6px 0; }}
    h2 {{ margin: 28px 0 12px; font-size: 20px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 14px;
      overflow: hidden; box-shadow: 0 8px 22px rgba(15, 23, 42, 0.05); }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid #edf2f7;
      text-align: left; font-size: 14px; }}
    th {{ background: #f0fdfa; color: #115e59; }}
    ul {{ background: white; border: 1px solid #e5e7eb; border-radius: 16px; padding: 16px 22px; }}
    li {{ margin: 7px 0; line-height: 1.6; }}
    .score {{ font-size: 48px; font-weight: 800; color: #2563eb; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <h1>业务数据处理结果</h1>
    <p><strong>本次目标：</strong>{escape(answer.goal)}</p>
    <p>{escape(answer.result_summary)}</p>
  </section>

  <section class="grid">{metric_cards}</section>

  <section class="card">
    <h2>数据质量评分</h2>
    <div class="score">{answer.data_quality_score}/100</div>
    <p class="muted">评分综合考虑可用记录、异常率和严重问题数量。</p>
  </section>

  <h2>主要异常与影响</h2>
  <table><thead><tr><th>异常类型</th><th>影响行数</th><th>是否影响最终结果</th><th>建议处理</th></tr></thead>
  <tbody>{exception_rows}</tbody></table>

  <h2>统计分析结果</h2>
  <ul>{findings}</ul>
  <table><thead><tr><th>分析类型</th><th>维度</th><th>指标</th><th>值</th><th>占比</th><th>业务解读</th></tr></thead>
  <tbody>{analysis_rows}</tbody></table>

  <h2>图表解读</h2>
  {chart_blocks}

  <h2>需要业务确认的问题</h2>
  <table><thead><tr><th>源行号</th><th>问题类型</th><th>严重级别</th><th>建议处理</th><th>备注</th></tr></thead>
  <tbody>{review_rows}</tbody></table>

  <h2>推荐处理动作</h2>
  <ul>{actions}</ul>

  <h2>输出文件</h2>
  <ul>{output_files}</ul>

  <h2>审计与追溯说明</h2>
  <ul>{audit_files}</ul>
</main>
</body>
</html>
"""


def _metric_card(title: str, value: str, desc: str) -> str:
    return (
        '<div class="card metric">'
        f"<span>{escape(title)}</span><strong>{escape(value)}</strong>"
        f'<p class="muted">{escape(desc)}</p></div>'
    )


def _table_rows(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return f"<tr><td colspan='{len(columns)}'>暂无数据</td></tr>"
    result = []
    for row in rows:
        cells = "".join(f"<td>{escape(_display_value(row.get(column)))}</td>" for column in columns)
        result.append(f"<tr>{cells}</tr>")
    return "\n".join(result)


def _list_items(items: list[str], empty_text: str) -> str:
    values = items or [empty_text]
    return "\n".join(f"<li>{escape(str(item))}</li>" for item in values)


def _file_items(files) -> str:
    if not files:
        return "<li>暂无文件</li>"
    return "\n".join(
        f"<li><strong>{escape(file.name)}</strong>：{escape(file.path)}。"
        f"{escape(file.description)}</li>"
        for file in files
    )


def _chart_blocks(answer: BusinessAnswer) -> str:
    if not answer.charts:
        return '<p class="card">暂无图表。</p>'

    blocks = []
    for chart in answer.charts:
        path = escape(chart.path)
        if chart.chart_type == "html":
            visual = (
                f'<iframe title="{escape(chart.title)}" src="{path}" '
                'style="width:100%;height:420px;border:0;border-radius:14px;background:white;">'
                "</iframe>"
            )
        elif chart.chart_type in {"svg", "png"}:
            visual = f'<img src="{path}" alt="{escape(chart.title)}" style="max-width:100%;">'
        else:
            visual = f'<p><a href="{path}">{path}</a></p>'
        blocks.append(
            f'<div class="card"><h3>{escape(chart.title)}</h3>'
            f"<p>{escape(chart.description)}</p>{visual}</div>"
        )
    return "\n".join(blocks)


def _display_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)
