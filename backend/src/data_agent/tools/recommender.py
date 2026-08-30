from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.planning.goal_interpreter import (
    critical_fields_from_goal,
    derive_capabilities,
    suggest_base_table,
)
from data_agent.schemas.job import JobConfig
from data_agent.tools.conditions import evaluate_condition
from data_agent.tools.formulas import apply_formulas
from data_agent.tools.lookup import vlookup
from data_agent.utils.field_names import looks_like_key

logger = logging.getLogger(__name__)


def build_recommendation_package(
    tables: dict[str, pd.DataFrame],
    profile_sheets: dict[str, pd.DataFrame],
    output_job_path: str | Path = "configs/recommended_cleaning_job.json",
    goal: str = "",
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    logger.info("开始构建推荐方案，输入表数量=%s", len(tables))
    capabilities = derive_capabilities(goal, tables)
    base_table = infer_base_table(profile_sheets["table_profile"], tables=tables, goal=goal)
    # Cross-table lookups only when the goal is about matching/merging.
    recommended_lookups = (
        recommend_lookups(base_table, tables, profile_sheets)
        if capabilities["needs_lookup"]
        else []
    )
    matching_diagnostics = build_matching_diagnostics(
        base_table,
        profile_sheets["relationship_candidates"],
        recommended_lookups,
    )
    available_columns = infer_available_columns_after_lookups(
        tables[base_table],
        recommended_lookups,
    )
    # Derived formulas are opt-in: only generate them when the goal actually asks
    # for a derived/computed field. A plain clean goal must not spawn record_key.
    formulas = recommend_formulas(available_columns, capabilities=capabilities)
    # Only fields the goal cares about (or key-like fields) drive error-severity
    # blank rules; everything else is downgraded to warn so incidental nulls in
    # irrelevant columns never push otherwise-clean rows into review.
    critical_fields = critical_fields_from_goal(goal, tables)
    anomaly_rules, label_taxonomy = recommend_anomaly_rules(
        base_table,
        profile_sheets,
        recommended_lookups,
        critical_fields=critical_fields,
    )
    job_config = build_recommended_job_config(
        base_table=base_table,
        profile_sheets=profile_sheets,
        recommended_lookups=recommended_lookups,
        formulas=formulas,
        anomaly_rules=anomaly_rules,
        output_file="data/output/recommended_cleaning_result.xlsx",
        capabilities=capabilities,
    )
    processing_steps = build_processing_steps(
        base_table=base_table,
        recommended_lookups=recommended_lookups,
        formulas=formulas,
        anomaly_rules=anomaly_rules,
        output_job_path=output_job_path,
    )
    field_cleaning_plan = build_field_cleaning_plan(
        base_table=base_table,
        profile_sheets=profile_sheets,
        recommended_lookups=recommended_lookups,
        formulas=formulas,
        anomaly_rules=anomaly_rules,
    )
    impact_sheets = build_impact_preview_sheets(tables, job_config)
    logger.info(
        "推荐方案生成完成：base_table=%s, lookups=%s, formulas=%s, anomaly_rules=%s",
        base_table,
        len(recommended_lookups),
        len(formulas),
        len(anomaly_rules),
    )

    summary = pd.DataFrame(
        [
            {
                "item": "base_table",
                "recommendation": base_table,
                "reason": (
                    "优先匹配用户目标提到的表名/字段；未命中时选择 "
                    "raw/main/detail 命名或行数最多的业务明细表"
                ),
            },
            {
                "item": "lookup_count",
                "recommendation": len(recommended_lookups),
                "reason": "从跨表关系候选中筛选出的 VLOOKUP/mapping 关系",
            },
            {
                "item": "formula_count",
                "recommendation": len(formulas),
                "reason": "可自动生成的派生字段或字段标准化规则",
            },
            {
                "item": "anomaly_rule_count",
                "recommendation": len(anomaly_rules),
                "reason": "建议用于异常打标的规则数量",
            },
            {
                "item": "processing_step_count",
                "recommendation": len(processing_steps),
                "reason": "从画像、匹配、转换、打标到导出的推荐执行步骤数",
            },
            {
                "item": "label_count",
                "recommendation": len(label_taxonomy),
                "reason": "建议输出给业务用户确认的异常标签数量",
            },
            {
                "item": "generated_job_config",
                "recommendation": str(output_job_path),
                "reason": "可直接审阅和执行的清洗配置草案",
            },
        ]
    )

    sheets = {
        "recommendation_summary": summary,
        "matching_diagnostics": matching_diagnostics,
        "recommended_processing_steps": processing_steps,
        "field_cleaning_plan": field_cleaning_plan,
        "recommended_lookups": pd.DataFrame(recommended_lookups),
        "recommended_formulas": pd.DataFrame(formulas),
        "recommended_anomaly_rules": pd.DataFrame(anomaly_rules),
        "label_taxonomy": pd.DataFrame(label_taxonomy),
        **impact_sheets,
    }

    return sheets, job_config


def infer_base_table(
    table_profile: pd.DataFrame,
    tables: dict[str, pd.DataFrame] | None = None,
    goal: str = "",
) -> str:
    rows = table_profile.to_dict(orient="records")
    preferred_tokens = ("raw", "main", "detail", "source", "明细", "原始")
    goal_base_table = suggest_base_table(goal, tables or {}) if goal else None

    scored = []
    for row in rows:
        table = str(row["table"])
        row_count = int(row["row_count"])
        name_score = 100 if any(token in table.lower() for token in preferred_tokens) else 0
        goal_score = 1000 if goal_base_table == table else 0
        # Empty tables (e.g. import templates with only a header) are never valid
        # cleaning targets; push them below any table that actually has rows.
        empty_penalty = -100000 if row_count == 0 else 0
        scored.append((goal_score + name_score + empty_penalty, row_count, table))

    selected = sorted(scored, reverse=True)[0][2]
    logger.info("主表推断结果：selected=%s, candidates=%s", selected, scored)
    return selected


def recommend_lookups(
    base_table: str,
    tables: dict[str, pd.DataFrame],
    profile_sheets: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    relationships = profile_sheets["relationship_candidates"]
    if relationships.empty:
        logger.info("未发现跨表关系候选，跳过 lookup 推荐")
        return []

    rows = []
    logger.info(
        "开始筛选 lookup 推荐：base_table=%s, relationship_candidates=%s",
        base_table,
        len(relationships),
    )

    for row in relationships.to_dict(orient="records"):
        relation_name = (
            f"{row['left_table']}.{row['left_field']} -> "
            f"{row['right_table']}.{row['right_field']}"
        )
        if row["left_table"] != base_table:
            logger.debug("跳过关系 %s：左表不是主表 %s", relation_name, base_table)
            continue
        if not _is_lookup_recommendable(row):
            logger.debug(
                "跳过关系 %s：recommendation=%s, match_rate=%s",
                relation_name,
                row["recommendation"],
                row["left_match_rate"],
            )
            continue

        right_table = row["right_table"]
        right_field = row["right_field"]
        fields_to_bring = [
            column
            for column in tables[right_table].columns
            if column != right_field and column not in tables[base_table].columns
        ]
        if not fields_to_bring:
            logger.debug("跳过关系 %s：右表没有可带回的新字段", relation_name)
            continue

        name = f"{right_table}_by_{row['left_field']}"
        duplicate_strategy = _recommend_duplicate_strategy(
            row,
            tables[right_table],
            fields_to_bring,
        )
        logger.info(
            "推荐 lookup：%s, fields=%s, match_rate=%s, duplicate_strategy=%s, "
            "join_will_expand=%s",
            relation_name,
            fields_to_bring,
            row["left_match_rate"],
            duplicate_strategy,
            row.get("join_will_expand", False),
        )
        rows.append(
            {
                "name": name,
                "source_table": right_table,
                "left_key": row["left_field"],
                "right_key": right_field,
                "fields": fields_to_bring,
                "match_field": f"_lookup_matched_{name}",
                "confidence_field": f"_lookup_confidence_{name}",
                "explanation_field": f"_lookup_explanation_{name}",
                "ambiguity_field": f"_lookup_ambiguous_{name}",
                "match_mode": row.get("match_mode", "normalized_exact"),
                "duplicate_strategy": duplicate_strategy,
                "aggregate_sep": ", ",
                "fuzzy_threshold": 0.85,
                "match_rate": row["left_match_rate"],
                "unmatched_left_count": row["unmatched_left_count"],
                "unmatched_left_sample": row["unmatched_left_sample"],
                "right_duplicate_key_rows": row.get("right_duplicate_key_rows", 0),
                "right_duplicate_key_sample": row.get("right_duplicate_key_sample", ""),
                "estimated_join_row_count": row.get("estimated_join_row_count", ""),
                "join_will_expand": row.get("join_will_expand", False),
                "relationship_type": row["relationship_type"],
                "recommendation": row["recommendation"],
                "duplicate_strategy_reason": _duplicate_strategy_reason(
                    duplicate_strategy,
                    row,
                ),
                "suggested_action": _lookup_action(duplicate_strategy),
            }
        )

    return rows


def build_matching_diagnostics(
    base_table: str,
    relationships: pd.DataFrame,
    recommended_lookups: list[dict[str, Any]],
) -> pd.DataFrame:
    recommended_names = {
        (lookup["source_table"], lookup["left_key"], lookup["right_key"])
        for lookup in recommended_lookups
    }
    rows = []
    if relationships.empty:
        return pd.DataFrame(
            [
                {
                    "left_table": base_table,
                    "decision": "no_relationship_candidates",
                    "reason": "没有发现同名或规范化同名的跨表字段",
                }
            ]
        )

    for row in relationships.to_dict(orient="records"):
        key = (row["right_table"], row["left_field"], row["right_field"])
        decision = "recommended" if key in recommended_names else "skipped"
        reason = "已推荐为 lookup"
        if decision == "skipped":
            reason = _skip_reason(base_table, row)
        rows.append(
            {
                "left_table": row["left_table"],
                "left_field": row["left_field"],
                "right_table": row["right_table"],
                "right_field": row["right_field"],
                "decision": decision,
                "reason": reason,
                "match_rate": row.get("left_match_rate", ""),
                "unmatched_left_count": row.get("unmatched_left_count", ""),
                "right_duplicate_key_rows": row.get("right_duplicate_key_rows", ""),
                "join_will_expand": row.get("join_will_expand", ""),
                "relationship_type": row.get("relationship_type", ""),
                "recommendation": row.get("recommendation", ""),
            }
        )

    return pd.DataFrame(rows)


def infer_available_columns_after_lookups(
    base_df: pd.DataFrame,
    recommended_lookups: list[dict[str, Any]],
) -> list[str]:
    columns = list(base_df.columns)
    for lookup in recommended_lookups:
        for field in lookup["fields"]:
            if field not in columns:
                columns.append(field)
    return columns


def recommend_formulas(
    available_columns: list[str],
    capabilities: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    # Do not fabricate derived columns the user never asked for. The old behaviour
    # auto-created a `record_key` concat whenever two key-like columns existed,
    # which violated the zero-redundancy principle. Derived formulas now come only
    # from an explicit goal (handled by the LLM planner) — the deterministic
    # recommender stays hands-off here.
    return []


def recommend_anomaly_rules(
    base_table: str,
    profile_sheets: dict[str, pd.DataFrame],
    recommended_lookups: list[dict[str, Any]],
    critical_fields: list[str] | None = None,
    blank_rule_min_null_rate: float = 0.05,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rules = []
    taxonomy = []
    critical = {str(field) for field in (critical_fields or [])}
    base_row_count = _base_row_count(profile_sheets.get("table_profile"), base_table)

    base_issues = profile_sheets["profile_issues"]
    if not base_issues.empty:
        base_issues = base_issues[base_issues["table"].eq(base_table)]
        for issue in base_issues.to_dict(orient="records"):
            field = issue["field"]
            is_critical = field in critical
            null_rate = _safe_rate(int(issue.get("count", 0)), base_row_count)
            # Skip incidental nulls in non-critical columns: a couple of blank cells
            # in a field the goal never mentions must not flag the whole row.
            if not is_critical and null_rate < blank_rule_min_null_rate:
                continue
            severity = "error" if is_critical else "warn"
            label = f"{field}_{issue['issue_type']}"
            rules.append(
                {
                    "name": label,
                    "condition": {"field": field, "op": "is_blank"},
                    "severity": severity,
                    "reason": label,
                    "source": "profile_issues",
                }
            )
            taxonomy.append(
                _taxonomy_row(label, severity, f"{base_table}.{field} 存在空值或空白值")
            )

    for lookup in recommended_lookups:
        label = f"{lookup['name']}_not_found"
        rules.append(
            {
                "name": label,
                "condition": {"field": lookup["match_field"], "op": "lookup_missing"},
                "severity": "error",
                "reason": label,
                "source": "lookup_recommendation",
            }
        )
        taxonomy.append(
            _taxonomy_row(
                label,
                "error",
                (
                    f"{lookup['left_key']} 未能在 "
                    f"{lookup['source_table']}.{lookup['right_key']} 中匹配"
                ),
            )
        )

    key_candidates = profile_sheets["key_candidates"]
    if not key_candidates.empty:
        duplicates = key_candidates[
            key_candidates["table"].eq(base_table)
            & key_candidates["duplicate_value_count"].gt(0)
        ]
        for row in duplicates.to_dict(orient="records"):
            label = f"duplicate_{row['field']}"
            rules.append(
                {
                    "name": label,
                    "condition": {"field": row["field"], "op": "duplicated"},
                    "severity": "warn",
                    "reason": label,
                    "source": "key_candidates",
                }
            )
            taxonomy.append(
                _taxonomy_row(label, "warn", f"{base_table}.{row['field']} 存在重复值")
            )

    return rules, taxonomy


def build_processing_steps(
    base_table: str,
    recommended_lookups: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    anomaly_rules: list[dict[str, Any]],
    output_job_path: str | Path,
) -> pd.DataFrame:
    rows = []

    def add_step(step_type: str, target: str, action: str, reason: str) -> None:
        rows.append(
            {
                "step": len(rows) + 1,
                "step_type": step_type,
                "target": target,
                "recommended_action": action,
                "reason": reason,
            }
        )

    add_step(
        "profile",
        base_table,
        "将该表作为清洗主表，保留原始字段并继续做匹配、转换和打标",
        "优先选择 raw/main/detail 命名或行数最多的业务明细表",
    )
    for lookup in recommended_lookups:
        add_step(
            "lookup_mapping",
            f"{base_table}.{lookup['left_key']} -> "
            f"{lookup['source_table']}.{lookup['right_key']}",
            (
                f"带回字段: {', '.join(lookup['fields'])}; "
                f"匹配模式: {lookup['match_mode']}; "
                f"右表重复 key 策略: {lookup['duplicate_strategy']}"
            ),
            (
                f"匹配率 {lookup['match_rate']}，"
                f"未匹配 {lookup['unmatched_left_count']} 行，"
                f"join 是否膨胀: {lookup['join_will_expand']}"
            ),
        )
    for formula in formulas:
        add_step(
            "transform",
            formula["output"],
            _formula_action(formula),
            formula.get("reason", "生成清洗后的派生字段"),
        )
    for rule in anomaly_rules:
        condition = rule["condition"]
        add_step(
            "label",
            rule["name"],
            f"满足 {_condition_text(condition)} 时写入异常标签",
            rule.get("reason") or rule["name"],
        )
    add_step(
        "export",
        str(output_job_path),
        "生成推荐报告和可执行 JobConfig",
        "让用户可以先审阅方案，再运行清洗任务",
    )

    return pd.DataFrame(rows)


def build_field_cleaning_plan(
    base_table: str,
    profile_sheets: dict[str, pd.DataFrame],
    recommended_lookups: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    anomaly_rules: list[dict[str, Any]],
) -> pd.DataFrame:
    column_profile = profile_sheets["column_profile"]
    base_columns = column_profile[column_profile["table"].eq(base_table)]
    issue_rows = profile_sheets["profile_issues"]
    field_to_issues = _field_to_issues(base_table, issue_rows)
    field_to_rules = _field_to_rules(anomaly_rules)
    field_to_formulas = _field_to_formulas(formulas)
    field_to_lookups = _field_to_lookups(recommended_lookups)
    rows = []

    for column in base_columns.to_dict(orient="records"):
        field = column["name"]
        actions = []
        if field in field_to_lookups:
            actions.append("; ".join(field_to_lookups[field]))
        if field in field_to_formulas:
            actions.append("; ".join(field_to_formulas[field]))
        if field in field_to_rules:
            actions.append("; ".join(field_to_rules[field]))
        if not actions:
            actions.append("保留原始字段，暂不建议自动改写")

        rows.append(
            {
                "table": base_table,
                "field": field,
                "dtype": column["dtype"],
                "null_count": column["null_count"],
                "empty_string_count": column["empty_string_count"],
                "unique_count": column["unique_count"],
                "duplicate_value_count": column["duplicate_value_count"],
                "profile_issue": "; ".join(field_to_issues.get(field, [])),
                "recommended_action": "; ".join(actions),
            }
        )

    for lookup in recommended_lookups:
        for field in lookup["fields"]:
            rows.append(
                {
                    "table": lookup["source_table"],
                    "field": field,
                    "dtype": "",
                    "null_count": "",
                    "empty_string_count": "",
                    "unique_count": "",
                    "duplicate_value_count": "",
                    "profile_issue": "",
                    "recommended_action": (
                        f"通过 {base_table}.{lookup['left_key']} 匹配带回，"
                        f"匹配模式 {lookup['match_mode']}，"
                        f"右表重复 key 策略 {lookup['duplicate_strategy']}"
                    ),
                }
            )

    return pd.DataFrame(rows)


def build_recommended_job_config(
    base_table: str,
    profile_sheets: dict[str, pd.DataFrame],
    recommended_lookups: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    anomaly_rules: list[dict[str, Any]],
    output_file: str,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    sources = _sources_from_inventory(profile_sheets.get("source_inventory", pd.DataFrame()))
    lookups = [
        {
            "name": lookup["name"],
            "source_table": lookup["source_table"],
            "left_key": lookup["left_key"],
            "right_key": lookup["right_key"],
            "fields": lookup["fields"],
            "match_field": lookup["match_field"],
            "confidence_field": lookup.get("confidence_field"),
            "explanation_field": lookup.get("explanation_field"),
            "ambiguity_field": lookup.get("ambiguity_field"),
            "match_mode": lookup["match_mode"],
            "duplicate_strategy": lookup["duplicate_strategy"],
            "aggregate_sep": lookup["aggregate_sep"],
            "fuzzy_threshold": lookup["fuzzy_threshold"],
        }
        for lookup in recommended_lookups
    ]

    config: dict[str, Any] = {
        "name": "recommended-cleaning-job",
        "sources": sources,
        "base_table": base_table,
        "lookups": lookups,
        "formulas": [_strip_reason(formula) for formula in formulas],
        "anomaly_rules": [_strip_source(rule) for rule in anomaly_rules],
        "export": {
            "output_file": output_file,
            "include_source_tables": False,
            "include_internal_sheets": False,
        },
    }

    # A pivot is an analysis artifact; only add it when the goal wants analysis and
    # there are anomaly labels worth grouping. Otherwise the delivery stays lean.
    needs_analysis = bool(capabilities and capabilities.get("needs_analysis"))
    if needs_analysis and anomaly_rules:
        config["pivot"] = {
            "group_by": ["abnormal_type"],
            "metrics": [{"agg": "count", "output_name": "row_count"}],
        }

    return config


def build_impact_preview_sheets(
    tables: dict[str, pd.DataFrame],
    job_config: dict[str, Any],
    preview_limit: int = 20,
) -> dict[str, pd.DataFrame]:
    job = JobConfig.model_validate(job_config)
    working_df = _prepare_recommended_working_df(tables, job)
    abnormal_field = job.abnormal_field
    label_series = pd.Series("", index=working_df.index, dtype="object")
    rule_rows = []

    for rule in job.anomaly_rules:
        mask = evaluate_condition(working_df, rule.condition)
        label = rule.reason or rule.name
        label_series = _append_label(label_series, mask, label)
        rule_rows.append(
            _impact_row(
                rule_name=rule.name,
                rule_type="anomaly_rule",
                label=label,
                severity=rule.severity,
                condition=_condition_text(rule.condition.model_dump()),
                mask=mask,
                df=working_df,
            )
        )

    labeled_mask = label_series.astype(str).str.strip().ne("")
    preview_df = _record_label_preview(
        working_df,
        label_series,
        labeled_mask,
        abnormal_field,
        preview_limit,
    )
    label_impact = _label_impact_summary(rule_rows)
    approval_checklist = _approval_checklist(job, working_df, labeled_mask, rule_rows)

    return {
        "rule_impact_summary": _ensure_impact_columns(pd.DataFrame(rule_rows)),
        "label_impact_summary": label_impact,
        "record_label_preview": preview_df,
        "approval_checklist": approval_checklist,
    }


def _prepare_recommended_working_df(
    tables: dict[str, pd.DataFrame],
    job: JobConfig,
) -> pd.DataFrame:
    if not job.base_table:
        raise ValueError("Recommended job requires base_table")
    if job.base_table not in tables:
        raise KeyError(f"base_table not found in loaded tables: {job.base_table}")

    working_df = tables[job.base_table].copy()
    for index, lookup in enumerate(job.lookups):
        if not lookup.source_table or lookup.source_table not in tables:
            raise KeyError(f"lookup source_table not found: {lookup.source_table}")
        match_field = lookup.match_field or f"_lookup_matched_{lookup.name or index}"
        working_df = vlookup(
            working_df,
            tables[lookup.source_table],
            left_key=lookup.left_keys or lookup.left_key,
            right_key=lookup.right_keys or lookup.right_key,
            fields=lookup.fields,
            field_aliases=lookup.field_aliases,
            suffix=lookup.suffix,
            match_field=match_field,
            confidence_field=lookup.confidence_field,
            explanation_field=lookup.explanation_field,
            ambiguity_field=lookup.ambiguity_field,
            match_mode=lookup.match_mode,
            duplicate_strategy=lookup.duplicate_strategy,
            aggregate_sep=lookup.aggregate_sep,
            fuzzy_threshold=lookup.fuzzy_threshold,
        )

    return apply_formulas(working_df, job.formulas)


def _impact_row(
    rule_name: str,
    rule_type: str,
    label: str,
    severity: str,
    condition: str,
    mask: pd.Series,
    df: pd.DataFrame,
) -> dict[str, Any]:
    affected_count = int(mask.sum())
    return {
        "rule_name": rule_name,
        "rule_type": rule_type,
        "label": label,
        "severity": severity,
        "condition": condition,
        "affected_count": affected_count,
        "affected_rate": round(_safe_rate(affected_count, len(df)), 4),
        "sample_rows": ", ".join(str(index) for index in df.index[mask].tolist()[:10]),
        "sample_keys": _sample_key_values(df[mask]),
    }


def _append_label(label_series: pd.Series, mask: pd.Series, label: str) -> pd.Series:
    result = label_series.copy()
    current = result.loc[mask].fillna("").astype(str)
    blank = current.eq("")
    result.loc[current[blank].index] = label
    result.loc[current[~blank].index] = current[~blank] + ";" + label
    return result


def _record_label_preview(
    df: pd.DataFrame,
    label_series: pd.Series,
    labeled_mask: pd.Series,
    abnormal_field: str,
    preview_limit: int,
) -> pd.DataFrame:
    preview = df.loc[labeled_mask].head(preview_limit).copy()
    preview.insert(0, abnormal_field, label_series.loc[preview.index])
    preview.insert(0, "source_row_index", preview.index)
    preferred = ["source_row_index", abnormal_field, *_key_like_columns(preview.columns)]
    selected = [column for column in preferred if column in preview.columns]
    selected.extend(
        column
        for column in preview.columns
        if column.startswith("_lookup_matched_") and column not in selected
    )
    selected.extend(column for column in preview.columns if column not in selected)
    return preview[selected[:20]]


def _label_impact_summary(rule_rows: list[dict[str, Any]]) -> pd.DataFrame:
    label_rows = {}
    for row in rule_rows:
        label = row["label"]
        current = label_rows.setdefault(
            label,
            {
                "label": label,
                "severity": row["severity"],
                "rule_count": 0,
                "affected_count": 0,
                "sample_keys": "",
            },
        )
        current["rule_count"] += 1
        current["affected_count"] += int(row["affected_count"])
        if not current["sample_keys"]:
            current["sample_keys"] = row["sample_keys"]

    return pd.DataFrame(
        list(label_rows.values()),
        columns=["label", "severity", "rule_count", "affected_count", "sample_keys"],
    )


def _approval_checklist(
    job: JobConfig,
    df: pd.DataFrame,
    labeled_mask: pd.Series,
    rule_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    total = int(len(df))
    affected = int(labeled_mask.sum())
    return pd.DataFrame(
        [
            {
                "item": "base_table",
                "status": "ready",
                "detail": job.base_table,
            },
            {
                "item": "lookup_rules",
                "status": "ready" if job.lookups else "needs_review",
                "detail": f"{len(job.lookups)} 个 lookup 将被执行",
            },
            {
                "item": "label_rules",
                "status": "ready" if rule_rows else "needs_review",
                "detail": f"{len(rule_rows)} 条规则预计命中 {affected}/{total} 行",
            },
            {
                "item": "raw_traceability",
                "status": "ready" if job.export.include_source_tables else "needs_review",
                "detail": "导出源表快照" if job.export.include_source_tables else "未导出源表快照",
            },
            {
                "item": "execution",
                "status": "ready",
                "detail": "可执行 data-agent run 生成的 JobConfig",
            },
        ]
    )


def _ensure_impact_columns(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "rule_name",
        "rule_type",
        "label",
        "severity",
        "condition",
        "affected_count",
        "affected_rate",
        "sample_rows",
        "sample_keys",
    ]
    if df.empty:
        return pd.DataFrame(columns=columns)
    return df[columns]


def _sample_key_values(df: pd.DataFrame) -> str:
    key_columns = _key_like_columns(df.columns)
    if not key_columns:
        return ""
    values = []
    for row in df[key_columns].head(10).to_dict(orient="records"):
        values.append("|".join(f"{key}={value}" for key, value in row.items()))
    return "; ".join(values)


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _base_row_count(table_profile: pd.DataFrame | None, base_table: str) -> int:
    if table_profile is None or table_profile.empty or "table" not in table_profile.columns:
        return 0
    rows = table_profile[table_profile["table"].eq(base_table)]
    if rows.empty or "row_count" not in rows.columns:
        return 0
    return int(rows.iloc[0]["row_count"])


def write_recommended_job_config(path: str | Path, job_config: dict[str, Any]) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(job_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def _is_lookup_recommendable(row: dict[str, Any]) -> bool:
    recommendation = row["recommendation"]
    if recommendation in {"recommended_lookup", "possible_lookup_needs_review"}:
        return True
    if recommendation == "right_key_not_unique":
        return float(row.get("left_match_rate", 0)) >= 0.5
    return False


def _recommend_duplicate_strategy(
    relationship: dict[str, Any],
    right_df: pd.DataFrame,
    fields_to_bring: list[str],
) -> str:
    if int(relationship.get("right_duplicate_key_rows", 0)) == 0:
        return "first"
    if fields_to_bring and all(_is_numeric_like(right_df[field]) for field in fields_to_bring):
        return "aggregate"
    return "review"


def _is_numeric_like(series: pd.Series) -> bool:
    non_empty = series.dropna()
    if non_empty.empty:
        return False
    return pd.to_numeric(non_empty, errors="coerce").notna().all()


def _duplicate_strategy_reason(strategy: str, relationship: dict[str, Any]) -> str:
    duplicate_rows = int(relationship.get("right_duplicate_key_rows", 0))
    if duplicate_rows == 0:
        return "右表 key 唯一，按 first 策略执行等价于精确 VLOOKUP"
    if strategy == "aggregate":
        return "右表 key 重复且带回字段可数值化，建议聚合后再匹配，避免 join 行数膨胀"
    if strategy == "list":
        return "右表 key 重复且带回字段偏属性文本，建议合并为列表，保留冲突信息"
    if strategy == "review":
        return "右表 key 重复且带回字段偏属性文本，建议先取首条并标记歧义，进入人工复核"
    return "右表 key 重复，建议执行前人工处理或配置 error 策略阻断"


def _lookup_action(strategy: str) -> str:
    if strategy == "first":
        return "执行 VLOOKUP/mapping，并对未匹配记录打标"
    if strategy == "aggregate":
        return "先按右表 key 聚合，再执行 VLOOKUP/mapping，并对未匹配记录打标"
    if strategy == "list":
        return "先将右表重复 key 的属性合并为列表，再执行 VLOOKUP/mapping"
    if strategy == "review":
        return "右表重复 key 时继续执行首条匹配，并把歧义匹配放入复核"
    return "右表重复 key 时阻断执行，要求人工处理后再匹配"


def _skip_reason(base_table: str, row: dict[str, Any]) -> str:
    if row["left_table"] != base_table:
        return f"左表不是当前主表 {base_table}"
    if row["recommendation"] == "low_match_rate":
        return f"匹配率过低: {row.get('left_match_rate')}"
    if row["recommendation"] == "right_key_not_unique":
        return "右表 key 重复且匹配率不足，暂不自动推荐"
    return f"关系状态不满足自动推荐条件: {row['recommendation']}"


def _sources_from_inventory(source_inventory: pd.DataFrame) -> dict[str, dict[str, Any]]:
    sources = {}
    for row in source_inventory.to_dict(orient="records"):
        source = {"path": row["file"]}
        if row.get("sheet"):
            source["sheet"] = row["sheet"]
        sources[row["table"]] = source
    return sources


def _strip_reason(formula: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in formula.items() if key != "reason"}


def _strip_source(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in rule.items() if key != "source"}


def _taxonomy_row(label: str, severity: str, description: str) -> dict[str, str]:
    return {"label": label, "severity": severity, "description": description}


def _field_to_issues(base_table: str, issue_rows: pd.DataFrame) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if issue_rows.empty:
        return result
    for issue in issue_rows[issue_rows["table"].eq(base_table)].to_dict(orient="records"):
        result.setdefault(issue["field"], []).append(
            f"{issue['issue_type']}({issue['count']})"
        )
    return result


def _field_to_rules(anomaly_rules: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for rule in anomaly_rules:
        field = rule["condition"]["field"]
        result.setdefault(field, []).append(f"异常打标: {rule['name']}")
    return result


def _field_to_formulas(formulas: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for formula in formulas:
        fields = []
        if formula.get("source"):
            fields.append(formula["source"])
        fields.extend(formula.get("columns", []))
        if formula.get("condition"):
            fields.append(formula["condition"]["field"])
        for field in fields:
            result.setdefault(field, []).append(f"参与生成: {formula['output']}")
    return result


def _field_to_lookups(recommended_lookups: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for lookup in recommended_lookups:
        result.setdefault(lookup["left_key"], []).append(
            f"作为匹配键关联 {lookup['source_table']}.{lookup['right_key']}"
        )
    return result


def _formula_action(formula: dict[str, Any]) -> str:
    op = formula["op"]
    if formula.get("source"):
        return f"执行 {op}，来源字段: {formula['source']}"
    if formula.get("columns"):
        return f"执行 {op}，来源字段: {', '.join(formula['columns'])}"
    if formula.get("condition"):
        return f"执行 {op}，条件: {_condition_text(formula['condition'])}"
    return f"执行 {op}"


def _condition_text(condition: dict[str, Any]) -> str:
    field = condition["field"]
    op = condition["op"]
    if condition.get("values"):
        return f"{field} {op} {condition['values']}"
    if "value" in condition and condition["value"] is not None:
        return f"{field} {op} {condition['value']}"
    return f"{field} {op}"





def _find_field(columns, target: str) -> str | None:
    for column in columns:
        if str(column).lower() == target.lower():
            return str(column)
    return None


def _key_like_columns(columns) -> list[str]:
    result = []
    for column in columns:
        if looks_like_key(column):
            result.append(str(column))
        if len(result) >= 4:
            break
    return result
