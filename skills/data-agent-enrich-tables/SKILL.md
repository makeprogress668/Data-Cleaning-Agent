---
name: data-agent-enrich-tables
description: Enrich a declared base Excel or CSV table from related tables while preserving base rows. Use for 关联、补齐、VLOOKUP-style field lookup; do not use for aggregating detail rows or merging same-shaped files.
---

# Related-table enrichment

Use the repository's existing Data Agent CLI. Do not implement joins with ad-hoc
pandas code: the normal planner, confirmation policy, Capability Registry and Output
Linter must remain in control.

## Required facts

Before execution, identify:

- the base table whose row set must be preserved;
- the lookup table;
- left and right key fields;
- the exact fields to bring back.

If any of these are ambiguous, stop and ask a short clarification. Never choose a
base table or join key from plausibility alone.

## Workflow

1. Form an explicit goal using the patterns in
   [references/goal-patterns.md](references/goal-patterns.md).
2. Run from the repository root:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main answer "{input_path}" `
     --goal "{goal}" --output "{output_dir}"
   ```

   On macOS/Linux use `backend/.venv/bin/python`. Prefer a directory containing
   only the intended inputs.
3. Do not add `--yes`; this workflow is additive. If the generated plan unexpectedly
   asks to remove or overwrite data, show that plan to the user and stop.
4. Inspect the delivered workbook, not only the command exit status. Check that base
   row count and order are preserved, requested fields are present, and unrelated
   source fields were not copied.

Unmatched keys stay unmatched. Report them only when the user requested an unmatched
list; never drop those rows implicitly. Generate spreadsheet formulas only when the
user explicitly asks for formulas.
