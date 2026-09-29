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
uv run pytest
```

Dependencies live only in `pyproject.toml`; after changing them run `uv lock` and commit `uv.lock` (CI uses `uv sync --locked`).

If you change planner output, exported Markdown, or prompt-driven behavior, verify the relevant flow manually in the UI as well.

## Pull Requests

- Keep changes focused and explain motivation.
- Add tests for behavior changes when they provide meaningful regression protection.
- Update `README.md`, `CHANGELOG.md`, or contributor docs when setup/runtime behavior changes.
- Never commit secrets (`.env`, API keys, credentials).
- Include any important manual verification notes in the PR description.
