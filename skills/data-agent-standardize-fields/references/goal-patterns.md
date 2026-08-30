# Goal patterns and acceptance

## Reusable goals

- `清理 {表名}.{字段列表} 的前后空格和不可见字符，其他字段保持不变。`
- `把 {字段} 中的 {原值到目标值映射} 统一映射，并列出本次改了什么。`
- `按 {明确格式规则} 标准化 {字段}，不删除、不填充、不去重。`

## Acceptance checks

- Row count and row identity remain unchanged.
- Only named fields and transformations changed.
- Original business values in untargeted fields remain byte-for-byte equivalent when
  exported.
- A requested change manifest reconciles with the actual changed cells.
- Formula-like uploaded strings remain literal unless the user asked the system to
  generate a formula.

Golden references: `customer_text_normalization` and `formula_injection_literal`.
