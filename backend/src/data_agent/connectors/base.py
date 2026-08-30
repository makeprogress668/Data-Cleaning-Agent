"""Public connector SDK contracts.

Third-party connectors are trusted installed Python packages. The HTTP API accepts
only connector ids and validated configuration; it never accepts executable code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ConnectorDescriptor(BaseModel):
    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    source_kind: str = Field(min_length=1)
    produces: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class ConnectorContext:
    tenant_id: str
    job_id: str


class Connector(Protocol):
    descriptor: ConnectorDescriptor
    config_model: type[BaseModel]

    def materialize(
        self,
        config: BaseModel,
        destination: Path,
        context: ConnectorContext,
    ) -> list[Path]: ...


def public_descriptor(connector: Connector) -> dict[str, Any]:
    return {
        **connector.descriptor.model_dump(mode="json"),
        "config_schema": connector.config_model.model_json_schema(),
    }
