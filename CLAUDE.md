# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` sits beside this file and is not repeated here. It holds the product
principle every default follows, the conventions that are easy to violate, and the
current known gotchas. **Read it before changing behaviour.** This file covers
commands and the shape of the system.

## Commands

Everything runs from the repo root unless stated. Use the venv interpreter, never the
system one (see AGENTS.md for why).

```bash
# Backend tests (500+), lint
python -m pytest backend/tests -q
python -m pytest backend/tests/test_rollup.py -q                       # one file
python -m pytest backend/tests/test_rollup.py::test_a_key_with_no_detail_rows_totals_zero -q
cd backend && python -m ruff check src tests ../scripts                # line limit 100
cd backend && python -m ruff check --fix src tests ../scripts

# Acceptance matrix — checks the delivered workbook, not function behaviour.
# Run this for any change that can affect what the user receives.
python scripts/make_acceptance_data.py data/acceptance                 # fixtures
python scripts/run_acceptance_matrix.py scripts/acceptance_cases.json --no-llm
python scripts/run_acceptance_matrix.py scripts/acceptance_cases.json  # with the model
python scripts/run_acceptance_matrix.py scripts/acceptance_cases.json -k F_授权

# Services
backend/.venv/Scripts/python.exe -m uvicorn data_agent.api:app --host 127.0.0.1 --port 8000
cd apps/web && npm run dev            # console on :5173, proxies /api to :8000
cd apps/web && npm run build          # tsc -b && vite build — the only frontend check

# CLI
data-agent answer <input-dir> --goal "..." --output <dir>   # full run + delivery
data-agent discover <input-dir>                             # profile only
data-agent plan <input-dir> --goal "..."                    # reviewable plan, no execution
data-agent understanding-cache --clear                      # drop cached goal understandings
```

CI runs backend ruff + pytest on ubuntu-3.10 / ubuntu-3.12 / windows-3.12, the
deterministic acceptance matrix, and the frontend build.

## Architecture

### Two layers, one rule

The LLM only ever returns JSON. Every mutation of a DataFrame happens in `tools/` and
`pipelines/`; nothing under `agent/` touches data. A wrong model answer costs a wrong
*plan*, never corrupted output.

### The contract chain

```
goal ──► understand_goal ──► TaskSpec ──► JobConfig ──► ExecutionPlan ──► run_job ──► workbook
         agent/            schemas/     schemas/      capabilities/    pipelines/   OutputSpec
         goal_understanding  task.py     job.py        planning.py      runner.py   + linter
```

Each seam is validated, and the validators are the fastest way to understand what the
system refuses to do:

| Validator | Refuses |
| --- | --- |
| `planning/goal_interpreter.validate_task_spec_operations` | a row-removing rule not traceable to the TaskSpec |
| `capabilities/planning.validate_task_action_coverage` | a plan step for an action the user did not ask for, or a claimed action nothing covers |
| `capabilities/planning.validate_task_rule_preservation` | an LLM draft that alters already-validated rules |
| `capabilities/planning.validate_execution_plan` | a step that downgrades its own risk or confirmation |
| `tools/output_linter.lint_output` | an undeclared sheet/field, or a declared one that never got written |

When a change is rejected by one of these, the usual fix is to declare the new thing
properly (registry entry, OutputSpec field, action mapping) rather than to loosen the
check.

### Two entry points, one deliverable

`services/business_delivery.answer_input_paths` (CLI, synchronous) and
`api/job_manager` (async jobs, with clarification and confirmation pauses) must produce
the same workbook for the same goal. Both go through `services/processing.plan_input_paths`
→ `execute_planned_job`, and both project the result through `tools/delivery_view`.
Anything that formats a deliverable belongs there, not in an entry point.

### Understanding is cached, not recomputed

`agent/understanding_cache` keys on goal + schema + prompt version + model +
data-access mode. The same request cannot drift between runs, and a model outage
replays the previous reading instead of silently falling back to keyword parsing.
`understanding_source` distinguishes `llm` / `llm_cached` / `deterministic` /
`deterministic_fallback`, and the console shows the last one as a warning.

### Import direction

`schemas/` and `utils/` are leaves — they import nothing from the project. `planning/`
and `tools/` may use them; `agent/` sits on top; `services/` and `api/` compose. A new
helper that needs pandas but no project code belongs in `utils/`. Putting it in
`tools/` has caused an import cycle (`tools/__init__` pulls `recommender`, which needs
`planning`).

### Where each Excel-shaped capability lives

Reaching a capability from a sentence usually needs a parser in `agent/task_spec.py`,
a config field in `schemas/job.py`, an executor in `tools/` or `utils/`, a registry
entry in `capabilities/builtins.py`, and a step in `capabilities/planning.py`. Missing
any one of them fails in a different place — the registry omission is the quiet one
(its stage metrics get dropped and the plan reports phantom skips).

| Operation | Executor |
| --- | --- |
| cross-table fill (VLOOKUP) | `tools/lookup.py` |
| cross-table total (SUMIF) | `tools/rollup.py` |
| stack same-shaped files | `tools/table_union.py` |
| wide → long, cross-tab | `utils/reshape.py`, `tools/pivot.build_crosstab` |
| split a column | `utils/text_split.py` |
| live Excel formulas in the output | `tools/excel_formula.py` |
| business number parsing | `utils/numeric.py` |
| target table shape | `utils/target_schema.py` |
