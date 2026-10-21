# speed-1: the first run with `agent_team` and the plan writer's scope budget (2026-10-21)

`bench --name speed-1 --live --ideas climbing-log --profile fast --max-total-usd 3 --max-usd-per-idea 3 --set IMPLEMENTER_PERMISSION_MODE=bypassPermissions --set EXECUTION_STRATEGY=agent_team`

Result: **not verified**, $0.40, 13.6 minutes (the first run, `smoke-live`, took 51 minutes for the same idea). It is not a speed comparison and is not in the README table:

- The architect chose a **Flutter** app this time (the first run chose a React PWA; the idea states no stack). The sandbox cannot write the Flutter SDK's cache, so `T01` spent its 40 turns on the environment and ended without an app; `T02`-`T04` were skipped and all three lanes failed because no project existed.
- A run that fails this early is short and cheap for that reason, not because it was fast.

What it changed: the speed measurement moved to an idea that states its stack (`link-shortener`), see `fast-1` and `balanced-1`.
