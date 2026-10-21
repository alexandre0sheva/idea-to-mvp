# CLAUDE.md

Read `AGENTS.md` first: it is this project's operating guide (goal, workflow, definition of done) and ends with the guardrails every session follows. `PRD.md` holds the requirements, `ARCHITECTURE.md` the design, and `plan.md` the tasks (`plan.json` is its source of truth).

<!-- idea-to-mvp:commands -->
## Commands

- Check everything: `bash scripts/check.sh` (install, lint, then the tests with their report; `bash scripts/check.sh test` runs one step). Run it before you finish.
- Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
- Test: `.venv/bin/python -m pytest`
- Test with report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
- Lint: `.venv/bin/ruff check .`
- Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
<!-- /idea-to-mvp:commands -->

## Skills

Project skills live in `.claude/skills/`; use one when its trigger applies:
- `add-api-endpoint`: Use when adding or changing an HTTP route (links, redirect, health) so it follows the router order, auth, error envelope, and repository boundary of this FastAPI app.
- `add-repository-function`: Use when a feature needs new or changed SQL or schema, so the statement lives in the repository layer with the right connection, PRAGMAs, and error translation.
- `add-cli-command`: Use when adding or changing an operator command in `python -m app.cli` (keys, links disable/enable, db init/backup).
- `run-and-debug-locally`: Use when you need to run the service locally, exercise endpoints by hand, or debug a failing test.

## Tools

- Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`; always call tools via `.venv/bin/...`, never a global python or pip.
- One test: `.venv/bin/python -m pytest tests/test_redirect_api.py::test_name -x -vv`; full suite: `.venv/bin/python -m pytest`.
- Lint: `.venv/bin/ruff check .` (use `--fix` for autofixable issues); `bash scripts/check.sh lint` runs only the lint step.
- Run the app: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`; operator commands: `.venv/bin/python -m app.cli ...`.
- Tests use FastAPI `TestClient` with the `tests/conftest.py` tmp_path DB fixtures and the overridable `get_settings` dependency; never point tests at `./data/shortener.db`.
- Coverage report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml` (or `bash scripts/check.sh test`).
