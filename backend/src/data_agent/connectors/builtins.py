"""Safe built-in connectors used for job chaining and API-native tables."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from data_agent.api.artifact_service import materialize_named_artifact
from data_agent.api.job_store import read_job_status
from data_agent.connectors.base import ConnectorContext, ConnectorDescriptor


class InlineTableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = "input.csv"
    records: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)

    @field_validator("file_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if Path(value).name != value or not value.lower().endswith(".csv"):
            raise ValueError("inline_table file_name 必须是安全的 .csv 文件名")
        return value


class InlineTableConnector:
    descriptor = ConnectorDescriptor(
        connector_id="inline_table",
        version="1.0.0",
        name="内联数据表",
        description="把 API JSON records 物化为 CSV，适合系统间小批量调用。",
        source_kind="records",
        produces=[".csv"],
    )
    config_model = InlineTableConfig

    def materialize(
        self,
        config: BaseModel,
        destination: Path,
        context: ConnectorContext,
    ) -> list[Path]:
        parsed = InlineTableConfig.model_validate(config)
        fields: list[str] = []
        for record in parsed.records:
            for field in record:
                if field not in fields:
                    fields.append(field)
        if not fields:
            raise ValueError("inline_table records 必须至少包含一个字段")
        path = destination / parsed.file_name
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(parsed.records)
        return [path]


class JobArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    file_name: str = Field(min_length=1)
    output_name: str | None = None

    @field_validator("file_name", "output_name")
    @classmethod
    def validate_names(cls, value: str | None) -> str | None:
        if value is not None and Path(value).name != value:
            raise ValueError("artifact file name cannot contain a path")
        return value


class JobArtifactConnector:
    descriptor = ConnectorDescriptor(
        connector_id="job_artifact",
        version="1.0.0",
        name="任务产物",
        description="把同租户历史任务的工作簿作为新任务输入。",
        source_kind="job_artifact",
        produces=[".xlsx", ".csv", ".json"],
    )
    config_model = JobArtifactConfig

    def materialize(
        self,
        config: BaseModel,
        destination: Path,
        context: ConnectorContext,
    ) -> list[Path]:
        parsed = JobArtifactConfig.model_validate(config)
        payload = read_job_status(parsed.source_job_id)
        source = materialize_named_artifact(
            parsed.source_job_id,
            parsed.file_name,
            payload=payload,
        )
        if source is None or not source.exists():
            raise ValueError("历史任务产物不存在或已过期")
        output_name = parsed.output_name or source.name
        if Path(output_name).suffix.lower() != source.suffix.lower():
            raise ValueError("output_name 不能改变历史产物的文件类型")
        target = destination / output_name
        shutil.copy2(source, target)
        return [target]


def builtin_connectors() -> list[InlineTableConnector | JobArtifactConnector]:
    return [InlineTableConnector(), JobArtifactConnector()]
