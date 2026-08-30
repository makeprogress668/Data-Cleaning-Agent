---
name: data-agent-consolidate-files
description: Combine multiple same-shaped periodic Excel or CSV files into one row-preserving dataset. Use for 月度、季度、门店或批次文件纵向合并; do not use for relational joins or deduplication.
---

# Periodic file consolidation

Use the normal Data Agent pipeline. Consolidation is a row union, not permission to
clean, deduplicate, aggregate or normalize source values.

## Required facts

Confirm the intended file set and the target output. Prefer an input directory that
contains only those files. If the directory also contains unrelated spreadsheets or
rule documents, ask the user to identify the exact scope before execution.

## Workflow

1. Check that files represent the same business entity and have compatible columns.
   If schemas differ materially, surface the difference instead of forcing a union.
2. Form the goal with [references/goal-patterns.md](references/goal-patterns.md).
3. Run from the repository root:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main answer "{input_directory}" `
     --goal "{goal}" --output "{output_dir}"
   ```

   On macOS/Linux use `backend/.venv/bin/python`.
4. Verify the final workbook. Row count must equal the sum of the selected source
   tables, every source period must be represented, and original cell values must be
   preserved.

Do not add `--yes`, drop duplicates, fill blanks or standardize formats unless the
user separately requested those operations.
