import pytest

from data_agent.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    UnknownCapabilityError,
    get_capability_registry,
)
from data_agent.tools import export_workbook, vlookup


def test_builtin_registry_contains_prd_capabilities() -> None:
    registry = get_capability_registry()
    required = {
        "read_tables",
        "extract_document_rules",
        "profile_dataset",
        "detect_relationships",
        "trim",
        "normalize_case",
        "normalize_width",
        "map_values",
        "filter_rows",
        "deduplicate",
        "fill_missing",
        "join_tables",
        "lookup_fields",
        "derive_column",
        "project_schema",
        "write_formulas",
        "aggregate",
        "reshape",
        "rollup",
        "union_tables",
        "pivot",
        "validate_rules",
        "detect_anomalies",
        "summarize",
        "top_n",
        "trend_analysis",
        "generate_chart",
        "score_quality",
        "build_audit",
        "export_table",
        "export_report",
    }

    assert required <= {item["capability_id"] for item in registry.descriptors()}
    assert registry.resolve("lookup_fields").executor is vlookup
    assert registry.resolve("export_table").executor is export_workbook
    assert all(item["version"] == "1.0.0" for item in registry.descriptors())
    assert all(item["acceptance_rules"] for item in registry.descriptors())


def test_registry_is_the_single_formula_operator_binding() -> None:
    registry = get_capability_registry()

    assert registry.resolve_formula_operator("trim").capability_id == "trim"
    assert registry.resolve_formula_operator("lower").capability_id == "normalize_case"
    assert registry.resolve_formula_operator("dedupe").capability_id == "deduplicate"
    assert registry.resolve_formula_operator("literal").capability_id == "derive_column"


def test_registry_rejects_unknown_and_duplicate_capabilities() -> None:
    registry = CapabilityRegistry()
    capability = CapabilitySpec(
        capability_id="example",
        version="1.0.0",
        input_schema={"data": "DataFrame"},
        output_schema={"data": "DataFrame"},
        parameter_schema={},
        required_parameters=(),
        supported_engines=("pandas",),
        risk_level="low",
        requires_confirmation=False,
        field_effects=("none",),
        executor=lambda: None,
        acceptance_rules=("completes",),
    )
    registry.register(capability)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(capability)
    with pytest.raises(UnknownCapabilityError, match="unregistered capability"):
        registry.resolve("arbitrary_python")


def test_capability_requires_declared_parameters_and_acceptance_rules() -> None:
    with pytest.raises(ValueError, match="undeclared required parameters"):
        CapabilitySpec(
            capability_id="invalid",
            version="1",
            input_schema={},
            output_schema={},
            parameter_schema={},
            required_parameters=("column",),
            supported_engines=("pandas",),
            risk_level="low",
            requires_confirmation=False,
            field_effects=("none",),
            executor=lambda: None,
            acceptance_rules=("completes",),
        )

    with pytest.raises(ValueError, match="acceptance rules"):
        CapabilitySpec(
            capability_id="invalid_acceptance",
            version="1",
            input_schema={},
            output_schema={},
            parameter_schema={},
            required_parameters=(),
            supported_engines=("pandas",),
            risk_level="low",
            requires_confirmation=False,
            field_effects=("none",),
            executor=lambda: None,
        )


def test_registry_rejects_duplicate_formula_bindings_and_fallbacks() -> None:
    registry = CapabilityRegistry()

    def capability(
        capability_id: str,
        *,
        operators: tuple[str, ...] = (),
        fallback: bool = False,
    ) -> CapabilitySpec:
        return CapabilitySpec(
            capability_id=capability_id,
            version="1",
            input_schema={},
            output_schema={},
            parameter_schema={},
            required_parameters=(),
            supported_engines=("pandas",),
            risk_level="low",
            requires_confirmation=False,
            field_effects=("none",),
            executor=lambda: None,
            formula_operators=operators,
            formula_fallback=fallback,
            acceptance_rules=("completes",),
        )

    registry.register(capability("first", operators=("trim",), fallback=True))
    with pytest.raises(ValueError, match="operators already registered"):
        registry.register(capability("duplicate_operator", operators=("trim",)))
    with pytest.raises(ValueError, match="fallback already registered"):
        registry.register(capability("duplicate_fallback", fallback=True))
