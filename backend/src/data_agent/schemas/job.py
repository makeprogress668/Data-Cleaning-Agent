from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from data_agent.schemas.task import ActionAuthorization

LookupMatchMode = Literal[
    "exact",
    "trim",
    "case_insensitive",
    "normalized_exact",
    "fuzzy",
]
DuplicateStrategy = Literal["first", "error", "list", "aggregate", "review"]
ConditionOperator = Literal[
    "is_blank",
    "not_blank",
    "is_null",
    "not_null",
    "duplicated",
    "lookup_missing",
    "numeric_invalid",
    "date_invalid",
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "regex_match",
    "regex_not_match",
    "in",
    "not_in",
    "gt",
    "gte",
    "lt",
    "lte",
]
FormulaOperator = Literal[
    "copy",
    "literal",
    "trim",
    "upper",
    "lower",
    "concat",
    "coalesce",
    "map",
    "if_else",
    "ifs",
    "and",
    "or",
    "not",
    "add",
    "subtract",
    "multiply",
    "divide",
    "is_blank",
    "equals",
    "isin",
    "contains",
    "iferror",
    "ifna",
    "countif",
    "sumif",
    "countifs",
    "sumifs",
    "filter_rows",
    "dedupe",
    "sort_rank",
    "top_n",
    "date_diff",
    "fill_down",
    "xlookup",
    "index_match",
    "left",
    "right",
    "mid",
    "len",
    "substitute",
    "replace",
    "text",
    "date",
    "year",
    "month",
    "day",
    "today",
    "now",
    "round",
    "roundup",
    "rounddown",
    "abs",
    "bucket",
    "regex_extract",
    "regex_replace",
    "split",
    "join",
    "textjoin",
    "remove_special_chars",
    "normalize_width",
]
PivotAggregation = Literal[
    "count",
    "sum",
    "mean",
    "min",
    "max",
    "median",
    "nunique",
    "first",
    "last",
]
AnomalySeverity = Literal["error", "warn"]


class SourceConfig(BaseModel):
    path: Path
    sheet: Optional[Union[str, int]] = None


class LookupConfig(BaseModel):
    name: str = "lookup"
    source: Optional[SourceConfig] = None
    source_table: Optional[str] = None
    left_key: Optional[str] = None
    right_key: Optional[str] = None
    left_keys: list[str] = Field(default_factory=list)
    right_keys: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    field_aliases: dict[str, str] = Field(default_factory=dict)
    suffix: str = "_lookup"
    match_field: Optional[str] = None
    confidence_field: Optional[str] = None
    explanation_field: Optional[str] = None
    ambiguity_field: Optional[str] = None
    match_mode: LookupMatchMode = "exact"
    duplicate_strategy: Optional[DuplicateStrategy] = None
    aggregate_sep: str = ", "
    fuzzy_threshold: float = 0.85

    @model_validator(mode="after")
    def validate_lookup_keys(self) -> LookupConfig:
        left_keys = self.left_keys or ([self.left_key] if self.left_key else [])
        right_keys = self.right_keys or ([self.right_key] if self.right_key else [])
        if not left_keys:
            raise ValueError("Lookup requires left_key or left_keys")
        if not right_keys:
            raise ValueError("Lookup requires right_key or right_keys")
        if len(left_keys) != len(right_keys):
            raise ValueError("left_keys and right_keys must have the same length")
        return self


class RollupConfig(BaseModel):
    """Summarise a detail table onto the master by key — Excel's cross-sheet SUMIF.

    Separate from LookupConfig because the two answer different questions. A lookup
    brings one matching value across; a rollup brings the total of every matching row,
    and it cannot multiply the master's rows the way joining a detail table does.
    """

    name: str = "rollup"
    source_table: str = Field(min_length=1)
    left_key: str = Field(min_length=1)
    right_key: str = Field(min_length=1)
    measure: str = Field(min_length=1)
    agg: PivotAggregation = "sum"
    output: str = Field(min_length=1)


class MeltConfig(BaseModel):
    """Unpivot a wide block into rows — twelve month columns become a 月份 column.

    Runs before anything else, like a union: profiling, anomaly detection and every
    later step should see the shape the user actually wants analysed.
    """

    table: str = Field(min_length=1)
    value_columns: list[str] = Field(min_length=2)
    variable_name: str = Field(min_length=1)
    value_name: str = Field(min_length=1)


class FieldMappingConfig(BaseModel):
    rename: dict[str, str] = Field(default_factory=dict)
    value_maps: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ConditionConfig(BaseModel):
    field: str
    op: ConditionOperator
    value: Any = None
    values: list[Any] = Field(default_factory=list)
    case_sensitive: bool = False
    # "Keep the rows this does NOT match" — not the same as the opposite comparison.
    # "删掉金额小于0的" was compiled as "keep 金额 >= 0", and a blank or non-numeric 金额
    # satisfies neither, so those rows were deleted too. A cell that cannot be compared
    # has not been shown to match, so it stays.
    negate: bool = False


class FormulaConfig(BaseModel):
    output: str
    op: FormulaOperator
    required: bool = True
    # Why this rule is in the plan. Defaults to "inferred" because an absent
    # declaration is not authorization: a hand-written config or future caller must
    # stop rather than inherit a free pass. Rules compiled from a goal get "stated"
    # only after planning.provenance verified a quote. Authorization is independent of
    # the impact gate: even a stated action pauses if its blast radius is high/unknown.
    authorization: ActionAuthorization = "inferred"
    source: Optional[str] = None
    columns: list[str] = Field(default_factory=list)
    value: Any = None
    values: list[Any] = Field(default_factory=list)
    mapping: dict[str, Any] = Field(default_factory=dict)
    default: Any = None
    sep: str = ""
    condition: Optional[ConditionConfig] = None
    conditions: list[ConditionConfig] = Field(default_factory=list)
    true_value: Any = None
    false_value: Any = None


class AnomalyRuleConfig(BaseModel):
    name: str
    condition: ConditionConfig
    reason: Optional[str] = None
    severity: AnomalySeverity = "error"


class PivotMetric(BaseModel):
    column: Optional[str] = None
    agg: PivotAggregation = "count"
    output_name: str = "count"


class PivotConfig(BaseModel):
    group_by: list[str]
    metrics: list[PivotMetric] = Field(default_factory=lambda: [PivotMetric()])


class ExportConfig(BaseModel):
    output_file: Path = Path("data/output/cleaned_result.xlsx")
    include_source_tables: bool = False
    include_internal_sheets: bool = False
    # The 问题汇总 (metrics) sheet is process information, not a deliverable, so it is
    # off by default (P2). Counts/score still reach the user via the report panel and
    # the API response; internal-sheet exports re-add it for audit.
    include_summary_sheet: bool = False


class InputConfig(BaseModel):
    path: Optional[Path] = None
    include_unstructured_docs: bool = True


class DirtyDataConfig(BaseModel):
    enabled: bool = True
    auto_fix_safe_issues: bool = True
    mark_uncertain_for_review: bool = True


class MatchingConfig(BaseModel):
    match_modes: list[LookupMatchMode] = Field(
        default_factory=lambda: [
            "exact",
            "trim",
            "case_insensitive",
            "normalized_exact",
            "fuzzy",
        ]
    )
    duplicate_key_strategy: DuplicateStrategy = "review"


class AnalysisConfig(BaseModel):
    enabled: bool = True


class ChartConfig(BaseModel):
    enabled: bool = True
    formats: list[str] = Field(default_factory=lambda: ["html"])


class QualityScoreConfig(BaseModel):
    enabled: bool = True
    critical_fields: list[str] = Field(default_factory=list)


class ExceptionPolicyConfig(BaseModel):
    exclude_critical_from_final: bool = True
    include_minor_in_final: bool = True
    require_review_for_unmatched_lookup: bool = True


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["markdown", "html"])


class AuditConfig(BaseModel):
    enabled: bool = True
    default_visible: bool = False


class JobConfig(BaseModel):
    name: str = "data-cleaning-job"
    goal: Optional[str] = None
    output_mode: str = "cleaning_result"
    business_views: list[str] = Field(default_factory=list)
    input: InputConfig = Field(default_factory=InputConfig)
    # Deliver lookup fills as live Excel formulas instead of computed values. Asked
    # for by the goal ("用 VLOOKUP 匹配"), because it changes what the user opens: a
    # cell they can click, read and recalculate rather than a number they must trust.
    formula_output: bool = False
    # Column list the delivered table must match exactly, in order. An uploaded
    # template's headers carry into the deliverable unchanged: that file usually
    # exists to be fed somewhere afterwards, and a renamed or reordered column breaks
    # whatever waits downstream.
    target_schema: list[str] = Field(default_factory=list)
    # Tables to stack into the main table before anything else runs. Named rather than
    # inferred at execution time: which files make up one dataset is a reading of the
    # user's intent, and it belongs in the reviewable plan like every other decision.
    union_tables: list[str] = Field(default_factory=list)
    # Detail-table totals carried onto the master. Declared alongside lookups
    # because the user asks for both the same way: 「把明细的金额汇总到客户表」.
    rollups: list[RollupConfig] = Field(default_factory=list)
    # Wide-to-long reshaping, applied to the raw tables before profiling.
    melt: Optional[MeltConfig] = None
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    base_table: Optional[str] = None
    main_source: Optional[SourceConfig] = None
    lookup: Optional[LookupConfig] = None
    lookups: list[LookupConfig] = Field(default_factory=list)
    field_mapping: FieldMappingConfig = Field(default_factory=FieldMappingConfig)
    formulas: list[FormulaConfig] = Field(default_factory=list)
    anomaly_rules: list[AnomalyRuleConfig] = Field(default_factory=list)
    abnormal_field: str = "abnormal_type"
    pivot: PivotConfig = Field(default_factory=lambda: PivotConfig(group_by=["abnormal_type"]))
    export: ExportConfig = Field(default_factory=ExportConfig)
    dirty_data: DirtyDataConfig = Field(default_factory=DirtyDataConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    charts: ChartConfig = Field(default_factory=ChartConfig)
    quality_score: QualityScoreConfig = Field(default_factory=QualityScoreConfig)
    exception_policy: ExceptionPolicyConfig = Field(default_factory=ExceptionPolicyConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)


# Structural subset the post-execution reflection layer may adjust. This is the
# ordered source of truth; it must never rename the job or repoint its export.
STRUCTURAL_OVERRIDE_KEYS: tuple[str, ...] = (
    "base_table",
    "lookup",
    "lookups",
    "field_mapping",
    "formulas",
    "anomaly_rules",
    "abnormal_field",
    "pivot",
)

# Reflection may tune processing details but cannot change the task's primary
# entity after planning. Caller-supplied configuration still uses the broader
# structural whitelist above.
REFLECTION_OVERRIDE_KEYS: tuple[str, ...] = tuple(
    key for key in STRUCTURAL_OVERRIDE_KEYS if key != "base_table"
)

# Full whitelist of JobConfig keys a caller-supplied override may set: the
# structural keys plus the execution-policy blocks a caller legitimately controls.
# Defined here (a leaf module) so the processing overrides gate and the reflection
# layer share one source of truth instead of each maintaining a drifting copy.
#
# ``dirty_data`` and ``exception_policy`` are included because they are what a caller
# uses to override goal-inferred behaviour ("clean this but do not auto-fix",
# "deliver minor issues anyway"). Overrides are merged *after* the goal-derived
# defaults, so an explicit caller decision always beats inference.
JOB_CONFIG_OVERRIDE_KEYS: frozenset[str] = frozenset(STRUCTURAL_OVERRIDE_KEYS) | {
    "name",
    "export",
    "quality_score",
    "business_views",
    "dirty_data",
    "exception_policy",
}
