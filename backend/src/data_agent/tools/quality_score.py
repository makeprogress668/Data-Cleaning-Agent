from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Deduction caps deliberately sum to 100 so the model is a principled 0-100 scale
# (the previous caps summed to 105, biasing every score low). Each axis can at most
# consume its share of the 100 points.
_CAP_UNMET_GOAL = 40
_CAP_EXCEPTION_RATE = 25
_CAP_CRITICAL_ISSUE = 12
_CAP_DIRTY_ERROR = 12
_CAP_DIRTY_WARN = 6
_CAP_JOIN_RISK = 5

# 面向业务用户的动作名。评分理由要说人话：「跨表关联未执行」而不是「lookup missing」。
ACTION_LABELS: dict[str, str] = {
    "lookup": "跨表关联",
    "filter": "按条件筛选",
    "deduplicate": "去重",
    "derive": "新增标记/计算列",
    "analyze": "汇总分析",
    "chart": "图表",
    "validate": "规则校验",
    "review": "异常复核",
    "export": "结果导出",
}

# A small share of flagged rows is expected noise on real data; grant a grace band
# so a nearly-clean dataset is not penalised for a handful of review rows.
_EXCEPTION_RATE_GRACE = 0.05


@dataclass(frozen=True)
class QualityScoreResult:
    score: int
    summary: pd.DataFrame
    deductions: pd.DataFrame


def compute_quality_score(
    total_records: int,
    usable_records: int,
    exception_records: int,
    exception_summary: pd.DataFrame,
    dirty_issue_summary: pd.DataFrame,
    relationship_candidates: pd.DataFrame | None = None,
    unmet_actions: list[str] | None = None,
) -> QualityScoreResult:
    """Score a run on whether it did the job, then on how clean the data is.

    ``unmet_actions`` names things the user asked for that the plan could not carry
    out. It is the heaviest axis on purpose: every other axis measures data hygiene,
    so without it a run that delivered none of the requested work still scored 100
    and the reflection loop had no signal that the task itself had failed.
    """

    deductions = []

    unmet = list(unmet_actions or [])
    _deduct(
        deductions,
        "目标未达成",
        min(_CAP_UNMET_GOAL, len(unmet) * 20),
        (
            "未执行：" + "、".join(ACTION_LABELS.get(action, action) for action in unmet)
            if unmet
            else "用户要求的处理动作已全部执行"
        ),
    )

    exception_rate = _safe_rate(exception_records, total_records)
    # Only penalise the portion of exceptions above the grace band, so minor noise
    # costs little while genuinely high exception rates still approach the cap.
    effective_exception_rate = max(0.0, exception_rate - _EXCEPTION_RATE_GRACE)
    _deduct(
        deductions,
        "异常率",
        min(_CAP_EXCEPTION_RATE, round(effective_exception_rate * 45)),
        f"需复核记录 {exception_records} / 总记录 {total_records}",
    )

    critical_issue_count = _critical_issue_count(exception_summary)
    _deduct(
        deductions,
        "严重业务问题",
        min(_CAP_CRITICAL_ISSUE, critical_issue_count * 3),
        f"严重问题类型数 {critical_issue_count}",
    )

    dirty_error_rows = _dirty_rows(dirty_issue_summary, severity="error")
    dirty_warn_rows = _dirty_rows(dirty_issue_summary, severity="warn")
    _deduct(
        deductions,
        "脏数据严重问题",
        min(_CAP_DIRTY_ERROR, round(_safe_rate(dirty_error_rows, total_records) * 35)),
        f"脏数据 error 影响 {dirty_error_rows} 行",
    )
    _deduct(
        deductions,
        "脏数据提醒问题",
        min(_CAP_DIRTY_WARN, round(_safe_rate(dirty_warn_rows, total_records) * 12)),
        f"脏数据 warn 影响 {dirty_warn_rows} 行",
    )

    join_risk_count = _join_risk_count(relationship_candidates)
    _deduct(
        deductions,
        "Join 膨胀风险",
        min(_CAP_JOIN_RISK, join_risk_count * 5),
        f"检测到 {join_risk_count} 个 join 膨胀风险",
    )

    total_deduction = sum(row["扣分"] for row in deductions)
    score = max(0, min(100, int(100 - total_deduction)))
    summary = pd.DataFrame(
        [
            {
                "类型": "指标",
                "指标": "数据质量评分",
                "值": score,
                "说明": "综合目标达成度、异常率、脏数据、严重问题和 join 风险计算，满分 100",
            },
            {
                "类型": "指标",
                "指标": "未达成的处理动作",
                "值": len(unmet),
                "说明": (
                    "、".join(ACTION_LABELS.get(action, action) for action in unmet)
                    if unmet
                    else "无"
                ),
            },
            {"类型": "指标", "指标": "总行数", "值": total_records, "说明": "主表记录总数"},
            {
                "类型": "指标",
                "指标": "可直接使用记录数",
                "值": usable_records,
                "说明": "进入 final_result 的记录数",
            },
            {
                "类型": "指标",
                "指标": "需复核记录数",
                "值": exception_records,
                "说明": "被排除出可用结果、需要在问题说明中复核的记录数",
            },
            {
                "类型": "指标",
                "指标": "异常率",
                "值": round(exception_rate, 4),
                "说明": "需复核 / 总行数",
            },
            {
                "类型": "指标",
                "指标": "严重问题类型数",
                "值": critical_issue_count,
                "说明": "严重级别为错误的问题类型数量",
            },
            {
                "类型": "指标",
                "指标": "脏数据问题数",
                "值": len(dirty_issue_summary),
                "说明": "Dirty Data Engine 识别的问题类型数量",
            },
            {
                "类型": "指标",
                "指标": "已自动修复问题数",
                "值": _auto_fixed_issue_count(dirty_issue_summary),
                "说明": "安全自动修复的问题类型数量",
            },
            *[
                {
                    "类型": "扣分",
                    "指标": row["扣分项"],
                    "值": row["扣分"],
                    "说明": row["原因"],
                }
                for row in deductions
                if row["扣分"] > 0
            ],
        ]
    )
    return QualityScoreResult(score=score, summary=summary, deductions=pd.DataFrame(deductions))


def exception_summary_from_business_summary(business_summary: pd.DataFrame) -> pd.DataFrame:
    """Extract the issue-type rows (with impact > 0) from a business summary frame.

    Shared by the delivery flow and the API path so both feed identical inputs to
    ``compute_quality_score`` and therefore report the same quality score for the
    same run, instead of the API using a separate simplified formula.
    """

    empty = pd.DataFrame(columns=["异常类型", "影响行数", "严重级别", "建议处理"])
    if business_summary.empty or "类型" not in business_summary.columns:
        return empty
    rows = business_summary[business_summary["类型"].eq("问题类型")].copy()
    if rows.empty:
        return empty
    renamed = rows.rename(columns={"项目": "异常类型", "数量": "影响行数"})[
        ["异常类型", "影响行数", "严重级别", "建议处理"]
    ]
    renamed = renamed[pd.to_numeric(renamed["影响行数"], errors="coerce").fillna(0) > 0]
    return renamed.reset_index(drop=True)


def _deduct(rows: list[dict[str, object]], item: str, points: int, reason: str) -> None:
    rows.append({"扣分项": item, "扣分": int(points), "原因": reason})


def _critical_issue_count(exception_summary: pd.DataFrame) -> int:
    if exception_summary.empty or "严重级别" not in exception_summary.columns:
        return 0
    return int(exception_summary["严重级别"].astype(str).eq("错误").sum())


def _dirty_rows(dirty_issue_summary: pd.DataFrame, severity: str) -> int:
    if dirty_issue_summary.empty:
        return 0
    rows = dirty_issue_summary[dirty_issue_summary["severity"].eq(severity)]
    if rows.empty:
        return 0
    return int(rows["affected_rows"].sum())


def _auto_fixed_issue_count(dirty_issue_summary: pd.DataFrame) -> int:
    if dirty_issue_summary.empty or "auto_fixed" not in dirty_issue_summary.columns:
        return 0
    return int(dirty_issue_summary["auto_fixed"].astype(bool).sum())


def _join_risk_count(relationship_candidates: pd.DataFrame | None) -> int:
    if relationship_candidates is None or relationship_candidates.empty:
        return 0
    if "join_will_expand" not in relationship_candidates.columns:
        return 0
    return int(relationship_candidates["join_will_expand"].astype(bool).sum())


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator
