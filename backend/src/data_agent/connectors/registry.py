"""Validated connector registry and installed-plugin discovery."""

from __future__ import annotations

import importlib.metadata
import threading
from pathlib import Path
from typing import Any

from data_agent.connectors.base import (
    Connector,
    ConnectorContext,
    public_descriptor,
)
from data_agent.tools.document_rules import DOCUMENT_EXTENSIONS
from data_agent.tools.excel_reader import SUPPORTED_EXTENSIONS

_ALLOWED_EXTENSIONS = SUPPORTED_EXTENSIONS | DOCUMENT_EXTENSIONS


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        identifier = connector.descriptor.connector_id
        if identifier in self._connectors:
            raise ValueError(f"duplicate connector id: {identifier}")
        self._connectors[identifier] = connector

    def descriptors(self) -> list[dict[str, Any]]:
        return [
            public_descriptor(item)
            for _identifier, item in sorted(self._connectors.items())
        ]

    def materialize(
        self,
        connector_id: str,
        raw_config: dict[str, Any],
        destination: Path,
        context: ConnectorContext,
    ) -> list[Path]:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ValueError(f"未知 Connector：{connector_id}")
        config = connector.config_model.model_validate(raw_config)
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        paths = connector.materialize(config, destination, context)
        if not paths:
            raise ValueError(f"Connector {connector_id} 没有返回数据文件。")
        validated: list[Path] = []
        for path in paths:
            resolved = Path(path).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError("Connector 产物越过了任务目录边界。") from exc
            if not resolved.is_file():
                raise ValueError("Connector 返回了不存在的数据文件。")
            if resolved.suffix.lower() not in _ALLOWED_EXTENSIONS:
                raise ValueError(f"Connector 返回了不支持的文件类型：{resolved.suffix}")
            validated.append(resolved)
        return validated


def load_installed_connectors(registry: ConnectorRegistry) -> None:
    """Load explicitly installed entry points from ``data_agent.connectors``."""

    entries = importlib.metadata.entry_points()
    selected = (
        entries.select(group="data_agent.connectors")
        if hasattr(entries, "select")
        else entries.get("data_agent.connectors", [])
    )
    for entry in selected:
        loaded = entry.load()
        connector = loaded() if isinstance(loaded, type) else loaded
        registry.register(connector)


_registry: ConnectorRegistry | None = None
_lock = threading.Lock()


def get_connector_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        with _lock:
            if _registry is None:
                from data_agent.connectors.builtins import builtin_connectors

                built = ConnectorRegistry()
                for connector in builtin_connectors():
                    built.register(connector)
                load_installed_connectors(built)
                _registry = built
    return _registry


def reset_connector_registry() -> None:
    global _registry
    with _lock:
        _registry = None
