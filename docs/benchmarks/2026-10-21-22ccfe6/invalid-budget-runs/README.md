# fast-1 and balanced-1: invalid runs (2026-10-21)

`bench --name fast-1|balanced-1 --live --ideas link-shortener --max-usd-per-idea 3 --set IMPLEMENTER_PERMISSION_MODE=bypassPermissions --set EXECUTION_STRATEGY=agent_team`

These two runs are **not valid results** and are not in the README table. The per-idea budget ($3) equalled the per-task cap that applied (the user's `.env` sets $10, clamped to the budget), and the parallel engine reserves the per-task cap for every task it starts, so after `T01` (which cost $0.03 and $0.26) no other task could start: "Not started: the whole-run budget has $2.97 left, less than the $3.00 a task can need". `T02`... were never built, and the verdicts (not verified; `fast-1` for a missing `GET /{code}` redirect, a P0 requirement; `balanced-1` for a truncated PRD and missing routes) say nothing about the models. The ~15 minute wall times are for a one-task build.

| Run | Outcome | Lanes | Acceptance | Fixes | Cost | Time |
|---|---|---|---|---|---|---|
| `fast-1` | not verified | 3/4 | 3/3 | 2 | $0.31 | 878 s |
| `balanced-1` | not verified | 2/3 | none written | 2 | $3.71 | 926 s |

(`fast-1`'s raw files were removed by mistake before they were copied here; its row is from the bench's printed table. `balanced-1`'s `config.json` and `results.json` are next to this file.)

The fix: `bench` now refuses a per-idea budget below twice the per-task cap (`bench.check_task_budget`, `tests/test_bench.py::test_a_per_idea_budget_that_fits_one_task_session_is_refused`). The valid runs are `fast-2` and `balanced-2`, with `--max-usd-per-idea 4 --set IMPLEMENTER_MAX_TASK_USD=1.5`.
