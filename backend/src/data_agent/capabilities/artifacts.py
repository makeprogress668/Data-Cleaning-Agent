"""Plan the exact business-column contract before deterministic execution."""

from __future__ import annotations

from typing import Any

import pandas as pd

from data_agent.schemas.job import JobConfig
from data_agent.tools.delivery_view import is_technical_column


def planned_primary_fields(
    job_config: JobConfig | dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> list[str]:
    """Return delivered primary columns in their planned order."""

    job = (
        job_config
        if isinstance(job_config, JobConfig)
        else JobConfig.model_validate(job_config)
    )
    if job.target_schema:
        return list(job.target_schema)
    base_table = job.base_table or ("main" if job.main_source is not None else "")
    if base_table not in tables and tables:
        base_table = next(iter(tables))
    source_fields = [
        str(column)
        for column in tables.get(base_table, pd.DataFrame()).columns
        if not is_technical_column(str(column))
    ]
    if job.melt and job.melt.table == base_table:
        value_fields = set(job.melt.value_columns)
        first_value_index = next(
            (
                index
                for index, field in enumerate(source_fields)
                if field in value_fields
            ),
            len(source_fields),
        )
        source_fields = [field for field in source_fields if field not in value_fields]
        source_fields[first_value_index:first_value_index] = [
            job.melt.variable_name,
            job.melt.value_name,
        ]
    fields = [job.field_mapping.rename.get(field, field) for field in source_fields]

    lookups = [*([job.lookup] if job.lookup else []), *job.lookups]
    for lookup in lookups:
        existing = set(fields)
        for field in lookup.fields:
            output = str(lookup.field_aliases.get(field, field))
            if output in existing:
                output = f"{output}{lookup.suffix}"
            if output not in fields:
                fields.append(output)
                existing.add(output)
        for technical in (
            lookup.match_field,
            lookup.confidence_field,
            lookup.explanation_field,
            lookup.ambiguity_field,
        ):
            if technical and not is_technical_column(technical) and technical not in fields:
                fields.append(technical)

    for rollup in job.rollups:
        if rollup.output not in fields:
            fields.append(rollup.output)
    for formula in job.formulas:
        if (
            formula.op not in {"filter_rows", "dedupe"}
            and formula.output
            and not is_technical_column(formula.output)
            and formula.output not in fields
        ):
            fields.append(formula.output)
    return fields
