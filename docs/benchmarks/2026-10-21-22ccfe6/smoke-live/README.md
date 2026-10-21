# smoke-live: the first live run (2026-10-21, commit 22ccfe6)

`bench --name smoke-live --live --ideas climbing-log --profile fast --max-total-usd 3 --max-usd-per-idea 3 --set IMPLEMENTER_PERMISSION_MODE=acceptEdits`

One idea, the `fast` profile. Result: **delivered, not verified**: $1.73 (planning $0.09, agents $1.64), 51 minutes, 2 fix attempts.

What it showed (this is why it is kept):

- The quality lane's smoke probe failed twice with `listen EPERM`: the macOS sandbox refused to let the lane start the project's dev server. That is a bug in the sandbox settings, not in the project, and it alone kept the verdict at "not verified" (tests and requirements passed in the final round). Fixed afterwards: verification lanes now get `allowLocalBinding` (`implementation/options.py`).
- `acceptEdits` stops compound shell commands ("requires approval"), which a headless run cannot give: the first fix session ended at its 40-turn limit after its work was done.
- The acceptance author wrote no tests (the `fast` profile's architect is Haiku 5.5, and the app is a single-page web UI with no CLI or HTTP contract), so there is no acceptance rate.
- The catalog's estimate for `fast` ($0.17 a run) was about 10x too low: the lead session of strategy `subagents` alone cost $1.34.

These numbers are from a run before that fix; they are not a benchmark result and are not in the README table.
