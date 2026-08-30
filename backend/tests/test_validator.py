import pandas as pd
import pytest

from data_agent.schemas.job import AnomalyRuleConfig, ConditionConfig
from data_agent.tools.validator import append_reason, apply_anomaly_rules


def test_apply_anomaly_rules_flags_records_generically() -> None:
    df = pd.DataFrame(
        {
            "code": ["A001", "A002", "A003", None],
            "status": ["active", "inactive", None, "active"],
        }
    )

    rules = [
        AnomalyRuleConfig(
            name="missing_code",
            condition=ConditionConfig(field="code", op="is_blank"),
            reason="编码缺失",
            severity="error",
        ),
        AnomalyRuleConfig(
            name="status_not_active",
            condition=ConditionConfig(field="status", op="not_equals", value="active"),
            reason="状态非活跃",
            severity="warn",
        ),
    ]

    result = apply_anomaly_rules(df, rules)

    # Row 0 clean; row 1 inactive; row 2 blank status; row 3 blank code.
    assert result.loc[0, "abnormal_type"] == ""
    assert "状态非活跃" in result.loc[1, "abnormal_type"]
    assert "状态非活跃" in result.loc[2, "abnormal_type"]
    assert "编码缺失" in result.loc[3, "abnormal_type"]


def test_append_reason_concatenates_multiple_reasons() -> None:
    df = pd.DataFrame({"abnormal_type": ["", "已有问题"]})
    mask = pd.Series([True, True])

    append_reason(df, mask, "abnormal_type", "新问题")

    assert df.loc[0, "abnormal_type"] == "新问题"
    assert df.loc[1, "abnormal_type"] == "已有问题;新问题"


def test_missing_rule_field_fails_instead_of_silently_skipping() -> None:
    rules = [
        AnomalyRuleConfig(
            name="email_required",
            condition=ConditionConfig(field="email", op="is_blank"),
        )
    ]

    with pytest.raises(ValueError, match="email"):
        apply_anomaly_rules(pd.DataFrame({"email_address": [""]}), rules)
