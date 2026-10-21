# Live benchmark, 2026-10-21 (commit 22ccfe6 plus this release's changes)

The valid results are `fast-2` and `balanced-3` (one idea, `link-shortener`, one run per profile); `compare.txt` and `compare.json` are `idea-to-mvp bench --compare fast-2 balanced-3`. The README's table is checked against these files (`tests/test_docs_sync.py`).

```bash
idea-to-mvp bench --name fast-2 --live --profile fast --ideas link-shortener --max-usd-per-idea 4 --max-total-usd 2.5 \
  --set IMPLEMENTER_PERMISSION_MODE=bypassPermissions --set EXECUTION_STRATEGY=agent_team --set IMPLEMENTER_MAX_TASK_USD=1.5
idea-to-mvp bench --name balanced-3 --live --profile balanced --ideas link-shortener --max-usd-per-idea 4 --max-total-usd 5 \
  --set IMPLEMENTER_PERMISSION_MODE=bypassPermissions --set EXECUTION_STRATEGY=agent_team --set IMPLEMENTER_MAX_TASK_USD=1.5
```

`--max-total-usd` caps the agents' budget per idea, not planning spend: `balanced-3` spent $5.32 on planning and $2.74 on agents against a $5 cap.

## What the live runs cost in all

| Run | Result | Cost |
|---|---|---|
| `smoke-live` | not verified (sandbox refused the lane's server), diagnostic | $1.73 |
| `speed-1` | not verified (Flutter chosen, unbuildable in the sandbox), diagnostic | $0.40 |
| `invalid-budget-runs/fast-1`, `balanced-1` | one task built (budget rule), invalid | $0.31 + $3.71 |
| `invalid-budget-runs/balanced-2` | truncated PRD (Opus thinking spent the output cap), invalid | $4.14 |
| `fast-2` | verified | $0.25 |
| `balanced-3` | verified | $8.06 |
| | **bench total** | **$18.60** |

The bench total is the spend the runs recorded; a few cents more went on diagnostic calls (the truncation check). It was more than the $14 first planned: the invalid runs were each worth finding (each has its own README above), and `balanced-3` cost about twice what was expected.
