# Contributing

Thanks for contributing.

## Development Setup

```bash
uv sync          # creates .venv from uv.lock, including dev tools
cp .env.example .env
```

## Run Locally

```bash
uv run idea-to-mvp                 # real providers (needs keys in .env)
DEMO_MODE=true uv run idea-to-mvp  # offline: canned outputs, no keys, no spend
uv run idea-to-mvp doctor          # verify keys and models before a real run
uv run idea-to-mvp models          # model catalog: profiles, prices, cost of a typical run (--check: live, opt-in)
DEMO_MODE=true uv run idea-to-mvp bench --name smoke   # the golden ideas end to end, offline (see "Benchmark")
```

Use demo mode to exercise UI and graph changes end to end. When you add an LLM call, structured schema, or agent session, add its fixture in `src/idea_to_mvp/demo/` so demo mode and `tests/test_e2e_demo.py` keep covering the whole pipeline.

## Suggested Workflow

1. Create a focused branch or worktree.
2. Make one logical change at a time.
3. Run lint/tests for the touched area.
4. Update docs when user-facing flow, prompts, or generated outputs change.

## Quality Checks

```bash
uv run ruff check .
uv run mypy
uv run pytest                                  # offline: fakes and demo mode, no keys
uv run pytest --cov=idea_to_mvp                # what CI runs; fails below 80% coverage
```

CI also fails on a stale `uv.lock` (`uv lock --check`), on a graph diagram that is out of sync (`uv run python scripts/gen_graph_diagram.py` fixes it), and on a golden blueprint pack that stops validating (`tests/test_golden_blueprints.py`). `uvx pre-commit install` (one time) runs ruff, `ruff format`, and mypy before every commit; `uv run ruff format .` formats the tree by hand.

The starter templates (`src/idea_to_mvp/templates/`) are installed and checked for real by `uv run pytest -m scaffold tests/test_templates.py` (needs node 22.12+, npm, Python 3, and network; the `starter-templates` CI job runs it, the default run leaves it out).

Tests that call real provider APIs carry `@pytest.mark.live`, are skipped by default, and need keys: `uv run pytest -m live`. The opt-in blueprint quality eval, which spends tokens and is never run by CI, is `uv run python scripts/eval_blueprint.py --live --idea climbing-log` (add `DEMO_MODE=true` to try it offline).

## Benchmark

`idea-to-mvp bench --name <run>` runs the golden ideas (`tests/fixtures/golden_ideas.json`) headless with autopilot and `--implement` and scores each one: delivered and verified, lanes passed, acceptance pass rate, integrity flags, fix attempts, cost, and wall time. It spends money unless `DEMO_MODE=true`, so a real run needs `--live --max-total-usd N` (preview the cost with `--dry-run` first), and it is never part of CI beyond the offline smoke test in `tests/test_bench.py`.

```bash
uv run idea-to-mvp bench --name balanced --live --max-total-usd 60 --max-usd-per-idea 10
uv run idea-to-mvp bench --name fast --live --max-total-usd 60 --max-usd-per-idea 10 --profile fast
uv run idea-to-mvp bench --compare balanced fast     # only the ideas both runs scored
```

A run is a folder under `OUTPUT_DIR/bench/` with its own output root and a settings snapshot: call the same `--name` again to retry the ideas that could not run (they are *unscored*, not zero), but it refuses to continue under other settings, so compare configurations with different names (`--profile`, `--harness-tuning`, `--library`, `--set KEY=VALUE`). Each running task reserves `IMPLEMENTER_MAX_TASK_USD` (default 5) from the idea's budget, so `--max-usd-per-idea` must be at least twice that (the bench refuses less): a smaller budget starts the first task of a plan and never the rest. For small budgets lower the task cap for the run, e.g. `--max-usd-per-idea 4 --set IMPLEMENTER_MAX_TASK_USD=1.5`. Add an idea to the golden file to put it under the bench, the eval script, and the golden pack tests.

Dependencies live only in `pyproject.toml`; after changing them run `uv lock` and commit `uv.lock` (CI uses `uv sync --locked`).

Docs are tested too: `tests/test_docs_sync.py` fails when a doc names a file, test, or setting that does not exist, so a renamed module or removed setting shows up as a failing test rather than a stale paragraph.

If you change planner output, exported Markdown, or prompt-driven behavior, verify the relevant flow manually in the UI as well.

## Releasing

A release is a version bump plus a tag; publishing to PyPI stays a manual step, so no workflow holds a PyPI credential.

1. Bump `version` in `pyproject.toml`, move the `## Unreleased` entries of `CHANGELOG.md` under the new version, and commit.
2. Tag it and push the tag: `git tag -a v0.4.0 -m "v0.4.0"`, then `git push origin v0.4.0`.
3. The `Release build` workflow (`.github/workflows/release.yml`) runs on the tag: it refuses a tag that does not match `version`, builds the sdist and wheel with `uv build`, runs `twine check --strict` on them, and keeps them as the `dist` artifact.
4. Download the artifact, try the wheel in a clean environment, and publish by hand: `uvx twine upload dist/*` with your own PyPI token.

To check a build before tagging: `uv build` and `uvx twine check --strict dist/*`. The sdist is an allowlist (`[tool.hatch.build.targets.sdist]` in `pyproject.toml`), because hatch honours only the repository's `.gitignore` and would otherwise ship a file you ignore globally, such as `.claude/settings.local.json`.

## Folders from before 0.3

Before 0.3 the app wrote `exports/`, `project_blueprints/`, and `generated_projects/` inside the repository checkout. Now everything goes under `OUTPUT_DIR` (default `~/idea-to-mvp`), and those three folders are only git-ignored leftovers. To keep an old run, move its folder into `OUTPUT_DIR` (`project_blueprints/` is now `blueprints/` and `generated_projects/` is `projects/`); otherwise delete them: `rm -r exports project_blueprints generated_projects`. The app never reads or removes them.

## Pull Requests

- Keep changes focused and explain motivation.
- Add tests for behavior changes when they provide meaningful regression protection.
- Update `README.md`, `CHANGELOG.md`, or contributor docs when setup/runtime behavior changes.
- Never commit secrets (`.env`, API keys, credentials).
- Include any important manual verification notes in the PR description.
