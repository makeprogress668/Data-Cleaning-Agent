from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OutputFileRef(BaseModel):
    name: str
    path: str
    description: str = ""


class ChartRef(BaseModel):
    title: str
    path: str
    chart_type: str = "html"
    description: str = ""


class BusinessAnswer(BaseModel):
    goal: str
    input_summary: list[dict[str, Any]] = Field(default_factory=list)
    result_summary: str
    key_metrics: list[dict[str, Any]] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    data_quality_score: int = 0
    usable_records_count: int = 0
    exception_records_count: int = 0
    critical_issue_count: int = 0
    analysis_results: list[dict[str, Any]] = Field(default_factory=list)
    charts: list[ChartRef] = Field(default_factory=list)
    exception_impact: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    review_tasks: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    output_files: list[OutputFileRef] = Field(default_factory=list)
    audit_refs: list[OutputFileRef] = Field(default_factory=list)
