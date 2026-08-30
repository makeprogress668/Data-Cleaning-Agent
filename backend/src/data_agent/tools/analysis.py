from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from data_agent.utils.collections import unique
from data_agent.utils.field_names import looks_like_date_field, looks_like_key
from data_agent.utils.numeric import to_business_numeric


@dataclass(frozen=True)
class AnalysisResult:
    summary: pd.DataFrame
    details: dict[str, pd.DataFrame]
    findings: list[str]


SUMMARY_COLUMNS = ["分析类型", "维度", "指标", "值", "占比", "业务解读"]
LOW_VALUE_COLUMNS = {"备注", "建议处理", "确认状态", "是否需要人工确认", "是否可用于最终结果"}


def analyze_business_data(
    business_result: pd.DataFrame,
    final_result: pd.DataFrame,
    needs_review: pd.DataFrame,
    data_quality_summary: pd.DataFrame,
    exception_impact: pd.DataFrame,
    dirty_issue_summary: pd.DataFrame,
    relationship_candidates: pd.DataFrame | None = None,
    goal: str = "",
) -> AnalysisResult:
    """Build deterministic business analysis tables and natural-language findings."""

    summary_rows: list[dict[str, Any]] = []
    findings: list[str] = []
    details: dict[str, pd.DataFrame] = {}

    total = _record_count(business_result)
    usable = _record_count(final_result)
    raw_review = _record_count(needs_review)
    if total == 0:
        total = usable + raw_review
    review = min(raw_review, max(total - usable, 0)) if total else raw_review
    review_task_count = int(len(needs_review))
    exception_rate = _safe_rate(review, total)
    quality_score = _metric_value(data_quality_summary, "数据质量评分")

    status_distribution = _status_distribution(usable, review)
    details["record_status_distribution"] = status_distribution
    summary_rows.append(
        _summary_row(
            "总量统计",
            "全量数据",
            "记录处理结果",
            total,
            1,
            f"本次共处理 {total} 行，{usable} 行可直接使用，{review} 行需要复核。",
        )
    )
    summary_rows.append(
        _summary_row(
            "异常率分析",
            "全量数据",
            "需复核占比",
            review,
            exception_rate,
            (
                f"需复核记录占比为 {_format_percent(exception_rate)}，"
                f"数据质量评分为 {quality_score}/100。"
            ),
        )
    )
    if review_task_count > review:
        summary_rows.append(
            _summary_row(
                "复核任务统计",
                "异常复核清单",
                "任务明细数",
                review_task_count,
                "",
                (
                    f"复核清单包含 {review_task_count} 条任务明细，"
                    f"去重后影响 {review} 条源记录。"
                ),
            )
        )
    findings.append(
            f"本次数据可用率为 {_format_percent(_safe_rate(usable, total))}，"
        f"需复核占比为 {_format_percent(exception_rate)}。"
        )

    exception_distribution = _exception_distribution(exception_impact)
    details["exception_distribution"] = exception_distribution
    if not exception_distribution.empty:
        top_exception = exception_distribution.iloc[0]
        top_rate = _safe_rate(int(top_exception["影响行数"]), max(review, 1))
        summary_rows.append(
            _summary_row(
                "按异常类型聚合",
                str(top_exception["异常类型"]),
                "Top 异常影响行数",
                int(top_exception["影响行数"]),
                top_rate,
                (
                    f"{top_exception['异常类型']} 是当前影响最大的异常，"
                    f"占需复核记录 {_format_percent(top_rate)}。"
                ),
            )
        )
        top_exception_count = int(top_exception["影响行数"])
        findings.append(
            f"最大异常类型是 {top_exception['异常类型']}，影响 {top_exception_count} 行。"
        )

    missing_rate = _missing_rate(dirty_issue_summary, total)
    details["missing_rate"] = missing_rate
    if not missing_rate.empty:
        top_missing = missing_rate.iloc[0]
        summary_rows.append(
            _summary_row(
                "缺失率分析",
                str(top_missing["字段"]),
                "缺失或空字符串行数",
                int(top_missing["影响行数"]),
                float(top_missing["缺失率"]),
                (
                    f"{top_missing['字段']} 缺失最明显，"
                    f"缺失率 {_format_percent(top_missing['缺失率'])}。"
                ),
            )
        )
        findings.append(
            f"{top_missing['字段']} 是缺失最集中的字段，"
            f"缺失率 {_format_percent(top_missing['缺失率'])}。"
        )

    match_rate = _match_rate(relationship_candidates)
    details["match_rate"] = match_rate
    if not match_rate.empty:
        weakest = match_rate.sort_values(by="匹配率", ascending=True).iloc[0]
        summary_rows.append(
            _summary_row(
                "匹配率分析",
                str(weakest["匹配关系"]),
                "最低匹配率",
                _format_percent(weakest["匹配率"]),
                float(weakest["匹配率"]),
                (
                    f"{weakest['匹配关系']} 的匹配率为 "
                    f"{_format_percent(weakest['匹配率'])}，建议优先检查未匹配 key。"
                ),
            )
        )
        findings.append(
            f"{weakest['匹配关系']} 是当前匹配率最低的候选关系，"
            f"匹配率 {_format_percent(weakest['匹配率'])}。"
        )

    group_distribution = _group_distribution(business_result)
    details["group_distribution"] = group_distribution
    if not group_distribution.empty:
        strongest = group_distribution.iloc[0]
        summary_rows.append(
            _summary_row(
                "按业务字段聚合",
                str(strongest["维度"]),
                "Top 分类",
                f"{strongest['分类']}：{int(strongest['记录数'])} 行",
                float(strongest["占比"]),
                (
                    f"{strongest['维度']} 中 {strongest['分类']} 占比最高，"
                    f"为 {_format_percent(strongest['占比'])}。"
                ),
            )
        )
        findings.append(
            f"按 {strongest['维度']} 看，{strongest['分类']} 占比最高，"
            f"为 {_format_percent(strongest['占比'])}。"
        )

    numeric_summary = _numeric_summary(business_result)
    details["numeric_summary"] = numeric_summary
    if not numeric_summary.empty:
        numeric = numeric_summary.iloc[0]
        summary_rows.append(
            _summary_row(
                "分布分析",
                str(numeric["字段"]),
                "数值分布",
                f"均值 {numeric['均值']}，合计 {numeric['合计']}",
                "",
                f"{numeric['字段']} 的合计为 {numeric['合计']}，均值为 {numeric['均值']}。",
            )
        )

    metric_scatter = _numeric_scatter(business_result)
    details["metric_scatter"] = metric_scatter

    time_trend = _time_trend(business_result)
    details["time_trend"] = time_trend
    if not time_trend.empty:
        latest = time_trend.iloc[-1]
        summary_rows.append(
            _summary_row(
                "时间趋势",
                str(latest["字段"]),
                "最近周期记录数",
                int(latest["记录数"]),
                "",
                f"{latest['字段']} 最近周期 {latest['周期']} 有 {int(latest['记录数'])} 行记录。",
            )
        )

    cross_analysis = _cross_exception_distribution(business_result)
    details["cross_exception_distribution"] = cross_analysis
    if not cross_analysis.empty:
        top_cross = cross_analysis.iloc[0]
        findings.append(
            f"{top_cross['维度']}={top_cross['分类']} 下最集中的异常是 "
            f"{top_cross['问题类型']}，影响 {int(top_cross['记录数'])} 行。"
        )

    if goal:
        summary_rows.append(
            _summary_row(
                "目标解释",
                "用户目标",
                "业务目标覆盖",
                "已完成",
                "",
                f"围绕“{goal}”生成可用数据、异常复核、质量评分、统计分析和图表。",
            )
        )

    summary = pd.DataFrame(summary_rows, columns=SUMMARY_COLUMNS)
    return AnalysisResult(summary=summary, details=details, findings=_unique(findings))


def _status_distribution(usable: int, review: int) -> pd.DataFrame:
    total = usable + review
    return pd.DataFrame(
        [
            {"处理状态": "可直接使用", "记录数": usable, "占比": _safe_rate(usable, total)},
            {"处理状态": "需要复核", "记录数": review, "占比": _safe_rate(review, total)},
        ]
    )


def _exception_distribution(exception_impact: pd.DataFrame) -> pd.DataFrame:
    columns = ["异常类型", "影响行数", "占比", "严重级别", "建议处理"]
    if exception_impact.empty or "异常类型" not in exception_impact.columns:
        return pd.DataFrame(columns=columns)

    grouped = (
        exception_impact.groupby(
            ["异常类型", "严重级别", "建议处理"],
            dropna=False,
            as_index=False,
        )["影响行数"]
        .sum()
        .sort_values(by=["影响行数", "异常类型"], ascending=[False, True], ignore_index=True)
    )
    # 过滤影响 0 行的噪音项，避免交付分布表出现"影响 0 行"的无效条目。
    grouped = grouped[grouped["影响行数"].astype(int) > 0].reset_index(drop=True)
    if grouped.empty:
        return pd.DataFrame(columns=columns)
    total = int(grouped["影响行数"].sum())
    grouped["占比"] = grouped["影响行数"].map(lambda value: _safe_rate(int(value), total))
    return grouped[columns]


def _missing_rate(dirty_issue_summary: pd.DataFrame, total: int) -> pd.DataFrame:
    columns = ["字段", "问题类型", "影响行数", "缺失率", "建议处理"]
    if dirty_issue_summary.empty or "issue_type" not in dirty_issue_summary.columns:
        return pd.DataFrame(columns=columns)
    mask = dirty_issue_summary["issue_type"].isin(["null_values", "empty_strings"])
    missing = dirty_issue_summary[mask].copy()
    if missing.empty:
        return pd.DataFrame(columns=columns)
    missing = missing[missing["affected_rows"].astype(int) > 0]
    if missing.empty:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(
        {
            "字段": missing.get("field", ""),
            "问题类型": missing["issue_type"].map(
                {"null_values": "空值", "empty_strings": "空字符串"}
            ),
            "影响行数": missing["affected_rows"].astype(int),
            "缺失率": missing["affected_rows"].map(lambda value: _safe_rate(int(value), total)),
            "建议处理": missing.get("recommended_action", ""),
        }
    )
    return result.sort_values(by=["影响行数", "字段"], ascending=[False, True], ignore_index=True)


def _match_rate(relationship_candidates: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["匹配关系", "匹配率", "未匹配数", "右表重复 key 数", "是否有 Join 膨胀风险", "建议"]
    if relationship_candidates is None or relationship_candidates.empty:
        return pd.DataFrame(columns=columns)
    required = {"left_table", "left_field", "right_table", "right_field", "left_match_rate"}
    if not required.issubset(set(relationship_candidates.columns)):
        return pd.DataFrame(columns=columns)

    result = pd.DataFrame(
        {
            "匹配关系": relationship_candidates.apply(
                lambda row: (
                    f"{row['left_table']}.{row['left_field']} -> "
                    f"{row['right_table']}.{row['right_field']}"
                ),
                axis=1,
            ),
            "匹配率": relationship_candidates["left_match_rate"].astype(float),
            "未匹配数": relationship_candidates.get("unmatched_left_count", 0),
            "右表重复 key 数": relationship_candidates.get("right_duplicate_key_count", 0),
            "是否有 Join 膨胀风险": relationship_candidates.get("join_will_expand", False),
            "建议": relationship_candidates.get("recommendation", ""),
        }
    )
    return result.sort_values(by=["匹配率", "未匹配数"], ascending=[True, False], ignore_index=True)


def _group_distribution(df: pd.DataFrame, max_dimensions: int = 3, top_n: int = 5) -> pd.DataFrame:
    columns = ["维度", "分类", "记录数", "占比"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for column in _categorical_columns(df)[:max_dimensions]:
        series = df[column].fillna("未填写").astype(str).str.strip().replace("", "未填写")
        counts = series.value_counts(dropna=False).head(top_n)
        denominator = int(len(series))
        for category, count in counts.items():
            rows.append(
                {
                    "维度": column,
                    "分类": category,
                    "记录数": int(count),
                    "占比": _safe_rate(int(count), denominator),
                }
            )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        by=["记录数", "维度"], ascending=[False, True], ignore_index=True
    )


def _numeric_summary(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["字段", "非空数", "合计", "均值", "最小值", "最大值", "P90"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for column in df.columns:
        if _looks_like_identifier(column):
            continue
        numeric = _to_numeric_series(df[column]).dropna()
        if len(numeric) < 2:
            continue
        rows.append(
            {
                "字段": column,
                "非空数": int(len(numeric)),
                "合计": round(float(numeric.sum()), 4),
                "均值": round(float(numeric.mean()), 4),
                "最小值": round(float(numeric.min()), 4),
                "最大值": round(float(numeric.max()), 4),
                "P90": round(float(numeric.quantile(0.9)), 4),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def _numeric_scatter(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["X字段", "Y字段", "X值", "Y值"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    numeric_fields: list[tuple[str, pd.Series]] = []
    for column in df.columns:
        if _looks_like_identifier(column):
            continue
        numeric = _to_numeric_series(df[column])
        if numeric.notna().sum() >= 2:
            numeric_fields.append((str(column), numeric))
        if len(numeric_fields) == 2:
            break
    if len(numeric_fields) < 2:
        return pd.DataFrame(columns=columns)
    (x_field, x_values), (y_field, y_values) = numeric_fields
    points = pd.DataFrame({"X值": x_values, "Y值": y_values}).dropna().head(200)
    if points.empty:
        return pd.DataFrame(columns=columns)
    points.insert(0, "Y字段", y_field)
    points.insert(0, "X字段", x_field)
    return points[columns].reset_index(drop=True)


def _time_trend(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["字段", "周期", "记录数"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    for column in df.columns:
        if _looks_like_identifier(column):
            continue
        if not _looks_like_date_column(column, df[column]):
            continue
        parsed = pd.to_datetime(df[column], errors="coerce", format="mixed")
        if parsed.notna().sum() < max(3, len(df) * 0.5):
            continue
        periods = parsed.dropna().dt.to_period("M").astype(str)
        counts = periods.value_counts().sort_index()
        return pd.DataFrame(
            [
                {"字段": column, "周期": period, "记录数": int(count)}
                for period, count in counts.items()
            ],
            columns=columns,
        )
    return pd.DataFrame(columns=columns)


def _cross_exception_distribution(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["维度", "分类", "问题类型", "记录数"]
    if df.empty or "问题类型" not in df.columns:
        return pd.DataFrame(columns=columns)
    dimensions = [column for column in _categorical_columns(df) if column != "问题类型"]
    if not dimensions:
        return pd.DataFrame(columns=columns)
    dimension = dimensions[0]
    work = df[[dimension, "问题类型"]].copy()
    # One row can carry several issues joined by ；. Grouping on the joined string made
    # every distinct combination its own "category", so the headline read "最集中的异常是
    # 备注 为空；duplicate_订单号；duplicate_客户编号；重复行；空值；重复 key；数字存成文本；
    # 数字存成文本" — unreadable, duplicated, and counting nothing meaningful.
    work["问题类型"] = (
        work["问题类型"]
        .fillna("")
        .astype(str)
        .map(lambda text: unique(part.strip() for part in re.split(r"[;；]", text) if part.strip()))
    )
    work = work.explode("问题类型")
    work["问题类型"] = work["问题类型"].fillna("").astype(str).str.strip()
    work = work[work["问题类型"].ne("")]
    if work.empty:
        return pd.DataFrame(columns=columns)
    work[dimension] = (
        work[dimension].fillna("未填写").astype(str).str.strip().replace("", "未填写")
    )
    result = (
        work.groupby([dimension, "问题类型"], dropna=False)
        .size()
        .reset_index(name="记录数")
        .rename(columns={dimension: "分类"})
        .sort_values(by=["记录数", "分类"], ascending=[False, True], ignore_index=True)
        .head(20)
    )
    result.insert(0, "维度", dimension)
    return result[columns]


def _categorical_columns(df: pd.DataFrame) -> list[str]:
    # 仅预置流水线自身产出的通用维度列，其余业务维度由下方通用检测自动纳入。
    preferred = ["处理状态", "问题类型", "严重级别"]
    result = [
        column for column in preferred if column in df.columns and _is_categorical(df[column])
    ]
    for column in df.columns:
        if column in result or column in LOW_VALUE_COLUMNS or _looks_like_identifier(column):
            continue
        if _is_categorical(df[column]):
            result.append(column)
    return result


def _is_categorical(series: pd.Series) -> bool:
    if series.empty:
        return False
    unique_count = int(series.nunique(dropna=True))
    if unique_count <= 1:
        return False
    return unique_count <= min(20, max(2, int(len(series) * 0.7)))


def _looks_like_date_column(column: object, series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return looks_like_date_field(column)


def _to_numeric_series(series: pd.Series) -> pd.Series:
    """Analysis and aggregation must agree on what a number is.

    This used to strip its own short list of symbols while build_pivot stripped none,
    so the same 金额 column could be analysed correctly and summed as zero. Both now
    read through one parser.
    """

    return to_business_numeric(series)


def _looks_like_identifier(column: object) -> bool:
    # 源行号 is the pipeline's own trace column, not a business identifier, but it must
    # not become an analysis dimension either.
    name = str(column)
    return name.startswith("_") or looks_like_key(column) or "源行号" in name


def _record_count(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    for column in ("源行号", "source_row_index", "_source_row_index"):
        if column in df.columns:
            values = df[column].dropna()
            if not values.empty:
                return int(values.nunique())
    return int(len(df))


def _summary_row(
    analysis_type: str,
    dimension: str,
    metric: str,
    value: object,
    ratio: object,
    interpretation: str,
) -> dict[str, Any]:
    return {
        "分析类型": analysis_type,
        "维度": dimension,
        "指标": metric,
        "值": value,
        "占比": _display_ratio(ratio),
        "业务解读": interpretation,
    }


def _metric_value(summary: pd.DataFrame, metric: str) -> int:
    if summary.empty or "指标" not in summary.columns:
        return 0
    rows = summary[summary["指标"].eq(metric)]
    if rows.empty:
        return 0
    return int(rows.iloc[0]["值"])


def _display_ratio(value: object) -> object:
    if isinstance(value, float):
        return _format_percent(value)
    return value


def _format_percent(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    rate = numerator / denominator
    # 占比/率统一约束在 0%~100%：分子（跨列累加的影响行数）与分母（去重后的记录数）
    # 口径可能不一致，若不封顶会出现 >100% 的逻辑错误，误导非专业用户。
    if rate < 0:
        return 0.0
    return min(rate, 1.0)


def _unique(items: list[str]) -> list[str]:
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
