"""Validated intent contract between goal understanding and planning."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import NotRequired, Required, TypedDict

TaskConfidence = Literal["high", "medium", "low"]
TaskAction = Literal[
    "clean",
    "filter",
    "deduplicate",
    "lookup",
    "derive",
    "validate",
    "analyze",
    "chart",
    "review",
    "annotate",
    "export",
]
MissingSlotKind = Literal[
    "primary_table",
    "target_field",
    "business_rule",
    "retention_rule",
    "output_requirement",
    "confirmation",
]
# Whether the user asked for a row-removing rule, or something else proposed it.
# "stated" authorizes the action; a separate impact policy may still escalate a large
# or unknowable change for review. "implied" is plumbing a stated action requires.
# "inferred" is dropped before it reaches a plan. See planning.provenance.
ActionAuthorization = Literal["stated", "implied", "inferred"]
TaskRuleSource = Literal["goal_text", "llm"]
TaskFilterOperator = Literal[
    "gt",
    "gte",
    "lt",
    "lte",
    "equals",
    "not_equals",
    "in",
    "not_in",
]
TaskDeduplicationStrategy = Literal["first", "last", "latest", "earliest"]
TaskAggregationName = Literal[
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


class TaskFilter(TypedDict, total=False):
    """A row-retention rule, still represented as JSON at persistence boundaries."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    field: Required[str]
    op: Required[TaskFilterOperator]
    mode: Required[Literal["keep", "exclude"]]
    value: NotRequired[Any]
    values: NotRequired[list[Any]]
    evidence: NotRequired[str]
    source: NotRequired[TaskRuleSource]
    authorization: NotRequired[ActionAuthorization]


class TaskDeduplication(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    fields: Required[list[str]]
    strategy: Required[TaskDeduplicationStrategy]
    order_by: NotRequired[str]
    evidence: NotRequired[str]
    source: NotRequired[TaskRuleSource]
    authorization: NotRequired[ActionAuthorization]


class TaskCondition(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    field: Required[str]
    op: Required[TaskFilterOperator]
    value: NotRequired[Any]
    values: NotRequired[list[Any]]


class TaskDerivationBranch(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    when: TaskCondition
    then: str


class TaskDerivation(TypedDict, total=False):
    """Either a conditional label column or a deterministic column split."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    kind: NotRequired[Literal["split"]]
    output: NotRequired[str]
    branches: NotRequired[list[TaskDerivationBranch]]
    otherwise: NotRequired[str]
    source: NotRequired[str]
    sep: NotRequired[str]
    outputs: NotRequired[list[str]]


class TaskUnion(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    tables: Required[list[str]]
    source: NotRequired[TaskRuleSource]
    authorization: NotRequired[ActionAuthorization]


class TaskRollup(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    source_table: str
    left_key: str
    right_key: str
    measure: str
    agg: TaskAggregationName
    output: str


class TaskMelt(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    table: Required[str]
    value_columns: Required[list[str]]
    variable_name: Required[str]
    value_name: Required[str]


class TaskAggregationMetric(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    column: str | None
    agg: TaskAggregationName
    output_name: str


class TaskTimeBucket(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    field: str
    granularity: Literal["week", "month", "quarter", "year"]
    output_name: str


class TaskAggregation(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    group_by: Required[list[str]]
    metrics: Required[list[TaskAggregationMetric]]
    source: NotRequired[TaskRuleSource]
    column_field: NotRequired[str]
    time_bucket: NotRequired[TaskTimeBucket]
    top_n: NotRequired[int]
    rank_within: NotRequired[list[str]]


class TaskJoinRequirement(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    required: bool
    candidate_tables: list[str]


class TaskOutputIntent(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    requested_outputs: NotRequired[list[str]]
    business_views: NotRequired[list[str]]
    needs_charts: NotRequired[bool]
    chart_type: NotRequired[Literal["", "line", "bar", "pie"]]
    needs_review: NotRequired[bool]
    # Reporting a problem and withholding its row are separate decisions. This flag
    # carries the latter through the typed contract so an LLM JobConfig refinement
    # cannot silently change which rows reach the primary result.
    withhold_flagged_rows: NotRequired[bool]
    needs_report: NotRequired[bool]
    needs_change_manifest: NotRequired[bool]


class _TaskOperationBase(BaseModel):
    """One typed user-requested operation in the intent contract."""

    model_config = ConfigDict(extra="forbid")


class CleanTaskOperation(_TaskOperationBase):
    kind: Literal["clean"] = "clean"
    business_rules: list[str] = Field(default_factory=list)


class FilterTaskOperation(_TaskOperationBase):
    kind: Literal["filter"] = "filter"
    filters: list[TaskFilter] = Field(default_factory=list)


class DeduplicateTaskOperation(_TaskOperationBase):
    kind: Literal["deduplicate"] = "deduplicate"
    rules: list[TaskDeduplication] = Field(default_factory=list)


class LookupTaskOperation(_TaskOperationBase):
    kind: Literal["lookup"] = "lookup"
    joins: list[TaskJoinRequirement] = Field(default_factory=list)
    rollups: list[TaskRollup] = Field(default_factory=list)


class DeriveTaskOperation(_TaskOperationBase):
    kind: Literal["derive"] = "derive"
    derivations: list[TaskDerivation] = Field(default_factory=list)


class ValidateTaskOperation(_TaskOperationBase):
    kind: Literal["validate"] = "validate"
    business_rules: list[str] = Field(default_factory=list)


class AnalyzeTaskOperation(_TaskOperationBase):
    kind: Literal["analyze"] = "analyze"
    aggregation: TaskAggregation | dict[Literal["_empty"], None] = Field(
        default_factory=dict
    )


class ChartTaskOperation(_TaskOperationBase):
    kind: Literal["chart"] = "chart"


class ReviewTaskOperation(_TaskOperationBase):
    kind: Literal["review"] = "review"


class AnnotateTaskOperation(_TaskOperationBase):
    kind: Literal["annotate"] = "annotate"
    derivations: list[TaskDerivation] = Field(default_factory=list)


class ExportTaskOperation(_TaskOperationBase):
    kind: Literal["export"] = "export"
    target_schema: list[str] = Field(default_factory=list)
    output_intent: TaskOutputIntent = Field(default_factory=dict)


TaskOperation = Annotated[
    CleanTaskOperation
    | FilterTaskOperation
    | DeduplicateTaskOperation
    | LookupTaskOperation
    | DeriveTaskOperation
    | ValidateTaskOperation
    | AnalyzeTaskOperation
    | ChartTaskOperation
    | ReviewTaskOperation
    | AnnotateTaskOperation
    | ExportTaskOperation,
    Field(discriminator="kind"),
]


class FieldReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1)
    field: str = Field(min_length=1)


class MissingSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1)
    kind: MissingSlotKind
    question: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    priority: Literal["high", "medium"] = "high"
    options: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    """What the user wants, independent from how the executor implements it."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1, 2] = 2
    objective: str = Field(min_length=1)
    primary_entity: Optional[str] = None
    target_tables: list[str] = Field(default_factory=list)
    target_fields: list[FieldReference] = Field(default_factory=list)
    actions: list[TaskAction] = Field(default_factory=list)
    # v2's authoritative operation list. The legacy action-specific fields below are
    # deterministic compatibility projections for JobConfig-era code; validation
    # rejects any divergence between the two representations.
    operations: list[TaskOperation] = Field(default_factory=list)
    filters: list[TaskFilter] = Field(default_factory=list)
    deduplication: list[TaskDeduplication] = Field(default_factory=list)
    # Columns the task must ADD. Without this slot the spec could only express which
    # rows to remove, so "把金额大于 1000 的标记为大额订单" had nowhere to land: the
    # condition was captured as a row filter and the records were deleted instead of
    # labelled.
    derivations: list[TaskDerivation] = Field(default_factory=list)
    # The shape an analysis goal asks for: 按<group_by>统计<metrics>。Without it the
    # delivery contract could only describe a row-level table, so a pivot that had
    # already been computed never reached the workbook.
    # Tables that are one dataset split across files (12 monthly exports, identical
    # columns). Without this slot the pipeline could only nominate one of them as the
    # main table, so "把两个月的数据合并" delivered one month and said nothing.
    union: list[TaskUnion] = Field(default_factory=list)
    # Detail-table totals carried onto the master — Excel's cross-sheet SUMIF.
    rollups: list[TaskRollup] = Field(default_factory=list)
    # 宽转长：十二个月份列变成一个月份列。
    melt: TaskMelt | dict[Literal["_empty"], None] = Field(default_factory=dict)
    # The exact shape the deliverable must have, in order. Comes from an uploaded
    # blank template's headers or from a column list the goal writes out. Empty means
    # "whatever the processing produces" — the ordinary case.
    target_schema: list[str] = Field(default_factory=list)
    aggregation: TaskAggregation | dict[Literal["_empty"], None] = Field(
        default_factory=dict
    )
    join_requirements: list[TaskJoinRequirement] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    output_intent: TaskOutputIntent = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    # Actions the user asked for that this input cannot support (no joinable key,
    # only one table…). Kept alongside `actions` instead of quietly deleted, so the
    # score and the conclusion can say what was not done.
    unmet_actions: list[TaskAction] = Field(default_factory=list)
    confidence: TaskConfidence = "low"
    missing_slots: list[MissingSlot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_structured_requirements(self) -> TaskSpec:
        self._synchronize_operations()
        if len(self.actions) != len(set(self.actions)):
            raise ValueError("TaskSpec actions must be unique")
        for item in self.filters:
            if not item["field"].strip():
                raise ValueError("TaskSpec filter field cannot be empty")
            if item["op"] in {"in", "not_in"}:
                if not item.get("values"):
                    raise ValueError("TaskSpec set filter requires values")
            elif "value" not in item:
                raise ValueError("TaskSpec comparison filter requires value")
        for item in self.deduplication:
            if not item["fields"] or any(not field.strip() for field in item["fields"]):
                raise ValueError("TaskSpec deduplication requires non-empty fields")
            if item["strategy"] in {"latest", "earliest"} and not item.get("order_by"):
                raise ValueError("TaskSpec ordered deduplication requires order_by")
        for item in self.derivations:
            if item.get("kind") == "split":
                if not item.get("source") or not item.get("sep") or not item.get("outputs"):
                    raise ValueError("TaskSpec split derivation is incomplete")
            elif not item.get("output") or not item.get("branches"):
                raise ValueError("TaskSpec conditional derivation is incomplete")
        if self.melt and not self.melt.get("value_columns"):
            raise ValueError("TaskSpec melt requires value_columns")
        if self.aggregation and not self.aggregation.get("metrics"):
            raise ValueError("TaskSpec aggregation requires metrics")
        return self

    def _synchronize_operations(self) -> None:
        if not self.operations:
            object.__setattr__(
                self,
                "operations",
                [_operation_from_legacy(self, action) for action in self.actions],
            )
            return

        operation_actions = [operation.kind for operation in self.operations]
        if len(operation_actions) != len(set(operation_actions)):
            raise ValueError("TaskSpec operations must have unique kinds")
        if self.actions and self.actions != operation_actions:
            raise ValueError("TaskSpec actions diverge from typed operations")
        if not self.actions:
            object.__setattr__(self, "actions", operation_actions)

        _synchronize_projection(self, "filters", FilterTaskOperation, "filters")
        _synchronize_projection(
            self,
            "deduplication",
            DeduplicateTaskOperation,
            "rules",
        )
        _synchronize_projection(self, "derivations", DeriveTaskOperation, "derivations")
        _synchronize_projection(self, "aggregation", AnalyzeTaskOperation, "aggregation")
        _synchronize_projection(self, "join_requirements", LookupTaskOperation, "joins")
        _synchronize_projection(self, "rollups", LookupTaskOperation, "rollups")
        _synchronize_projection(self, "target_schema", ExportTaskOperation, "target_schema")
        _synchronize_projection(self, "output_intent", ExportTaskOperation, "output_intent")


def _operation_from_legacy(task: TaskSpec, action: TaskAction) -> TaskOperation:
    if action == "clean":
        return CleanTaskOperation(business_rules=task.business_rules)
    if action == "filter":
        return FilterTaskOperation(filters=task.filters)
    if action == "deduplicate":
        return DeduplicateTaskOperation(rules=task.deduplication)
    if action == "lookup":
        return LookupTaskOperation(joins=task.join_requirements, rollups=task.rollups)
    if action == "derive":
        return DeriveTaskOperation(derivations=task.derivations)
    if action == "validate":
        return ValidateTaskOperation(business_rules=task.business_rules)
    if action == "analyze":
        return AnalyzeTaskOperation(aggregation=task.aggregation)
    if action == "chart":
        return ChartTaskOperation()
    if action == "review":
        return ReviewTaskOperation()
    if action == "annotate":
        return AnnotateTaskOperation(derivations=task.derivations)
    return ExportTaskOperation(
        target_schema=task.target_schema,
        output_intent=task.output_intent,
    )


def _synchronize_projection(
    task: TaskSpec,
    field_name: str,
    operation_type: type[_TaskOperationBase],
    operation_field: str,
) -> None:
    operation = next(
        (item for item in task.operations if isinstance(item, operation_type)),
        None,
    )
    if operation is None:
        return
    projected = getattr(operation, operation_field)
    legacy = getattr(task, field_name)
    if legacy and legacy != projected:
        raise ValueError(
            f"TaskSpec {field_name} diverges from typed operations"
        )
    if not legacy and projected:
        object.__setattr__(task, field_name, projected)
