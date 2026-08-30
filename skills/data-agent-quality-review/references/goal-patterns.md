# Goal patterns and acceptance

## Reusable goals

- `检查 {表名}，列出 {字段/规则} 不符合要求的记录及异常原因，使用 {标识字段} 标识记录。`
- `分析 {表名} 的数据质量，按 {规则来源} 生成问题说明和待复核清单，不删除原记录。`
- `标出 {条件} 的记录并输出复核问题，保留全部原始行。`

## Acceptance checks

- Every review row has a stable identifier; if no identifier exists, ask how records
  should be referenced.
- Reasons use business wording rather than internal field markers.
- Rule-document conflicts are surfaced, not silently resolved.
- “标记” and “复核” preserve source records and do not authorize deduplication.
- Only explicitly requested issue categories are reported.

Golden references: `supplier_quality_review`, `laboratory_sample_review`, and
`duplicate_annotation_preserves_rows`.
