## Contract Registry

- C1: Core contract — The interface shared between workstreams.

## Tasks

### T01. Project setup and link-api
- Goal: Establish the MVP baseline and deliver this workstream's first slice.
- Workstream: link-api.
- Depends on: none.
- Requirements: R1.
- Contracts in: none.
- Contracts out: C1.
- Implementation scope: Code, configuration, and tests for this workstream.
- Acceptance criteria:
  - R1 demonstrably satisfied.
- Required unit/integration tests:
  - Unit and integration tests for every acceptance criterion.
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Updated docs and task notes.

### T02. Build click-pipeline
- Goal: Deliver the click-pipeline workstream.
- Workstream: click-pipeline.
- Depends on: T01.
- Requirements: R1.
- Contracts in: C1.
- Contracts out: none.
- Implementation scope: Code, configuration, and tests for this workstream.
- Acceptance criteria:
  - R1 demonstrably satisfied.
- Required unit/integration tests:
  - Unit and integration tests for every acceptance criterion.
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Updated docs and task notes.

### T03. Build ops-verification
- Goal: Deliver the ops-verification workstream.
- Workstream: ops-verification.
- Depends on: T01.
- Requirements: R1.
- Contracts in: C1.
- Contracts out: none.
- Implementation scope: Code, configuration, and tests for this workstream.
- Acceptance criteria:
  - R1 demonstrably satisfied.
- Required unit/integration tests:
  - Unit and integration tests for every acceptance criterion.
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Updated docs and task notes.

## Project Commands

- Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
- Test: `.venv/bin/python -m pytest`
- Test report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
- Lint: `.venv/bin/ruff check .`
- Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
