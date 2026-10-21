# Roadmap and known limitations

What v0.4.0 knowingly leaves open, and what is planned next. Each item was found while building 0.4.0 (the plan it came from was deleted once the release shipped; its design decisions live in `docs/architecture.md`, the user-visible changes in `CHANGELOG.md`). Pick an item, check it is still true, and fix it with a test; delete the line when it is done.

## Next (v0.5)

- **Executor substrates** (Harness²'s OpenCode / Codex seam): an `Executor` protocol behind `implementer.iter_agent_messages` so a run can use Codex CLI or OpenCode. Needs a per-executor sandbox story in `SECURITY.md` before it is safe.
- **Parallel recursion for harness tuning** (K INV + K DOM candidates, K compositions), once `idea-to-mvp bench` shows the pilot pays for itself.
- **Visual smoke test** of web deliveries (a headless-browser screenshot on the dashboard), which needs a browser inside the sandbox.
- **Deploy step** (Fly.io, Render, Vercel) behind its own spend-and-publish gate.

## What live runs showed

The first live benchmark (`docs/benchmarks/2026-10-21-22ccfe6/`) is one idea and one run per profile, so it is a data point, not a ranking.

- **The acceptance author wrote no tests** in either live run (a plain HTTP API idea), so neither had a held-out suite. Find out why before trusting the acceptance column of `bench`.
- **Estimates read low.** A `balanced` run cost $8.06 against the catalog's $3.43, mostly planning ($5.32): complete Opus documents are long (a 24 KB PRD with 33 requirements). The lead session of strategy `subagents` cost $1.34 against $0.11 estimated for all agents. `idea-to-mvp models`, the implement gate, and `bench --dry-run` should use measured planning and lead-session spend; the plan writer's scope budget could be given to the PRD writer too.
- **Budget caps cover agents only.** `bench --max-total-usd` and the global cap stop agent spend; a run can pass its cap by its planning spend, and the cap is checked between ideas.
- **No toolchain check for the chosen stack.** An idea with no stated stack chose Flutter, whose SDK cache is read-only in the sandbox, and failed with an environment error. `doctor` checks the starter templates' toolchains, not the architecture's; the strategy step could prefer a stack with a starter template, or the architect could be told which toolchains the sandbox has.
- **Turn cap on grouped tasks.** `IMPLEMENTER_MAX_TASK_TURNS=40` can end a session whose task the scope budget merged from several small ones; watch for `crashed` task rows in live ledgers.

## Verification

- **Acceptance author.** It is a structured call on the architect's model, not an agent session, so it cannot run its suite before handing it over; a suite that does not import or start fails as acceptance tests the fix sessions cannot see. A dry run of the suite against the empty pack (it must fail cleanly, not error), or an agent-session author, would catch that. The implement gate's estimate does not count the author call, and the suite is not offered as a download beside the delivery zip.
- **Test reports** are read from the main workspace's `.idea-to-mvp/reports/`, so only the tests lane's command leaves evidence. Coverage is shown but never gates the verdict, and the plan's per-task `coverage_target` is not checked.
- **Test integrity.** The lead and workstream sessions of strategy `subagents` are not watched. A drop in the test count is attributed to the verification round, not to the session that caused it. "Otherwise unchanged test" is judged per named test for Python, JS/TS, and Go only (per file elsewhere), and a justification in `quality/TEST_CHANGES.md` clears a finding without the user's approval (it is shown on the dashboard).
- **Fix loop.** Whether a failure is environmental is a heuristic (a lane's `ENVIRONMENT:` mark or a few English patterns in `implementation/fixes.py`), and the `<notes>` contract is checked softly. The demo never fails verification, so attempt folders, history, and the early stop are covered by unit tests, not by `tests/test_e2e_demo.py`.

## Agent harness and harness tuning

- Whether a real session loads the project harness (`CLAUDE.md` and skills through the *project* setting source) is covered by option tests only; a `-m live` case in `tests/test_agent_sandbox_live.py` that asks a session to name a project skill would prove it.
- Skill bodies are written from the plan of wave 2 and are not regenerated when the critic's revise loop rewrites the plan, so they may name old files or commands.
- The `subagents` lead session loads `.claude/agents/*.md` through the project source and also gets the same definitions as `agents=`; harmless while identical.
- Claude Code also reads `CLAUDE.md` files in the folders above a session's folder (under `OUTPUT_DIR`); `SECURITY.md` says to keep them free of such files, nothing enforces it.
- A run killed after a pilot session wrote its ledger row but before its node finished re-runs that session on resume, so the ledger identity fails and the base harness is kept. Recording the row and the node's result atomically would keep the tuning. A run killed after `adopt` committed re-runs it with nothing to commit and records no commit on the card.
- Proposers and the composer read trajectories cut to a character budget (head and tail); serving full trajectories as files to an agent-session composer would remove the cut.
- The selector judges diffs and test results from one call and never runs a build. The selected build is applied after the tuned harness is committed, so a session that edited a harness file the tuned harness also changed conflicts and the pilot task is built again.
- The implement gate's estimate of a tuned run counts a session for the pilot task too (about one task session high).

## Experience library

- The reflection call is not counted in the implement gate's estimate and is not a ledger row.
- The leak check matches exact text, so a lesson that paraphrases a hidden test is not caught (the reflector never sees acceptance failures, which keeps the risk small).
- The domain is the template, else the first two words of the stack, so `Python + FastAPI` and `FastAPI + Python` are two libraries; an alias table would merge them. Only the plan writer, the harness writer, and the harness editors read the library.

## Starter templates

- The JavaScript templates write no coverage into `.idea-to-mvp/reports/` (istanbul's Cobertura file has a DOCTYPE, which `implementation/reports.py` refuses on purpose).
- Templates pin dependency ranges, not lock files, so a scaffold is not reproducible; the `starter-templates` CI job is the canary.
- The template choice is the strategy model's alone, with no deterministic cross-check against the stack, and the implement gate does not list it.
- The Python templates call `.venv/bin/...` and the generated project's check script is bash: Windows hosts are out of scope, as they are for the sandbox.

## Headless runs, benchmark, MCP, publishing

- `run` has no flags for the project preferences, the panel rounds, or the panel mode, and `--max-total-usd` is not stored in the thread (a `resume` without it falls back to the setting).
- A step that raises while the panel's openings run leaves a harmless `RuntimeWarning: coroutine 'arun_with_retry' was never awaited` (seen in `tests/test_headless.py`).
- `bench` runs each idea once per configuration; a `--repeat N` would tell a real gain from noise. An idea whose step raised is unscored with no cost until its retry reads the thread's usage.
- The MCP server has no `continue_run` tool (a session stopped with the server is continued with `idea-to-mvp resume`), does not expose publishing, has no per-call budget or limit on concurrent runs, and keeps progress lines in memory only.
- A session is published once; pushing a later version to the same repository is by hand (moved tags need a rule). The `publication` record is not in the session export or the `sessions` list. The file check is by name (`.idea-to-mvp/`, `.env*`), so a secret written into a source file is not found; a scan with the library's key-shape patterns could warn before a public push.

## Models, cost, and observability

- `gemini-4-argon` is in the catalog but in no profile; re-check it with `idea-to-mvp models --check` for the `quality` Skeptic.
- The catalog has one price per model, so long-context rates (Haiku 5.5 above 100K tokens) are not modelled and the `fast` agent estimate is low.
- Reasoning models get `DISCUSSION_MAX_TOKENS` with no reasoning-effort setting, so a long reasoning pass can leave a cut-off panel turn.
- Trajectories carry no per-entry times, `[OBSERVATION]` lines are not tied to their `[ACTION]` by tool-call id, and nothing prunes `.idea-to-mvp/trajectories/`.
- The UI cannot open a saved session whose delivery zip lies outside the current `OUTPUT_DIR` (for example a bench session copied elsewhere): Gradio refuses to serve the file and the whole view fails instead of leaving the download out.

## Repository

- `scripts/make_example.py` takes a dashboard screenshot made by hand, and the README screenshots are taken by hand from a demo run (`scripts/make_demo_gif.py` builds the GIF from them); a scripted capture would make both reproducible.
- Release engineering: tag `v0.3.0` and `v0.4.0`; publishing to PyPI is manual (see `CONTRIBUTING.md`, "Releasing").
