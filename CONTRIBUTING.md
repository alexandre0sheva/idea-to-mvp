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

CI also fails on a stale `uv.lock` (`uv lock --check`), on a graph diagram that is out of sync (`uv run python scripts/gen_graph_diagram.py` fixes it), and on a golden blueprint pack that stops validating (`tests/test_golden_blueprints.py`). `uvx pre-commit install` (one time) runs ruff and mypy before every commit.

Tests that call real provider APIs carry `@pytest.mark.live`, are skipped by default, and need keys: `uv run pytest -m live`. The opt-in blueprint quality eval, which spends tokens and is never run by CI, is `uv run python scripts/eval_blueprint.py --live --idea climbing-log` (add `DEMO_MODE=true` to try it offline).

Dependencies live only in `pyproject.toml`; after changing them run `uv lock` and commit `uv.lock` (CI uses `uv sync --locked`).

Docs are tested too: `tests/test_docs_sync.py` fails when a doc names a file, test, or setting that does not exist, so a renamed module or removed setting shows up as a failing test rather than a stale paragraph.

If you change planner output, exported Markdown, or prompt-driven behavior, verify the relevant flow manually in the UI as well.

## Pull Requests

- Keep changes focused and explain motivation.
- Add tests for behavior changes when they provide meaningful regression protection.
- Update `README.md`, `CHANGELOG.md`, or contributor docs when setup/runtime behavior changes.
- Never commit secrets (`.env`, API keys, credentials).
- Include any important manual verification notes in the PR description.
