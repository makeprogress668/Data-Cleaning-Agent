---
name: data-agent-standardize-fields
description: Apply explicitly requested text or value standardization to named Excel/CSV fields and optionally report the changes. Use for 去空格、清理不可见字符、值映射 or field normalization; do not use for open-ended quality analysis.
---

# Explicit field standardization

Only perform the operations the user named, on the fields they named. “帮我清洗一下”
alone is not permission to normalize every column; ask which changes are wanted or
route an explicit analysis request to the quality-review skill.

## Workflow

1. Identify the target fields, exact transformations and whether the user wants a
   change manifest. Use [references/goal-patterns.md](references/goal-patterns.md).
2. For overwriting operations, preview the plan first:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main plan "{input_path}" `
     --goal "{goal}" --output "{plan_workbook}" --job-output "{job_config}"
   ```

3. Execute only after the plan matches the request:

   ```powershell
   backend\.venv\Scripts\python.exe -m data_agent.cli.main answer "{input_path}" `
     --goal "{goal}" --output "{output_dir}"
   ```

4. Use `--yes` only when the user explicitly approves the concrete plan and its impact.
5. Inspect the workbook: row count and untargeted fields must be unchanged; requested
   changes must be visible in the delivered cells. Include a change manifest only when
   requested.

Do not infer permission to fill missing values, delete rows, deduplicate records,
change number formats or replace business codes.
