---
name: data-agent-summarize-metrics
description: Calculate business totals, grouped summaries, trends, crosstabs, charts, or short reports from Excel/CSV data. Use when the user asks 统计、汇总、趋势、交叉表 or a chart; do not use for row-level cleaning.
---

# Business metric summary

The goal must name the measure, aggregation, and grouping dimension. A chart or report
is an independent output request: never add either because it seems useful.

## Required facts

Identify:

- the measure field and aggregation (`sum`, `count`, `mean`, and so on);
- the grouping fields, or that the user wants one grand total;
- optional time grain and ordering;
- whether a chart, crosstab or written report was explicitly requested.

If two fields could satisfy an unnamed dimension such as “城市”, ask the user to
choose. Use a selected Semantic Model when the business measure is organization-
specific.

## Workflow

1. Build the goal using [references/goal-patterns.md](references/goal-patterns.md).
2. Run from the repository root:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main answer "{input_path}" `
     --goal "{goal}" --output "{output_dir}"
   ```

3. Inspect the delivered summary values and dimensions. Text business numbers such as
   `1,234`, `(567)` and `1.2万` may participate in calculations, but their detail cells
   must remain unchanged.
4. Confirm that chart/report files exist only when requested and that their values
   reconcile with the workbook summary.

Do not clean, filter or remove detail records merely to make the summary look tidy.
