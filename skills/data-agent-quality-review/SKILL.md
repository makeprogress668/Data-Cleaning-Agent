---
name: data-agent-quality-review
description: Inspect Excel/CSV data against explicit business rules and deliver issue reasons or a review list without treating review as deletion permission. Use for 检查、分析、标记异常、列出问题 or 可用性复核.
---

# Data quality review

This skill applies when the user asked to inspect or report problems. It does not
authorize deletion, deduplication, filling blanks or normalizing values.

## Required facts

Determine the business identifier, the requested checks, and whether supporting
PDF/Word/Markdown/JSON rule documents are included. If the user says “能用的留下”
without defining “能用”, the missing criterion is blocking: ask before execution.

## Workflow

1. Express the exact checks and requested review output using
   [references/goal-patterns.md](references/goal-patterns.md).
2. Keep data files and their supporting rule documents in the intended input scope.
3. Run from the repository root:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main answer "{input_path}" `
     --goal "{goal}" --output "{output_dir}"
   ```

4. Inspect the workbook issue sheet/review list. Each item must retain a usable
   business identifier, a Chinese business reason, the affected field and evidence.
5. Reconcile issue counts with the source and ensure marking/review was not converted
   into implicit row deletion.

Never add `--yes` for a review request. If a generated plan includes destructive
steps, show it to the user and stop.
