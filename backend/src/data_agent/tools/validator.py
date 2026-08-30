from __future__ import annotations

import pandas as pd

from data_agent.schemas.job import AnomalyRuleConfig
from data_agent.tools.conditions import evaluate_condition


def append_reason(
    df: pd.DataFrame,
    mask: pd.Series,
    abnormal_field: str,
    reason: str,
) -> None:
    current = df.loc[mask, abnormal_field].fillna("").astype(str)
    blank_index = current[current.eq("")].index
    non_blank = current[~current.eq("")]
    df.loc[blank_index, abnormal_field] = reason
    df.loc[non_blank.index, abnormal_field] = non_blank + ";" + reason


def apply_anomaly_rules(
    df: pd.DataFrame,
    rules: list[AnomalyRuleConfig],
    abnormal_field: str = "abnormal_type",
) -> pd.DataFrame:
    result = df.copy()
    if abnormal_field not in result.columns:
        result[abnormal_field] = ""
    result[abnormal_field] = result[abnormal_field].fillna("").astype(str)

    for rule in rules:
        if rule.condition.field not in result.columns:
            raise ValueError(
                f"校验规则 {rule.name} 引用了结果中不存在的字段："
                f"{rule.condition.field}"
            )
        reason = rule.reason or rule.name
        append_reason(result, evaluate_condition(result, rule.condition), abnormal_field, reason)

    return result
