# Contributing

Thanks for contributing.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run Locally

```bash
python app.py
```

## Suggested Workflow

1. Create a focused branch or worktree.
2. Make one logical change at a time.
3. Run lint/tests for the touched area.
4. Update docs when user-facing flow, prompts, or generated outputs change.

## Quality Checks

```bash
ruff check .
pytest
```

If you change planner output, exported Markdown, or prompt-driven behavior, verify the relevant flow manually in the UI as well.

## Pull Requests

- Keep changes focused and explain motivation.
- Add tests for behavior changes when they provide meaningful regression protection.
- Update `README.md`, `CHANGELOG.md`, or contributor docs when setup/runtime behavior changes.
- Never commit secrets (`.env`, API keys, credentials).
- Include any important manual verification notes in the PR description.
