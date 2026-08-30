# Goal patterns and acceptance

## Reusable goals

- Grouped total: `按 {维度} 统计 {指标} 合计。`
- Grand total: `统计全部 {业务实体} 的 {指标} 总和，不分组。`
- Trend: `按 {时间字段} 分析 {指标} 趋势，生成一张 {图表类型} 和简要报告。`
- Crosstab: `按 {行维度} 和 {列维度} 做交叉表，统计 {指标} {聚合方式}。`

## Acceptance checks

- Summary keys are exactly the requested dimensions.
- A grand-total request produces one answer row rather than returning only detail.
- Numeric-string and accounting-negative values reconcile with a hand-calculated
  sample.
- Chart data equals workbook summary data.
- No unrequested sheet, chart, report or technical field appears.

Golden references: `monthly_sales_trend`, `text_numeric_city_total`,
`two_level_header_total`, and `city_month_crosstab`.
