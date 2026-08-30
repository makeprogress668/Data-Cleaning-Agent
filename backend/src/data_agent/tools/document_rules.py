from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union
from xml.etree import ElementTree

import pandas as pd

DOCUMENT_EXTENSIONS = {".txt", ".md", ".markdown", ".json", ".docx", ".pdf", ".doc"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


@dataclass(frozen=True)
class DocumentInsights:
    document_inventory: pd.DataFrame
    field_definitions: pd.DataFrame
    business_rules: pd.DataFrame
    import_requirements: pd.DataFrame
    template_validation: pd.DataFrame
    generated_rules: list[dict[str, Any]]


def extract_document_insights(
    input_paths: list[Union[str, Path]],
    tables: dict[str, pd.DataFrame] | None = None,
    source_inventory: pd.DataFrame | None = None,
    recursive: bool = True,
) -> DocumentInsights:
    """Extract deterministic rules from semi-structured business documents."""

    table_map = tables or {}
    known_fields = _known_fields(table_map)
    inventory_rows = []
    field_rows = []
    rule_rows = []
    import_rows = []

    for path in discover_document_files(input_paths, recursive=recursive):
        text, status = read_document_text(path)
        inventory_rows.append(
            {
                "file": str(path),
                "file_name": path.name,
                "file_type": path.suffix.lower().lstrip("."),
                "parse_status": status,
                "size_bytes": path.stat().st_size,
            }
        )
        if not text:
            continue

        table_insights = _extract_markdown_table_insights(path, text, known_fields)
        field_rows.extend(table_insights["field_definitions"])
        rule_rows.extend(table_insights["business_rules"])
        import_rows.extend(table_insights["import_requirements"])

        json_insights = _extract_json_insights(path, text, known_fields)
        field_rows.extend(json_insights["field_definitions"])
        rule_rows.extend(json_insights["business_rules"])
        import_rows.extend(json_insights["import_requirements"])

        lines = _content_lines(text)
        for line_no, line in enumerate(lines, start=1):
            field_rows.extend(_extract_field_definitions(path, line_no, line, known_fields))
            rule_rows.extend(_extract_rule_rows(path, line_no, line, known_fields))
            import_rows.extend(_extract_import_requirements(path, line_no, line))

    import_rows.extend(_extract_template_requirements(table_map, source_inventory))
    business_rules = _deduplicate_rules(pd.DataFrame(rule_rows))
    import_requirements = _deduplicate_import_requirements(pd.DataFrame(import_rows))
    template_validation = build_template_validation(
        import_requirements,
        _known_fields_for_template_validation(table_map, source_inventory),
    )
    generated_rules = _generated_anomaly_rules(business_rules)
    return DocumentInsights(
        document_inventory=pd.DataFrame(inventory_rows),
        field_definitions=_ensure_columns(
            pd.DataFrame(field_rows),
            ["source_file", "line_no", "field", "definition"],
        ),
        business_rules=_ensure_columns(
            business_rules,
            [
                "source_file",
                "line_no",
                "rule_type",
                "field",
                "op",
                "value",
                "values",
                "severity",
                "description",
                "generated_rule_name",
            ],
        ),
        import_requirements=_ensure_columns(
            import_requirements,
            ["source_file", "line_no", "requirement_type", "field", "description"],
        ),
        template_validation=template_validation,
        generated_rules=generated_rules,
    )


def build_template_validation(
    import_requirements: pd.DataFrame,
    available_fields: set[str],
) -> pd.DataFrame:
    columns = ["field", "status", "requirement_type", "source_file", "description"]
    if import_requirements.empty:
        return pd.DataFrame(columns=columns)
    available = {field.lower() for field in available_fields}
    rows = []
    for row in import_requirements.to_dict(orient="records"):
        field = str(row.get("field", ""))
        if not field:
            continue
        rows.append(
            {
                "field": field,
                "status": "present" if field.lower() in available else "missing",
                "requirement_type": row.get("requirement_type", ""),
                "source_file": row.get("source_file", ""),
                "description": row.get("description", ""),
            }
        )
    return pd.DataFrame(rows, columns=columns).drop_duplicates(ignore_index=True)


def discover_document_files(
    input_paths: list[Union[str, Path]],
    recursive: bool = True,
) -> list[Path]:
    files: list[Path] = []
    for raw_path in input_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        if path.is_file():
            if _is_document_file(path):
                files.append(path)
            continue
        iterator = path.rglob("*") if recursive else path.glob("*")
        files.extend(file for file in iterator if _is_document_file(file))
    return sorted(dict.fromkeys(files))


def read_document_text(path: Union[str, Path]) -> tuple[str, str]:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return _read_text_file(file_path), "parsed"
    if suffix == ".json":
        return _read_json_file(file_path), "parsed"
    if suffix == ".docx":
        text = _read_docx_file(file_path)
        return text, "parsed" if text else "empty_or_unreadable"
    if suffix == ".pdf":
        text = _read_pdf_file(file_path)
        return text, "parsed" if text else "unsupported_without_pdf_dependency"
    if suffix == ".doc":
        return "", "unsupported_legacy_word"
    return "", "unsupported"


def _extract_field_definitions(
    path: Path,
    line_no: int,
    line: str,
    known_fields: set[str],
) -> list[dict[str, Any]]:
    rows = []
    for field in _fields_in_line(line, known_fields):
        definition = _definition_text(line, field)
        if definition:
            rows.append(
                {
                    "source_file": str(path),
                    "line_no": line_no,
                    "field": field,
                    "definition": definition,
                }
            )
    return rows


def _extract_rule_rows(
    path: Path,
    line_no: int,
    line: str,
    known_fields: set[str],
) -> list[dict[str, Any]]:
    rows = []
    fields = _fields_in_line(line, known_fields)
    if not fields:
        return rows

    for field in fields:
        if _has_required_signal(line):
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "required",
                    field,
                    "is_blank",
                    "",
                    [],
                    f"{field} 必填，不能为空",
                )
            )

        enum_values = _enum_values(line, field)
        if enum_values:
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "enum",
                    field,
                    "not_in",
                    "",
                    enum_values,
                    f"{field} 必须属于允许值: {', '.join(enum_values)}",
                )
            )

        min_value = _min_value(line)
        if min_value is not None:
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "min_value",
                    field,
                    "lt",
                    min_value,
                    [],
                    f"{field} 必须大于等于 {min_value}",
                )
            )

        max_value = _max_value(line)
        if max_value is not None:
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "max_value",
                    field,
                    "gt",
                    max_value,
                    [],
                    f"{field} 必须小于等于 {max_value}",
                )
            )

        if _email_signal(line):
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "format",
                    field,
                    "regex_not_match",
                    EMAIL_PATTERN,
                    [],
                    f"{field} 必须符合邮箱格式",
                )
            )
        rows.extend(_type_rules(path, line_no, field, line))
        if _cleaning_requirement_signal(line):
            rows.append(
                _rule_row(
                    path,
                    line_no,
                    "cleaning_requirement",
                    field,
                    "",
                    "",
                    [],
                    line,
                )
            )

    return rows


def _extract_import_requirements(path: Path, line_no: int, line: str) -> list[dict[str, Any]]:
    if not any(token in line for token in ("导入字段", "目标字段", "系统字段", "输出字段")):
        return []
    fields_text = _after_separator(line)
    fields = _split_values(fields_text)
    return [
        {
            "source_file": str(path),
            "line_no": line_no,
            "requirement_type": "import_field",
            "field": field,
            "description": line,
        }
        for field in fields
    ]


def _extract_markdown_table_insights(
    path: Path,
    text: str,
    known_fields: set[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "field_definitions": [],
        "business_rules": [],
        "import_requirements": [],
    }
    for table in _markdown_tables(text):
        headers = [_normalize_header(header) for header in table["headers"]]
        field_index = _first_header_index(headers, {"field", "字段", "字段名", "name", "column"})
        if field_index is None:
            continue
        for offset, values in enumerate(table["rows"], start=1):
            field = values[field_index].strip()
            if not field:
                continue
            line_no = table["line_no"] + offset
            known_fields.add(field)
            description = _table_cell(headers, values, {"description", "说明", "定义", "口径"})
            if description:
                result["field_definitions"].append(
                    {
                        "source_file": str(path),
                        "line_no": line_no,
                        "field": field,
                        "definition": description,
                    }
                )
            if _truthy_required(_table_cell(headers, values, {"required", "必填", "是否必填"})):
                result["business_rules"].append(
                    _rule_row(
                        path,
                        line_no,
                        "required",
                        field,
                        "is_blank",
                        "",
                        [],
                        f"{field} 必填，不能为空",
                    )
                )
            enum_values = _split_values(
                _table_cell(headers, values, {"enum", "枚举", "允许值", "取值", "可选值"})
            )
            if enum_values:
                result["business_rules"].append(
                    _rule_row(
                        path,
                        line_no,
                        "enum",
                        field,
                        "not_in",
                        "",
                        enum_values,
                        f"{field} 必须属于允许值: {', '.join(enum_values)}",
                    )
                )
            type_text = _table_cell(headers, values, {"type", "类型", "数据类型"})
            result["business_rules"].extend(_type_rules(path, line_no, field, type_text))
            result["import_requirements"].append(
                {
                    "source_file": str(path),
                    "line_no": line_no,
                    "requirement_type": "document_field",
                    "field": field,
                    "description": "字段表定义",
                }
            )
    return result


def _extract_json_insights(
    path: Path,
    text: str,
    known_fields: set[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "field_definitions": [],
        "business_rules": [],
        "import_requirements": [],
    }
    if path.suffix.lower() != ".json":
        return result
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return result

    required_fields = {
        str(field)
        for field in _lookup_json_values(payload, {"required", "required_fields", "必填字段"})
        if isinstance(field, (str, int, float))
    }
    for field, spec in _json_field_specs(payload).items():
        known_fields.add(field)
        description = _json_description(spec)
        if description:
            result["field_definitions"].append(
                {
                    "source_file": str(path),
                    "line_no": 0,
                    "field": field,
                    "definition": description,
                }
            )
        result["import_requirements"].append(
            {
                "source_file": str(path),
                "line_no": 0,
                "requirement_type": "json_schema_field",
                "field": field,
                "description": description or "JSON 字段定义",
            }
        )
        if field in required_fields or _json_required(spec):
            result["business_rules"].append(
                _rule_row(
                    path,
                    0,
                    "required",
                    field,
                    "is_blank",
                    "",
                    [],
                    f"{field} 必填，不能为空",
                )
            )
        enum_values = _json_enum_values(spec)
        if enum_values:
            result["business_rules"].append(
                _rule_row(
                    path,
                    0,
                    "enum",
                    field,
                    "not_in",
                    "",
                    enum_values,
                    f"{field} 必须属于允许值: {', '.join(enum_values)}",
                )
            )
        result["business_rules"].extend(_json_range_rules(path, field, spec))
        result["business_rules"].extend(_type_rules(path, 0, field, _json_type(spec)))
        if str(spec.get("format", "")).lower() == "email":
            result["business_rules"].append(
                _rule_row(
                    path,
                    0,
                    "format",
                    field,
                    "regex_not_match",
                    EMAIL_PATTERN,
                    [],
                    f"{field} 必须符合邮箱格式",
                )
            )
    return result


def _extract_template_requirements(
    tables: dict[str, pd.DataFrame],
    source_inventory: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    if source_inventory is None or source_inventory.empty:
        return []
    rows = []
    for source in source_inventory.to_dict(orient="records"):
        table_name = str(source.get("table", ""))
        file_name = str(source.get("file_name", ""))
        if table_name not in tables or not _looks_like_template(file_name, table_name):
            continue
        for column in tables[table_name].columns:
            rows.append(
                {
                    "source_file": str(source.get("file", "")),
                    "line_no": 0,
                    "requirement_type": "import_template_field",
                    "field": str(column),
                    "description": f"导入模板字段: {column}",
                }
            )
    return rows


def _generated_anomaly_rules(rules: pd.DataFrame) -> list[dict[str, Any]]:
    if rules.empty:
        return []
    generated = []
    for row in rules.to_dict(orient="records"):
        if not row.get("op"):
            continue
        condition: dict[str, Any] = {"field": row["field"], "op": row["op"]}
        if row.get("value") not in ("", None):
            condition["value"] = row["value"]
        values = row.get("values")
        if isinstance(values, str) and values:
            condition["values"] = _split_values(values)
        elif isinstance(values, list) and values:
            condition["values"] = values
        generated.append(
            {
                "name": row["generated_rule_name"],
                "condition": condition,
                "severity": row.get("severity") or "error",
                "reason": row["generated_rule_name"],
                "source": "document_rule",
            }
        )
    return generated


def _rule_row(
    path: Path,
    line_no: int,
    rule_type: str,
    field: str,
    op: str,
    value: object,
    values: list[str],
    description: str,
) -> dict[str, Any]:
    return {
        "source_file": str(path),
        "line_no": line_no,
        "rule_type": rule_type,
        "field": field,
        "op": op,
        "value": value,
        "values": ", ".join(values),
        "severity": _rule_severity(op),
        "description": description,
        "generated_rule_name": f"doc_rule_{_safe_name(field)}_{rule_type}",
    }


# Ops that make a record genuinely undeliverable stay blocking (error): a required
# field is missing, a declared relationship cannot resolve, or a value cannot be
# parsed as its declared type at all. Range / enum / format violations are advisory
# (warn): the value is present and parseable but imperfect, so we flag the row
# without withholding an otherwise-usable record from delivery. This keeps a single
# soft violation from routing the whole row into review.
_BLOCKING_DOC_OPS = frozenset(
    {"is_blank", "lookup_missing", "numeric_invalid", "date_invalid"}
)


def _rule_severity(op: str) -> str:
    return "error" if op in _BLOCKING_DOC_OPS else "warn"


def _deduplicate_rules(rules: pd.DataFrame) -> pd.DataFrame:
    if rules.empty:
        return rules
    return rules.drop_duplicates(
        subset=["rule_type", "field", "op", "value", "values"],
        keep="first",
        ignore_index=True,
    )


def _deduplicate_import_requirements(import_requirements: pd.DataFrame) -> pd.DataFrame:
    if import_requirements.empty:
        return import_requirements
    return import_requirements.drop_duplicates(
        subset=["requirement_type", "field", "source_file"],
        keep="first",
        ignore_index=True,
    )


def _known_fields_for_template_validation(
    tables: dict[str, pd.DataFrame],
    source_inventory: pd.DataFrame | None,
) -> set[str]:
    template_tables = set()
    if source_inventory is not None and not source_inventory.empty:
        for row in source_inventory.to_dict(orient="records"):
            text = f"{row.get('table', '')} {row.get('file_name', '')}".lower()
            if any(token in text for token in ("template", "import", "导入", "模板")):
                template_tables.add(str(row.get("table", "")))

    fields = set()
    for table_name, table in tables.items():
        if table_name in template_tables:
            continue
        fields.update(str(column) for column in table.columns)
    return fields


def _markdown_tables(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    tables = []
    index = 0
    while index < len(lines) - 1:
        if "|" not in lines[index] or not _is_markdown_separator(lines[index + 1]):
            index += 1
            continue
        headers = _split_markdown_row(lines[index])
        rows = []
        row_index = index + 2
        while row_index < len(lines) and "|" in lines[row_index]:
            values = _split_markdown_row(lines[row_index])
            if len(values) < len(headers):
                values.extend([""] * (len(headers) - len(values)))
            rows.append(values[: len(headers)])
            row_index += 1
        tables.append({"line_no": index + 1, "headers": headers, "rows": rows})
        index = row_index
    return tables


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    if not stripped:
        return False
    cells = [cell.strip() for cell in stripped.split("|")]
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _normalize_header(header: str) -> str:
    return header.strip().lower().replace(" ", "_")


def _first_header_index(headers: list[str], candidates: set[str]) -> int | None:
    normalized_candidates = {_normalize_header(candidate) for candidate in candidates}
    for index, header in enumerate(headers):
        if header in normalized_candidates:
            return index
    return None


def _table_cell(headers: list[str], values: list[str], candidates: set[str]) -> str:
    index = _first_header_index(headers, candidates)
    if index is None or index >= len(values):
        return ""
    return values[index].strip()


def _truthy_required(value: object) -> bool:
    text = str(value).strip().lower()
    return text in {"是", "y", "yes", "true", "required", "必填", "1"}


def _type_rules(path: Path, line_no: int, field: str, type_text: object) -> list[dict[str, Any]]:
    text = str(type_text).lower()
    rows = []
    if any(token in text for token in ("number", "numeric", "integer", "float", "decimal")) or any(
        token in str(type_text) for token in ("数值", "数字", "金额", "整数", "小数")
    ):
        rows.append(
            _rule_row(
                path,
                line_no,
                "type_numeric",
                field,
                "numeric_invalid",
                "",
                [],
                f"{field} 必须为数值",
            )
        )
    if any(token in text for token in ("date", "datetime", "timestamp")) or any(
        token in str(type_text) for token in ("日期", "时间")
    ):
        rows.append(
            _rule_row(
                path,
                line_no,
                "type_date",
                field,
                "date_invalid",
                "",
                [],
                f"{field} 必须为合法日期",
            )
        )
    return rows


def _json_field_specs(payload: Any) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}

    def add_field(name: object, spec: Any) -> None:
        if not name:
            return
        field = str(name)
        specs[field] = spec if isinstance(spec, dict) else {"description": str(spec)}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, spec in properties.items():
                    add_field(name, spec)
            for key in ("fields", "columns"):
                fields = node.get(key)
                if isinstance(fields, list):
                    for item in fields:
                        if isinstance(item, dict):
                            add_field(item.get("name") or item.get("field"), item)
                elif isinstance(fields, dict):
                    for name, spec in fields.items():
                        add_field(name, spec)
            for key, value in node.items():
                if key in {"properties", "fields", "columns", "required"}:
                    continue
                if isinstance(value, dict) and _looks_like_field_spec(value):
                    add_field(key, value)
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return specs


def _looks_like_field_spec(value: dict[str, Any]) -> bool:
    keys = {str(key).lower() for key in value}
    return bool(
        keys
        & {
            "type",
            "description",
            "definition",
            "required",
            "enum",
            "values",
            "minimum",
            "maximum",
            "format",
        }
    )


def _lookup_json_values(payload: Any, keys: set[str]) -> list[Any]:
    result = []
    normalized = {key.lower() for key in keys}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in normalized:
                if isinstance(value, list):
                    result.extend(value)
                else:
                    result.append(value)
            elif isinstance(value, (dict, list)):
                result.extend(_lookup_json_values(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            result.extend(_lookup_json_values(item, keys))
    return result


def _json_description(spec: dict[str, Any]) -> str:
    for key in ("description", "definition", "comment", "说明", "定义", "口径"):
        if spec.get(key):
            return str(spec[key])
    return ""


def _json_required(spec: dict[str, Any]) -> bool:
    value = spec.get("required") or spec.get("必填")
    return bool(value) and str(value).lower() not in {"false", "0", "否", "no"}


def _json_enum_values(spec: dict[str, Any]) -> list[str]:
    for key in ("enum", "values", "allowed_values", "枚举", "允许值", "取值"):
        value = spec.get(key)
        if value:
            if isinstance(value, list):
                return [str(item) for item in value]
            return _split_values(value)
    return []


def _json_range_rules(path: Path, field: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    min_value = spec.get("minimum", spec.get("min", spec.get("最小值")))
    max_value = spec.get("maximum", spec.get("max", spec.get("最大值")))
    if min_value is not None:
        rows.append(
            _rule_row(
                path,
                0,
                "min_value",
                field,
                "lt",
                min_value,
                [],
                f"{field} 必须大于等于 {min_value}",
            )
        )
    if max_value is not None:
        rows.append(
            _rule_row(
                path,
                0,
                "max_value",
                field,
                "gt",
                max_value,
                [],
                f"{field} 必须小于等于 {max_value}",
            )
        )
    return rows


def _json_type(spec: dict[str, Any]) -> str:
    return str(spec.get("type", spec.get("数据类型", "")))


def _looks_like_template(file_name: str, table_name: str) -> bool:
    text = f"{file_name} {table_name}".lower()
    return any(token in text for token in ("template", "import", "导入", "模板"))


def _known_fields(tables: dict[str, pd.DataFrame]) -> set[str]:
    fields = set()
    for table in tables.values():
        fields.update(str(column) for column in table.columns)
    return fields


def _fields_in_line(line: str, known_fields: set[str]) -> list[str]:
    lowered = line.lower()
    fields = [
        field
        for field in sorted(known_fields, key=len, reverse=True)
        if field and field.lower() in lowered
    ]
    return fields[:5]


def _definition_text(line: str, field: str) -> str:
    if field not in line:
        return line if any(token in line for token in ("字段", "口径", "定义", "说明")) else ""
    after = line.split(field, 1)[-1].strip(" ：:-\t")
    if not after:
        return ""
    return after


def _has_required_signal(line: str) -> bool:
    return any(token in line for token in ("必填", "不能为空", "不为空", "不可为空", "required"))


def _enum_values(line: str, field: str) -> list[str]:
    patterns = [
        rf"{re.escape(field)}[^。；;\n]*(?:允许值|枚举|取值|可选值)[:：为是\s]*([^。；;\n]+)",
        r"(?:允许值|枚举|取值|可选值)[:：为是\s]*([^。；;\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            return _split_values(match.group(1))
    return []


def _min_value(line: str) -> float | None:
    if any(token in line for token in ("非负", "不小于0", "不能小于0", "大于等于0")):
        return 0.0
    match = re.search(r"(?:>=|大于等于|不小于)\s*(-?\d+(?:\.\d+)?)", line)
    return float(match.group(1)) if match else None


def _max_value(line: str) -> float | None:
    match = re.search(r"(?:<=|小于等于|不大于)\s*(-?\d+(?:\.\d+)?)", line)
    return float(match.group(1)) if match else None


def _email_signal(line: str) -> bool:
    lowered = line.lower()
    has_format_signal = "格式" in line or "format" in lowered
    return has_format_signal and ("email" in lowered or "邮箱" in line)


def _cleaning_requirement_signal(line: str) -> bool:
    return any(
        token in line
        for token in (
            "去空格",
            "去除空格",
            "统一大小写",
            "标准化",
            "清洗要求",
            "转换为",
            "格式化",
        )
    )


def _after_separator(line: str) -> str:
    for separator in (":", "："):
        if separator in line:
            return line.split(separator, 1)[1]
    return line


def _split_values(value: object) -> list[str]:
    text = str(value)
    text = re.sub(r"[，、/|；;]", ",", text)
    return [item.strip(" `\"'[]()（）") for item in text.split(",") if item.strip()]


def _content_lines(text: str) -> list[str]:
    return [line.strip(" -*\t") for line in text.splitlines() if line.strip(" -*\t")]


def _is_document_file(path: Path) -> bool:
    if not path.is_file() or path.name.startswith("~$"):
        return False
    return path.suffix.lower() in DOCUMENT_EXTENSIONS


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def _read_json_file(path: Path) -> str:
    payload = json.loads(_read_text_file(path))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _read_docx_file(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as docx:
            xml = docx.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile):
        return ""
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    texts = [node.text or "" for node in root.iter(f"{namespace}t")]
    return "\n".join(text for text in texts if text.strip())


def _read_pdf_file(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return ""
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    return df[columns]


def _safe_name(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5]+", "_", str(value)).strip("_")
