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
- `add-endpoint`: Use when adding or changing an HTTP route in the FastAPI app, such as link creation, redirect, or stats, to put it in its own router module with validated models and TestClient tests.
- `add-sql-migration`: Use when a change needs a new or altered SQLite table, column, or index, so it is applied as a plain-SQL migration at app startup.
- `add-click-counter-work`: Use when changing the click pipeline: the bounded queue, the batched flush worker, the drop counter, or the SIGTERM drain, so redirects never block on the database.
- `run-app-locally`: Use when starting the service on a developer machine to try the API, check health, or reproduce a bug by hand before writing a test.
- `add-test-fixture`: Use when a test needs an app client, a temporary SQLite database, an API key, or a clock, so each test starts from a clean, isolated state.

## Tools

- Install with the project install command: python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]". Add runtime dependencies to the dependencies list in pyproject.toml and test or lint tools to the dev extra, then reinstall.
- Run all tests with .venv/bin/python -m pytest. To run one file or test, add its path or a -k expression to the same command, for example .venv/bin/python -m pytest tests/test_health.py -q.
- Lint with .venv/bin/ruff check . and fix every reported issue before finishing; do not add ignore rules to pyproject.toml to silence new code.
- Generate the coverage and JUnit reports with .venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml. Coverage for changed scope must be at least 80 percent.
- Start the app with .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 and check GET /health. Use a single worker only.
- When a test fails, read the full traceback and the failing assertion first, then fix the code under test; do not edit expected values to match wrong output.
