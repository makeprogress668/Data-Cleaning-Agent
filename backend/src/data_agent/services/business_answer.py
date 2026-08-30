"""One business conclusion, whichever entry point asked for it.

The CLI assembled a full BusinessAnswer (metrics, findings, exception impact,
recommended actions, review preview) while the API path hand-rolled a four-number
stub for the same report file. Same job, same goal, two very different reports. This
module owns the single construction so both paths deliver the same conclusion.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from data_agent.schemas import BusinessAnswer, ChartRef, OutputFileRef
from data_agent.tools.issue_wording import display_dirty_issue_name
from data_agent.tools.quality_score import ACTION_LABELS


def build_business_answer(
    *,
    goal: str,
    input_tables: list[dict[str, Any]],
    total_records: int,
    final_result: pd.DataFrame,
    needs_review: pd.DataFrame,
    summary: pd.DataFrame,
    data_quality_summary: pd.DataFrame,
    exception_summary: pd.DataFrame,
    exception_impact: pd.DataFrame,
    dirty_issue_summary: pd.DataFrame,
    analysis_summary: pd.DataFrame,
    analysis_findings: list[str],
    document_rule_count: int,
    missing_template_field_count: int,
    recommended_actions: list[str],
    output_files: list[OutputFileRef],
    charts: list[ChartRef],
    audit_refs: list[OutputFileRef] | None = None,
    unmet_actions: list[str] | None = None,
) -> BusinessAnswer:
    """Assemble the user-facing conclusion from one executed run's evidence."""

    total = int(total_records)
    usable = len(final_result)
    exception = record_count(needs_review)
    filtered_out = max(0, total - usable - exception)
    score = score_from_quality_summary(data_quality_summary)
    critical = metric_value(data_quality_summary, "严重问题类型数")
    filtered_text = f"，按目标排除 {filtered_out} 行" if filtered_out else ""
    # An unmet request is the first thing the user needs to know — ahead of any
    # counts. Burying it in assumptions meant a run that did none of the requested
    # work still read as a clean success.
    unmet_text = (
        "。注意：你要求的"
        + "、".join(ACTION_LABELS.get(action, action) for action in (unmet_actions or []))
        + "本次未能执行"
        if unmet_actions
        else ""
    )
    result_summary = (
        f"本次共处理 {total} 行数据，其中 {usable} 行可直接使用，"
        f"{exception} 行需要业务复核{filtered_text}。数据质量评分为 {score}/100{unmet_text}。"
    )
    key_metrics = records(
        pd.DataFrame(
            [
                {"指标": "总行数", "值": total, "说明": "主表记录总数"},
                {"指标": "可直接使用", "值": usable, "说明": "进入最终结果的数据量"},
                {"指标": "需要复核", "值": exception, "说明": "存在异常或不确定的数据量"},
                *(
                    [
                        {
                            "指标": "按目标排除",
                            "值": filtered_out,
                            "说明": "按明确筛选或去重规则移出的记录数",
                        }
                    ]
                    if filtered_out
                    else []
                ),
                {"指标": "数据质量评分", "值": score, "说明": "面向业务使用的综合评分"},
            ]
        )
    )
    key_findings = unique_strings(
        [
            *_key_findings(summary, exception_summary, dirty_issue_summary),
            *analysis_findings,
        ]
    )
    for action in unmet_actions or []:
        key_findings.insert(
            0,
            f"你要求的「{ACTION_LABELS.get(action, action)}」本次未能执行，"
            "请在处理方案页查看原因。",
        )
    if document_rule_count:
        key_findings.append(f"已按说明文档中的 {document_rule_count} 条要求完成核对。")
    if missing_template_field_count:
        key_findings.append(
            f"导入模板校验发现 {missing_template_field_count} 个目标字段缺失。"
        )
    return BusinessAnswer(
        goal=goal,
        input_summary=input_tables,
        result_summary=result_summary,
        key_metrics=key_metrics,
        key_findings=key_findings,
        data_quality_score=score,
        usable_records_count=usable,
        exception_records_count=exception,
        critical_issue_count=int(critical),
        analysis_results=records(analysis_summary.head(20)),
        charts=charts,
        exception_impact=records(exception_impact),
        recommended_actions=recommended_actions,
        review_tasks=records(review_task_preview(needs_review)),
        assumptions=[
            "结果依据当前上传文件和本次任务目标生成。",
            "需要人工确认的记录会单独列出，不会自动判定为可用。",
            "说明文档中的明确要求会用于结果核对。",
        ],
        limitations=[
            "无法识别的字段不会被猜测补全。",
            "扫描图片类文档可能无法完整读取其中的文字内容。",
        ],
        output_files=output_files,
        audit_refs=audit_refs or [],
    )


def recommended_actions_from_impact(exception_impact: pd.DataFrame) -> list[str]:
    if exception_impact.empty:
        return []
    actions: list[str] = []
    for column in ("建议处理", "推荐处理", "recommended_action"):
        if column not in exception_impact.columns:
            continue
        for action in exception_impact[column].dropna().astype(str).tolist():
            if action and action not in actions:
                actions.append(action)
    return actions


def _key_findings(
    summary: pd.DataFrame,
    exception_summary: pd.DataFrame,
    dirty_issue_summary: pd.DataFrame,
) -> list[str]:
    findings: list[str] = []
    # The same finding arrives from two places, so track what has been said. The
    # dirty-data side also arrives untranslated: users were shown raw detector ids
    # ("发现 numeric_stored_as_text") directly under the Chinese line describing the
    # very same issue.
    reported: set[str] = set()
    if not exception_summary.empty:
        top = exception_summary.sort_values(by="影响行数", ascending=False).head(3)
        for row in top.to_dict(orient="records"):
            name = str(row["异常类型"])
            reported.add(name)
            findings.append(f"{name} 影响 {row['影响行数']} 行，建议：{row['建议处理']}")
    if not dirty_issue_summary.empty:
        ranked = dirty_issue_summary.sort_values(by="affected_rows", ascending=False)
        for row in ranked.to_dict(orient="records"):
            name = display_dirty_issue_name(row["issue_type"])
            if name in reported:
                continue
            reported.add(name)
            findings.append(
                f"{name} 影响 {row['affected_rows']} 行，"
                f"建议：{row['recommended_action']}"
            )
            if len(reported) >= 6:
                break
    if summary.empty:
        findings.append("未识别到可汇总的业务问题。")
    return findings


def review_task_preview(needs_review: pd.DataFrame) -> pd.DataFrame:
    columns = ["源行号", "问题类型", "严重级别", "建议处理", "备注"]
    if needs_review.empty:
        return pd.DataFrame(columns=columns)
    available = [column for column in columns if column in needs_review.columns]
    result = needs_review[available].head(20).copy()
    for column in columns:
        if column not in result.columns:
            result[column] = ""
    return result[columns]


def score_from_quality_summary(data_quality_summary: pd.DataFrame) -> int:
    return int(metric_value(data_quality_summary, "数据质量评分"))


def metric_value(data_quality_summary: pd.DataFrame, metric: str) -> int:
    if data_quality_summary.empty or "指标" not in data_quality_summary.columns:
        return 0
    rows = data_quality_summary[data_quality_summary["指标"].eq(metric)]
    if rows.empty:
        return 0
    return int(rows.iloc[0]["值"])


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    normalized = df.astype(object).where(pd.notna(df), None)
    return normalized.to_dict(orient="records")


def record_count(df: pd.DataFrame) -> int:
    """Count distinct source records, not rows, so one record with several findings
    is never double-counted."""

    if df.empty:
        return 0
    for column in ("源行号", "source_row_index", "_source_row_index"):
        if column in df.columns:
            values = df[column].dropna()
            if not values.empty:
                return int(values.nunique())
    return int(len(df))


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
