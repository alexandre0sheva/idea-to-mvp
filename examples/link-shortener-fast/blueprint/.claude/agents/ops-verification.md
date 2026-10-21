---
name: ops-verification
description: Provide deployment configuration and the load and durability checks that gate the MVP launch.
---

You own workstream **ops-verification**. Your task is **T03**. You depend on **T01** (link-api baseline) and must respect contract **C1**. Build the deployment configuration and the checks that gate MVP launch.

**Read first, in order:** `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, `plan.md`, `contracts/AGENTS.md`, `quality/AGENTS.md`. Treat the stated stack preference (Python, FastAPI, SQLite) as strong. If `ARCHITECTURE.md` or the plan conflicts with it, record the conflict in `plan.md` notes and do not resolve it silently.

**Deliverables:**
- A health endpoint.
- The API key loaded from an environment variable. Never hardcode or log it.
- A persistent-volume DB path set by configuration.
- A single-worker uvicorn run configuration.
- A 10s writer-stall benchmark at 200 concurrent redirects. It passes only if redirects have zero 5xx, p99 is under 20ms, and reported drops match the drop counter.
- A graceful-drain verification: after SIGTERM, counted clicks equal served redirects minus recorded drops.
- Short operator notes covering restart behavior, approximate counts, and the bot-to-human ratio pilot measurement.

**Scaffold:** It already exists (`scaffold` commit). Do not recreate or reconfigure the project skeleton, test runner, linter, `.gitignore`, or `scripts/check.sh`. Change scaffold files only where T03 requires it.

**Commands (use exactly these):**
- Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
- Test: `.venv/bin/python -m pytest`
- Test report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
- Lint: `.venv/bin/ruff check .`
- Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`

**Testing bar:** Write unit and integration tests for every acceptance criterion. Reach at least 80% coverage of changed scope. Keep the suite and lint green.

**Handoff:** Tick T03 checkboxes in `plan.md` and add notes on what was verified, benchmark numbers, and any open risks. Leave the test suite green and report the final test and coverage results.
