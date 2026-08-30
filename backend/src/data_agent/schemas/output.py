"""Goal-derived contract for minimal user-facing artifacts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from data_agent.schemas.task import TaskSpec

ChartType = Literal["bar", "line", "pie", "scatter", "heatmap"]
# Name of the optional target-system projection sheet. Mirrors
# ``tools.delivery_view.IMPORT_VIEW_SHEET``; declared here so the schema stays a leaf
# module with no dependency on the tools package.
IMPORT_VIEW_ARTIFACT = "可导入数据"
# 聚合结果的固定 sheet 名。透视一直在执行计划里跑，但 OutputSpec 只能声明一张
# 行级明细表，算出来的汇总进不了工作簿——用户要「按城市统计」拿到的还是原始明细。
AGGREGATE_ARTIFACT = "汇总结果"
# The account of what this run changed in the user's data.
CHANGE_MANIFEST_ARTIFACT = "本次改动"


class TableArtifactSpec(BaseModel):
    type: Literal["table"] = "table"
    name: str = Field(min_length=1)
    fields: list[str] = Field(default_factory=list)
    field_policy: Literal["exact", "prefix", "declared_subset", "source_dynamic"] = "exact"
    row_scope: Literal[
        "all_input_rows",
        "task_result_rows",
        "review_rows",
        "summary_rows",
        "source_rows",
        "change_rows",
    ] = "task_result_rows"
    exact_rows: int | None = Field(default=None, ge=0)
    sort_by: list[str] = Field(default_factory=list)
    preserve_input_order: bool = True
    description: str = ""
    # Some artifacts only make sense once the run has happened: 本次改动 is declared
    # because the goal authorised edits, but whether any edit occurred is not knowable
    # at planning time. Declaring it unconditionally would add an empty sheet to every
    # cleaning delivery; leaving it undeclared would let it appear unannounced. Optional
    # means "declared, written only if it has something to say".
    optional: bool = False


class AggregateArtifactSpec(BaseModel):
    """A summary table: the shape "按城市统计总金额" actually asks for.

    ``group_by`` may be empty, which means a grand total — "统计所有订单的金额总和" is
    a summary of one row. Requiring a dimension made that request fall through to no
    summary at all, and the user got the detail sheet back instead of their number.
    """

    type: Literal["aggregate"] = "aggregate"
    name: str = Field(min_length=1)
    group_by: list[str] = Field(default_factory=list)
    # 「行是城市，列是月份」—— the second dimension becomes columns rather than
    # more rows. Same numbers either way, but a report laid out the wrong way
    # is one nobody uses. Empty means an ordinary grouped summary.
    column_field: str = ""
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    # "按月统计" names a dimension no column holds. Derived here for the summary alone,
    # so the detail sheet does not gain a 月份 column the user never asked to see.
    time_bucket: dict[str, str] = Field(default_factory=dict)
    # "前3名". With rank_within set it means three per group — the within-group ranking
    # Excel needs an array formula for.
    top_n: int = 0
    rank_within: list[str] = Field(default_factory=list)
    description: str = ""


class ChartOutputSpec(BaseModel):
    chart_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    business_question: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    chart_type: ChartType
    file_format: Literal["html", "svg"] = "html"


class ReportOutputSpec(BaseModel):
    enabled: bool = False
    detail_level: Literal["brief", "standard", "detailed"] = "brief"
    formats: list[Literal["markdown", "html"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_formats(self) -> ReportOutputSpec:
        if self.enabled and not self.formats:
            raise ValueError("enabled report must declare at least one format")
        if not self.enabled and self.formats:
            raise ValueError("disabled report cannot declare formats")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("report formats must be unique")
        return self


class OutputSpec(BaseModel):
    version: Literal[1, 2] = 2
    primary_artifact: TableArtifactSpec = Field(
        default_factory=lambda: TableArtifactSpec(
            name="处理结果",
            description="与用户目标直接对应的最终数据",
        )
    )
    secondary_artifacts: list[TableArtifactSpec] = Field(default_factory=list)
    charts: list[ChartOutputSpec] = Field(default_factory=list)
    report: ReportOutputSpec = Field(default_factory=ReportOutputSpec)
    include_review_view: bool = False
    # Declared whenever the task is allowed to edit or remove rows, so the run can
    # account for what it did. Not declared for read-only goals: there is nothing to
    # account for, and an empty sheet is noise.
    include_change_manifest: bool = False
    # A system-import projection is an extra *declared* sheet, never a silent swap of
    # the primary result. The CLI used to replace 处理结果 with this view whenever an
    # import template was uploaded, so the same goal delivered different content
    # depending on the entry point.
    include_import_view: bool = False
    aggregate: AggregateArtifactSpec | None = None
    include_audit: bool = False

    @model_validator(mode="after")
    def validate_unique_outputs(self) -> OutputSpec:
        artifact_names = [
            self.primary_artifact.name,
            *(artifact.name for artifact in self.secondary_artifacts),
        ]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("artifact names must be unique")
        chart_ids = [chart.chart_id for chart in self.charts]
        if len(chart_ids) != len(set(chart_ids)):
            raise ValueError("chart outputs must be unique")
        chart_signatures = [
            (chart.dimension, chart.metric, chart.chart_type) for chart in self.charts
        ]
        if len(chart_signatures) != len(set(chart_signatures)):
            raise ValueError("duplicate chart metrics are not allowed")
        declared = {artifact.name for artifact in self.secondary_artifacts}
        if self.include_review_view != ("问题说明" in declared):
            raise ValueError(
                "include_review_view must match the declared 问题说明 artifact"
            )
        if self.include_change_manifest != (CHANGE_MANIFEST_ARTIFACT in declared):
            raise ValueError(
                "include_change_manifest must match the declared "
                f"{CHANGE_MANIFEST_ARTIFACT} artifact"
            )
        if self.include_import_view != (IMPORT_VIEW_ARTIFACT in declared):
            raise ValueError(
                f"include_import_view must match the declared {IMPORT_VIEW_ARTIFACT} artifact"
            )
        if (self.aggregate is not None) != (AGGREGATE_ARTIFACT in declared):
            raise ValueError(
                f"aggregate must match the declared {AGGREGATE_ARTIFACT} artifact"
            )
        return self

    @property
    def allowed_sheet_names(self) -> set[str]:
        return {
            self.primary_artifact.name,
            *(artifact.name for artifact in self.secondary_artifacts),
        }

    @property
    def required_sheet_names(self) -> set[str]:
        """Declared artifacts that must actually be written."""

        return {
            self.primary_artifact.name,
            *(
                artifact.name
                for artifact in self.secondary_artifacts
                if not artifact.optional
            ),
        }


def build_output_spec(
    task_spec: TaskSpec | dict[str, Any] | None,
    *,
    include_audit: bool = False,
    report_formats: list[str] | None = None,
    formula_source_tables: list[str] | None = None,
    primary_fields: list[str] | None = None,
    primary_row_count: int | None = None,
    source_artifact_fields: dict[str, list[str]] | None = None,
) -> OutputSpec:
    """Translate validated task intent into the smallest delivery contract.

    ``formula_source_tables`` are the lookup tables a formula-based fill points at.
    They are declared here rather than appearing at write time because a VLOOKUP whose
    source is missing is a broken cell — shipping the source is part of the request,
    and the user reviewing the plan should see it coming.
    """

    task = _task_spec(task_spec)
    objective = task.objective if task else ""
    output_intent = task.output_intent if task else {}
    wants_review = bool(output_intent.get("needs_review"))
    wants_charts = bool(output_intent.get("needs_charts"))
    # The import projection is opt-in from the goal ("输出可导入系统的数据"), signalled
    # by the system_import_view business view, and only materialises when the inputs
    # actually declare import requirements.
    wants_import_view = "system_import_view" in set(output_intent.get("business_views") or [])
    # When the primary table is already the template's shape, the import projection is
    # the same table twice. Uploading a form to fill mentions 导入/模板, which is exactly
    # what turns that view on — so the request for one sheet produced two.
    if task and task.target_schema:
        wants_import_view = False
    report_enabled = bool(output_intent.get("needs_report")) or _mentions_report(
        objective
    )
    formats = [
        item
        for item in (report_formats or ["markdown", "html"])
        if item in {"markdown", "html"}
    ]
    secondary: list[TableArtifactSpec] = []
    resolved_primary_fields = list(primary_fields or _task_primary_fields(task))
    if wants_review:
        review_fields = list(
            dict.fromkeys(
                [
                    "处理状态",
                    "问题类型",
                    "建议处理",
                    "源行号",
                    "影响字段",
                    "当前值",
                    *resolved_primary_fields,
                    *(
                        field
                        for fields in (source_artifact_fields or {}).values()
                        for field in fields
                    ),
                ]
            )
        )
        secondary.append(
            TableArtifactSpec(
                name="问题说明",
                fields=review_fields,
                # Review rows can retain any source field needed to identify the
                # record, while the declared/source union still prevents new fields
                # from appearing at export time.
                field_policy="source_dynamic",
                row_scope="review_rows",
                description="用户明确要求查看的异常与复核信息",
            )
        )
    if wants_import_view:
        secondary.append(
            TableArtifactSpec(
                name=IMPORT_VIEW_ARTIFACT,
                fields=list(task.target_schema if task else []),
                field_policy=("exact" if task and task.target_schema else "source_dynamic"),
                row_scope="task_result_rows",
                description="按导入模板字段投影的可导入数据",
            )
        )
    # Only when asked. Deriving this from the action list meant every cleaning job
    # carried a 本次改动 sheet the user never requested — and an unrequested sheet is
    # not a free bonus, it is one more thing between the reader and their answer.
    mutates = bool(output_intent.get("needs_change_manifest"))
    if mutates:
        secondary.append(
            TableArtifactSpec(
                name=CHANGE_MANIFEST_ARTIFACT,
                fields=["改动类型", "字段", "影响行数", "说明"],
                row_scope="change_rows",
                description="本次对原始数据做了哪些改动",
                optional=True,
            )
        )
    for table_name in formula_source_tables or []:
        name = str(table_name).strip()
        if name and name not in {item.name for item in secondary}:
            secondary.append(
                TableArtifactSpec(
                    name=name,
                    fields=[
                        str(field)
                        for field in (source_artifact_fields or {}).get(name, [])
                    ],
                    field_policy=(
                        "exact"
                        if (source_artifact_fields or {}).get(name)
                        else "source_dynamic"
                    ),
                    row_scope="source_rows",
                    description=f"{name}：填充公式引用的数据源",
                )
            )
    aggregate = _aggregate_spec(task)
    if aggregate is not None:
        secondary.append(
            TableArtifactSpec(
                name=AGGREGATE_ARTIFACT,
                fields=_aggregate_fields(aggregate),
                field_policy=("prefix" if aggregate.column_field else "exact"),
                row_scope="summary_rows",
                description=aggregate.description,
            )
        )
    return OutputSpec(
        primary_artifact=TableArtifactSpec(
            name="处理结果",
            fields=resolved_primary_fields,
            row_scope="task_result_rows",
            exact_rows=primary_row_count,
            description="与用户目标直接对应的最终数据",
        ),
        secondary_artifacts=secondary,
        aggregate=aggregate,
        charts=_chart_specs(task) if wants_charts else [],
        report=ReportOutputSpec(
            enabled=report_enabled,
            detail_level="brief",
            formats=formats if report_enabled else [],
        ),
        include_review_view=wants_review,
        include_change_manifest=mutates,
        include_import_view=wants_import_view,
        include_audit=include_audit,
    )


def _task_primary_fields(task: TaskSpec | None) -> list[str]:
    if task is None:
        return []
    if task.target_schema:
        return list(task.target_schema)
    fields = [
        reference.field
        for reference in task.target_fields
        if reference.table == task.primary_entity
    ]
    for reference in task.target_fields:
        if reference.field not in fields and reference.field in task.objective:
            fields.append(reference.field)
    for derivation in task.derivations:
        if derivation.get("kind") == "split":
            fields.extend(
                field for field in derivation.get("outputs") or [] if field not in fields
            )
        else:
            output = str(derivation.get("output") or "")
            if output and output not in fields:
                fields.append(output)
    return fields


def _aggregate_fields(spec: AggregateArtifactSpec) -> list[str]:
    if spec.column_field:
        # Crosstab columns are value-driven; the fixed prefix remains exact while the
        # generated columns are bounded by the declared column_field contract.
        return list(spec.group_by)
    fields = list(spec.group_by)
    bucket_output = str(spec.time_bucket.get("output_name") or "")
    if bucket_output and bucket_output not in fields:
        fields.append(bucket_output)
    fields.extend(
        str(metric.get("output_name") or metric.get("agg") or "")
        for metric in spec.metrics
        if str(metric.get("output_name") or metric.get("agg") or "") not in fields
    )
    return fields


def _aggregate_spec(task: TaskSpec | None) -> AggregateArtifactSpec | None:
    """Declare the summary the task asked for, grouped or not.

    A goal can ask for a single number — "统计所有订单的金额总和" names a measure and no
    dimension. Requiring a group_by here meant that request produced no summary sheet
    at all and the user got the detail rows back with their question unanswered.
    """

    aggregation = task.aggregation if task else {}
    group_by = [str(field) for field in (aggregation.get("group_by") or []) if str(field)]
    metrics = list(aggregation.get("metrics") or [])
    if not group_by and not metrics:
        return None
    return AggregateArtifactSpec(
        name=AGGREGATE_ARTIFACT,
        group_by=group_by,
        metrics=metrics,
        time_bucket={
            str(key): str(value)
            for key, value in (aggregation.get("time_bucket") or {}).items()
        },
        column_field=str(aggregation.get("column_field") or ""),
        top_n=int(aggregation.get("top_n") or 0),
        rank_within=[str(field) for field in (aggregation.get("rank_within") or [])],
        description=(
            "按 " + "、".join(group_by) + " 汇总的统计结果"
            if group_by
            else "全部记录的合计结果"
        ),
    )


def _task_spec(value: TaskSpec | dict[str, Any] | None) -> TaskSpec | None:
    if value is None:
        return None
    return value if isinstance(value, TaskSpec) else TaskSpec.model_validate(value)


def _mentions_report(goal: str) -> bool:
    lowered = goal.lower()
    return any(
        token in lowered or token in goal
        for token in ("报告", "业务结论", "结论说明", "report")
    )


def _chart_specs(task: TaskSpec) -> list[ChartOutputSpec]:
    goal = task.objective
    lowered = goal.lower()
    semantic_type = str(task.output_intent.get("chart_type") or "")
    semantic_defaults: dict[str, tuple[str, str, str]] = {
        "line": ("time_trend", "周期", "记录数"),
        "bar": ("top_business_dimension", "业务维度", "记录数"),
        "pie": ("record_status_distribution", "处理状态", "记录数"),
    }
    if semantic_type in semantic_defaults:
        chart_id, dimension, metric = semantic_defaults[semantic_type]
        return [
            ChartOutputSpec(
                chart_id=chart_id,
                business_question=goal,
                dimension=dimension,
                metric=metric,
                chart_type=semantic_type,
            )
        ]
    matches: list[tuple[tuple[str, ...], str, str, str, ChartType]] = [
        (("趋势图", "折线图", "trend", "line chart"), "time_trend", "周期", "记录数", "line"),
        (("柱状图", "bar chart"), "top_business_dimension", "业务维度", "记录数", "bar"),
        (("饼图", "pie chart"), "record_status_distribution", "处理状态", "记录数", "pie"),
        (("散点图", "scatter"), "metric_scatter", "数值字段 X", "数值字段 Y", "scatter"),
        (("热力图", "heatmap"), "exception_heatmap", "业务维度", "异常类型", "heatmap"),
        (("缺失率",), "missing_rate", "字段", "缺失率", "bar"),
        (("匹配率",), "match_rate", "匹配关系", "匹配率", "bar"),
    ]
    specs: list[ChartOutputSpec] = []
    for tokens, chart_id, dimension, metric, chart_type in matches:
        if any(token in lowered or token in goal for token in tokens):
            specs.append(
                ChartOutputSpec(
                    chart_id=chart_id,
                    business_question=goal,
                    dimension=dimension,
                    metric=metric,
                    chart_type=chart_type,
                )
            )
    if not specs:
        chart_id = (
            "exception_distribution"
            if any(token in goal for token in ("异常", "问题", "复核"))
            else "record_status_distribution"
        )
        specs.append(
            ChartOutputSpec(
                chart_id=chart_id,
                business_question=goal,
                dimension="异常类型" if chart_id == "exception_distribution" else "处理状态",
                metric="影响行数" if chart_id == "exception_distribution" else "记录数",
                chart_type="bar" if chart_id == "exception_distribution" else "pie",
            )
        )
    return specs
