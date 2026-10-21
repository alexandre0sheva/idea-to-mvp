## What and why

<!-- The change and the reason for it. Link the issue if there is one. -->

## Checks

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest --cov=idea_to_mvp` pass (coverage stays at 80% or more)
- [ ] New behaviour has a test; a new LLM call or agent session has its fixture in `src/idea_to_mvp/demo/`
- [ ] Docs are updated where they live: `docs/architecture.md` (design), `.env.example` (settings), `SECURITY.md` (what agents may do), `README.md` (what it is and how to start), `CHANGELOG.md` (what a user sees)
- [ ] No secrets, `.env` content, or generated project output in the diff

## How you checked it

<!-- Commands you ran, and for a UI change, what you saw with `DEMO_MODE=true uv run idea-to-mvp`. -->
