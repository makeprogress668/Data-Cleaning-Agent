# Goal patterns and acceptance

## Reusable goals

- `把 {期间列表} 的 {业务实体} 文件纵向合并成一张表，保留全部记录，不去重、不清洗。`
- `合并目录中的 {文件范围}，每个来源文件都必须进入结果，并保留来源信息。`
- `把各 {门店/部门/批次} 的同结构明细追加为一张总表，字段按原表保留。`

## Acceptance checks

- Select the file set explicitly; do not depend on an arbitrary directory listing.
- Output rows equal the sum of rows in all selected inputs.
- The earliest and latest period, plus at least one row from each source, are present.
- No implicit deduplication, null deletion or value normalization occurred.
- Unexpected schema differences are reported before delivery.

Golden reference: `monthly_union`.
