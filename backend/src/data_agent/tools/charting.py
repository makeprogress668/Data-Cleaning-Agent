from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class GeneratedChart:
    title: str
    path: Path
    chart_type: str
    description: str


PALETTE = ["#2563eb", "#0f766e", "#f97316", "#7c3aed", "#dc2626", "#0891b2", "#65a30d"]


def generate_business_charts(
    details: dict[str, pd.DataFrame],
    charts_dir: Path,
    output_dir: Path,
    requested_chart_ids: set[str] | None = None,
) -> list[GeneratedChart]:
    """Generate no-dependency HTML/SVG charts for the business report."""

    charts_dir.mkdir(parents=True, exist_ok=True)
    charts: list[GeneratedChart] = []

    status = details.get("record_status_distribution", pd.DataFrame())
    if _requested("record_status_distribution", requested_chart_ids) and not status.empty:
        charts.append(
            _write_chart(
                charts_dir / "record_status_distribution.html",
                output_dir,
                "记录处理状态分布",
                "展示最终可用记录和需要复核记录的占比。",
                _donut_chart(
                    status,
                    label_col="处理状态",
                    value_col="记录数",
                    title="记录处理状态分布",
                ),
                status,
            )
        )

    exceptions = details.get("exception_distribution", pd.DataFrame())
    if _requested("exception_distribution", requested_chart_ids) and not exceptions.empty:
        top = exceptions.head(10)
        charts.append(
            _write_chart(
                charts_dir / "exception_distribution.html",
                output_dir,
                "异常分布图",
                "按异常类型展示影响行数，帮助定位优先处理对象。",
                _horizontal_bar_chart(
                    top,
                    label_col="异常类型",
                    value_col="影响行数",
                    title="异常分布 Top 10",
                    x_label="影响行数",
                ),
                top,
            )
        )
    missing = details.get("missing_rate", pd.DataFrame())
    if _requested("missing_rate", requested_chart_ids) and not missing.empty:
        charts.append(
            _write_chart(
                charts_dir / "missing_rate.html",
                output_dir,
                "缺失率图",
                "展示空值和空字符串最集中的字段。",
                _vertical_bar_chart(
                    missing.head(10),
                    label_col="字段",
                    value_col="缺失率",
                    title="字段缺失率 Top 10",
                    y_label="缺失率",
                    percent=True,
                ),
                missing.head(10),
            )
        )

    match = details.get("match_rate", pd.DataFrame())
    if _requested("match_rate", requested_chart_ids) and not match.empty:
        charts.append(
            _write_chart(
                charts_dir / "match_rate.html",
                output_dir,
                "匹配率图",
                "展示跨表关系候选的匹配率，辅助确认 lookup 质量。",
                _vertical_bar_chart(
                    match.head(8),
                    label_col="匹配关系",
                    value_col="匹配率",
                    title="跨表匹配率",
                    y_label="匹配率",
                    percent=True,
                ),
                match.head(8),
            )
        )

    group_distribution = details.get("group_distribution", pd.DataFrame())
    if _requested("top_business_dimension", requested_chart_ids) and not group_distribution.empty:
        first_dimension = str(group_distribution.iloc[0]["维度"])
        group_top = group_distribution[group_distribution["维度"].eq(first_dimension)].head(8)
        charts.append(
            _write_chart(
                charts_dir / "top_business_dimension.html",
                output_dir,
                "业务字段 Top N 图",
                f"按 {first_dimension} 展示记录分布。",
                _horizontal_bar_chart(
                    group_top,
                    label_col="分类",
                    value_col="记录数",
                    title=f"{first_dimension} Top N 分布",
                    x_label="记录数",
                ),
                group_top,
            )
        )

    trend = details.get("time_trend", pd.DataFrame())
    if _requested("time_trend", requested_chart_ids) and not trend.empty:
        charts.append(
            _write_chart(
                charts_dir / "time_trend.html",
                output_dir,
                "时间趋势图",
                "按月份展示记录数量变化。",
                _line_chart(
                    trend,
                    label_col="周期",
                    value_col="记录数",
                    title="时间趋势",
                    y_label="记录数",
                ),
                trend,
            )
        )

    scatter = details.get("metric_scatter", pd.DataFrame())
    if _requested("metric_scatter", requested_chart_ids) and not scatter.empty:
        x_field = str(scatter.iloc[0]["X字段"])
        y_field = str(scatter.iloc[0]["Y字段"])
        charts.append(
            _write_chart(
                charts_dir / "metric_scatter.html",
                output_dir,
                "数值关系散点图",
                f"展示 {x_field} 与 {y_field} 的数值关系。",
                _scatter_chart(
                    scatter,
                    x_col="X值",
                    y_col="Y值",
                    title=f"{x_field} 与 {y_field}",
                ),
                scatter,
            )
        )

    cross = details.get("cross_exception_distribution", pd.DataFrame())
    if _requested("exception_heatmap", requested_chart_ids) and not cross.empty:
        charts.append(
            _write_chart(
                charts_dir / "exception_heatmap.html",
                output_dir,
                "异常交叉热力图",
                "展示业务维度与异常类型的交叉分布。",
                _heatmap_chart(
                    cross.head(40),
                    x_col="问题类型",
                    y_col="分类",
                    value_col="记录数",
                    title="异常交叉分布",
                ),
                cross.head(40),
            )
        )

    return charts


def _requested(chart_id: str, requested_chart_ids: set[str] | None) -> bool:
    return requested_chart_ids is None or chart_id in requested_chart_ids


def _write_chart(
    path: Path,
    output_dir: Path,
    title: str,
    description: str,
    svg: str,
    data: pd.DataFrame,
) -> GeneratedChart:
    html = _chart_page(title, description, svg, data)
    path.write_text(html, encoding="utf-8")
    return GeneratedChart(
        title=title,
        path=path.relative_to(output_dir),
        chart_type="html",
        description=description,
    )


def _chart_page(title: str, description: str, svg: str, data: pd.DataFrame) -> str:
    table = _data_table(data)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1f2937; background: #f8fafc; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 24px; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 18px; padding: 18px;
      box-shadow: 0 10px 26px rgba(15, 23, 42, .06); }}
    p {{ color: #64748b; line-height: 1.7; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
    th {{ background: #f0fdfa; color: #115e59; }}
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1>{escape(title)}</h1>
    <p>{escape(description)}</p>
    {svg}
    {table}
  </section>
</main>
</body>
</html>
"""


def _horizontal_bar_chart(
    data: pd.DataFrame,
    label_col: str,
    value_col: str,
    title: str,
    x_label: str,
) -> str:
    rows = _chart_rows(data, label_col, value_col)
    width = 860
    bar_height = 28
    gap = 14
    top = 68
    left = 210
    right = 40
    height = top + len(rows) * (bar_height + gap) + 54
    max_value = max((row["value"] for row in rows), default=1)
    parts = [_svg_open(width, height), _title(title, width)]
    for index, row in enumerate(rows):
        y = top + index * (bar_height + gap)
        value_width = int((width - left - right) * row["value"] / max_value) if max_value else 0
        color = PALETTE[index % len(PALETTE)]
        parts.append(
            f'<text x="16" y="{y + 19}" font-size="13" fill="#334155">'
            f'{escape(row["label"])}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y}" width="{max(value_width, 2)}" height="{bar_height}" '
            f'rx="7" fill="{color}"><title>{escape(row["label"])}: {row["display"]}</title></rect>'
        )
        parts.append(
            f'<text x="{left + value_width + 8}" y="{y + 19}" font-size="13" '
            f'fill="#0f172a">{escape(row["display"])}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{height - 20}" font-size="12" fill="#64748b">{escape(x_label)}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _vertical_bar_chart(
    data: pd.DataFrame,
    label_col: str,
    value_col: str,
    title: str,
    y_label: str,
    percent: bool = False,
) -> str:
    rows = _chart_rows(data, label_col, value_col, percent=percent)
    width = 880
    height = 430
    top = 70
    bottom = 110
    left = 56
    right = 28
    chart_height = height - top - bottom
    chart_width = width - left - right
    max_value = max((row["value"] for row in rows), default=1)
    bar_width = max(18, int(chart_width / max(len(rows), 1) * 0.58))
    step = chart_width / max(len(rows), 1)
    parts = [_svg_open(width, height), _title(title, width)]
    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + chart_height}" '
        'stroke="#cbd5e1"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{top + chart_height}" x2="{width - right}" '
        f'y2="{top + chart_height}" stroke="#cbd5e1"/>'
    )
    for index, row in enumerate(rows):
        x = left + int(index * step + (step - bar_width) / 2)
        bar_h = int(chart_height * row["value"] / max_value) if max_value else 0
        y = top + chart_height - bar_h
        color = PALETTE[index % len(PALETTE)]
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{max(bar_h, 2)}" '
            f'rx="7" fill="{color}"><title>{escape(row["label"])}: {row["display"]}</title></rect>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2}" y="{y - 8}" font-size="12" text-anchor="middle" '
            f'fill="#0f172a">{escape(row["display"])}</text>'
        )
        label = _short_label(row["label"], 12)
        parts.append(
            f'<text transform="translate({x + bar_width / 2},'
            f'{top + chart_height + 18}) rotate(38)" '
            f'font-size="11" fill="#475569">{escape(label)}</text>'
        )
    parts.append(
        f'<text x="10" y="{top - 12}" font-size="12" fill="#64748b">'
        f"{escape(y_label)}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _line_chart(
    data: pd.DataFrame,
    label_col: str,
    value_col: str,
    title: str,
    y_label: str,
) -> str:
    rows = _chart_rows(data, label_col, value_col)
    width = 880
    height = 390
    top = 68
    bottom = 72
    left = 58
    right = 30
    chart_height = height - top - bottom
    chart_width = width - left - right
    max_value = max((row["value"] for row in rows), default=1)
    denominator = max(len(rows) - 1, 1)
    points = []
    for index, row in enumerate(rows):
        x = left + chart_width * index / denominator
        y = top + chart_height - (chart_height * row["value"] / max_value if max_value else 0)
        points.append((x, y, row))
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
    parts = [_svg_open(width, height), _title(title, width)]
    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + chart_height}" '
        'stroke="#cbd5e1"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{top + chart_height}" x2="{width - right}" '
        f'y2="{top + chart_height}" stroke="#cbd5e1"/>'
    )
    parts.append(f'<polyline points="{path}" fill="none" stroke="#2563eb" stroke-width="3"/>')
    for x, y, row in points:
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#2563eb">'
            f'<title>{escape(row["label"])}: {row["display"]}</title></circle>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{top + chart_height + 20}" font-size="11" '
            f'text-anchor="middle" fill="#475569">{escape(_short_label(row["label"], 10))}</text>'
        )
    parts.append(
        f'<text x="10" y="{top - 12}" font-size="12" fill="#64748b">'
        f"{escape(y_label)}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _donut_chart(
    data: pd.DataFrame,
    label_col: str,
    value_col: str,
    title: str,
) -> str:
    rows = _chart_rows(data, label_col, value_col)
    width = 720
    height = 360
    cx = 180
    cy = 190
    radius = 110
    stroke_width = 34
    total = sum(row["value"] for row in rows) or 1
    circumference = 2 * 3.1415926 * radius
    offset = 0.0
    parts = [_svg_open(width, height), _title(title, width)]
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="#e5e7eb" '
        f'stroke-width="{stroke_width}"/>'
    )
    for index, row in enumerate(rows):
        dash = circumference * row["value"] / total
        color = PALETTE[index % len(PALETTE)]
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_width}" stroke-dasharray="{dash:.2f} {circumference:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})">'
            f'<title>{escape(row["label"])}: {row["display"]}</title></circle>'
        )
        offset += dash
    parts.append(
        f'<text x="{cx}" y="{cy - 6}" text-anchor="middle" font-size="28" '
        f'font-weight="700" fill="#0f172a">{int(total)}</text>'
    )
    parts.append(
        f'<text x="{cx}" y="{cy + 22}" text-anchor="middle" font-size="13" '
        'fill="#64748b">总记录</text>'
    )
    legend_x = 390
    for index, row in enumerate(rows):
        y = 140 + index * 42
        color = PALETTE[index % len(PALETTE)]
        parts.append(
            f'<rect x="{legend_x}" y="{y}" width="16" height="16" rx="4" '
            f'fill="{color}"/>'
        )
        parts.append(
            f'<text x="{legend_x + 26}" y="{y + 13}" font-size="14" fill="#334155">'
            f'{escape(row["label"])}：{escape(row["display"])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _scatter_chart(
    data: pd.DataFrame,
    x_col: str,
    y_col: str,
    title: str,
) -> str:
    points = data[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna()
    if points.empty:
        return _empty_svg(title)
    width = 880
    height = 420
    top = 68
    bottom = 58
    left = 68
    right = 32
    chart_width = width - left - right
    chart_height = height - top - bottom
    x_min, x_max = float(points[x_col].min()), float(points[x_col].max())
    y_min, y_max = float(points[y_col].min()), float(points[y_col].max())
    x_span = x_max - x_min or 1.0
    y_span = y_max - y_min or 1.0
    parts = [_svg_open(width, height), _title(title, width)]
    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + chart_height}" '
        'stroke="#cbd5e1"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{top + chart_height}" x2="{width - right}" '
        f'y2="{top + chart_height}" stroke="#cbd5e1"/>'
    )
    for row in points.to_dict(orient="records"):
        x_value = float(row[x_col])
        y_value = float(row[y_col])
        x = left + chart_width * (x_value - x_min) / x_span
        y = top + chart_height - chart_height * (y_value - y_min) / y_span
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#2563eb" opacity="0.72">'
            f"<title>{x_value:g}, {y_value:g}</title></circle>"
        )
    parts.append(
        f'<text x="{width / 2:.0f}" y="{height - 14}" text-anchor="middle" '
        f'font-size="12" fill="#64748b">{escape(x_col)}</text>'
    )
    parts.append(
        f'<text x="14" y="{top - 12}" font-size="12" fill="#64748b">'
        f"{escape(y_col)}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _heatmap_chart(
    data: pd.DataFrame,
    x_col: str,
    y_col: str,
    value_col: str,
    title: str,
) -> str:
    if data.empty:
        return _empty_svg(title)
    pivot = data.pivot_table(
        index=y_col,
        columns=x_col,
        values=value_col,
        aggfunc="sum",
        fill_value=0,
    )
    pivot = pivot.iloc[:8, :8]
    cell = 58
    left = 160
    top = 88
    width = left + cell * max(len(pivot.columns), 1) + 36
    height = top + cell * max(len(pivot.index), 1) + 60
    max_value = float(pivot.to_numpy().max()) or 1.0
    parts = [_svg_open(width, height), _title(title, width)]
    for col_idx, column in enumerate(pivot.columns):
        x = left + col_idx * cell
        parts.append(
            f'<text x="{x + cell / 2}" y="{top - 16}" font-size="11" text-anchor="middle" '
            f'fill="#475569">{escape(_short_label(str(column), 8))}</text>'
        )
    for row_idx, (index, row) in enumerate(pivot.iterrows()):
        y = top + row_idx * cell
        parts.append(
            f'<text x="{left - 12}" y="{y + 34}" font-size="12" text-anchor="end" '
            f'fill="#475569">{escape(_short_label(str(index), 16))}</text>'
        )
        for col_idx, value in enumerate(row.tolist()):
            x = left + col_idx * cell
            opacity = 0.18 + 0.72 * float(value) / max_value
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell - 4}" height="{cell - 4}" rx="8" '
                f'fill="#2563eb" opacity="{opacity:.2f}"><title>{escape(str(index))} / '
                f'{escape(str(row.index[col_idx]))}: {int(value)}</title></rect>'
            )
            parts.append(
                f'<text x="{x + (cell - 4) / 2}" y="{y + 34}" font-size="13" '
                f'text-anchor="middle" fill="#0f172a">{int(value)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _chart_rows(
    data: pd.DataFrame,
    label_col: str,
    value_col: str,
    percent: bool = False,
) -> list[dict[str, Any]]:
    if data.empty or label_col not in data.columns or value_col not in data.columns:
        return []
    rows = []
    chart_data = data[[label_col, value_col]].dropna(subset=[label_col])
    for row in chart_data.to_dict(orient="records"):
        try:
            value = float(row[value_col])
        except (TypeError, ValueError):
            continue
        display = _display_chart_value(value, percent=percent)
        rows.append({"label": str(row[label_col]), "value": value, "display": display})
    return rows


def _data_table(data: pd.DataFrame) -> str:
    if data.empty:
        return ""
    display = data.head(12).copy()
    headers = "".join(f"<th>{escape(str(column))}</th>" for column in display.columns)
    rows = []
    for row in display.astype(object).where(pd.notna(display), "").to_dict(orient="records"):
        cells = "".join(f"<td>{escape(_display_value(value))}</td>" for value in row.values())
        rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _svg_open(width: int, height: int) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
        'xmlns="http://www.w3.org/2000/svg" role="img">'
    )


def _title(title: str, width: int) -> str:
    return (
        f'<text x="{width / 2:.0f}" y="32" text-anchor="middle" font-size="20" '
        f'font-weight="700" fill="#0f172a">{escape(title)}</text>'
    )


def _empty_svg(title: str) -> str:
    return (
        _svg_open(640, 220)
        + _title(title, 640)
        + '<text x="320" y="120" text-anchor="middle" fill="#64748b">暂无可视化数据</text></svg>'
    )


def _short_label(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[: max_len - 3] + "..."


def _display_chart_value(value: float, percent: bool) -> str:
    if percent:
        return f"{value * 100:.1f}%"
    return str(int(value) if value.is_integer() else round(value, 4))


def _display_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
    return str(value)
