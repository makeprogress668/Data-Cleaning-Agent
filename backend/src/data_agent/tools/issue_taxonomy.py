"""What an issue label *is*: its format, its class, its severity.

Anomaly labels are internal tokens — ``amount_null_values``, ``duplicate_订单号``,
``dirty:客户名称:leading_trailing_spaces``, ``doc_rule_金额_min_value``. Three
different questions get asked about them, and mixing the answers together is what made
``audit.py`` 889 lines: *what kind of problem is this* (here), *what do we call it in
front of a user* (``issue_wording``), and *which rows does it apply to*
(``audit``). Both display bugs in the last round came from wording logic sitting inside
frame-building code, where nobody looked for it.

This module is the structural half. It knows the label grammar and nothing about
DataFrames or Chinese wording, so it stays cheap to test and safe to import anywhere.
"""

from __future__ import annotations

# Reasons the dirty-data engine contributes carry this prefix (see pipelines.runner)
# so the exception policy can recognise them and the display layer can translate them.
DIRTY_ISSUE_PREFIX = "dirty:"


def dirty_label_parts(label: str) -> tuple[str, str]:
    """Split ``dirty:<field>:<issue_type>`` into its field and issue type."""

    body = str(label).removeprefix(DIRTY_ISSUE_PREFIX)
    field, _, issue_type = body.rpartition(":")
    return field, issue_type or body


def classify_label_source(label: str) -> str:
    if label.endswith("_not_found"):
        return "lookup_rule"
    if label.endswith("_null_values") or label.endswith("_empty_strings"):
        return "profile_quality_rule"
    if label.startswith("duplicate_"):
        return "duplicate_rule"
    return "custom_rule"


def severity_for_label(label: str) -> str:
    if label in {"duplicate_record"} or label.startswith("duplicate_"):
        return "warn"
    if label.endswith("_empty_strings"):
        return "warn"
    return "error"


def taxonomy_category(label: str) -> str:
    if label.endswith("_not_found"):
        return "lookup_mapping"
    if label.endswith("_null_values") or label.endswith("_empty_strings"):
        return "data_quality"
    if label == "duplicate_record" or label.startswith("duplicate_"):
        return "duplicate"
    return "custom"


def classify_reason_code(label: str) -> str:
    if label.endswith("_not_found"):
        return "LOOKUP_NOT_FOUND"
    if label.endswith("_null_values"):
        return "PROFILE_NULL_VALUES"
    if label.endswith("_empty_strings"):
        return "PROFILE_EMPTY_STRINGS"
    if label == "duplicate_record" or label.startswith("duplicate_"):
        return "DUPLICATE_RECORD"
    return label.upper()


def hit_rule_name(label: str, label_source: str) -> str:
    if label_source == "lookup_rule":
        return label.removesuffix("_not_found")
    return label
