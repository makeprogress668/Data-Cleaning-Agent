# Goal patterns and acceptance

Replace braces with real table and field names. Keep the table names exactly as the
Data Agent discovered them.

## Reusable goals

- `以 {主表} 为主表，按 {左键}/{右键} 从 {关联表} 补充 {字段列表}，保持主表行数和顺序。`
- `把 {关联表}.{来源字段} 按 {关联键} 关联到 {主表}，只新增 {输出字段}。`
- When formulas are explicitly required: `用 VLOOKUP 按 {关联键} 把 {字段} 匹配到 {主表}。`

## Acceptance checks

- Delivered base rows equal input base rows unless the user explicitly requested a
  filter.
- Keys and requested fields exist in the declared tables.
- One lookup row cannot silently multiply a base row.
- Unmatched rows remain present with an empty result field.
- Output contains only the requested enrichment fields plus declared base fields.

Golden reference: `order_customer_enrichment`.
