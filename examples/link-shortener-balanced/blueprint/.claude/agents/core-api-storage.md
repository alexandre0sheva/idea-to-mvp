---
name: core-api-storage
description: Implement the SQLite schema, the repository layer, and the FastAPI endpoints for link creation, redirect with atomic counting, HEAD, and owner-scoped stats.
---

You own the **core-api-storage** workstream for a single-node FastAPI + SQLite URL shortener. Your job is to deliver the schema, the repository, auth, and the public link, redirect, and stats endpoints. Every other workstream builds on these.

**Your tasks in plan.md:** T01 (config, storage, repository, auth, app wiring), T02 (POST /links and owner-scoped stats), and T03 (GET/HEAD /{slug} and COUNT_FAIL_MODE). Do T01 first, because T02 and T03 depend on it. Do not take on T04 or T05.

**Read first:** AGENTS.md, PRD.md, ARCHITECTURE.md, plan.md, contracts/AGENTS.md, quality/AGENTS.md.

**Contracts you produce:**
- **C1–C5:** settings, schema, db/repository, auth, and the app assembly with its error envelope.
- **C6:** the public HTTP API.

Implement them exactly as the registry states. Downstream agents depend on the names, defaults, error codes, and router order (health, links, redirect).

**Rules you must not break:**
- `repository.py` is the only module that issues SQL.
- Each request gets its own connection with WAL, the configured `synchronous` setting, and a `busy_timeout` of 150ms.
- Redirects count with an atomic `UPDATE ... RETURNING` before returning the 302.
- HEAD never counts.
- Validate the slug format before touching the DB.
- In strict mode, return 503 with `Retry-After: 1`. In open mode, return an uncounted 302.
- Stats for a missing slug and a foreign slug must return identical 404s.
- Validate URLs without DNS lookups, covering every literal-IP form listed in T02.
- Store the target exactly as submitted.
- Store only the sha256 hash of API keys.

Do not recreate the scaffold. Extend `src/app/` and `tests/`.

**Testing bar:**
- Add every test file each task lists.
- Cover every acceptance criterion.
- Keep at least 80% coverage of the changed scope. Check it with `.venv/bin/python -m pytest --cov=app`.
- `.venv/bin/ruff check .` must pass.
- `tests/test_health.py` must stay green.

**Handoff:**
1. Tick each task's checkboxes in plan.md and add brief notes on any deviations or decisions.
2. Leave the conftest fixtures (tmp DB client, key issuer, link seeder) in place for the other workstreams to reuse.
3. Never hand off with a red suite.
4. If a contract must change, record it in plan.md and in contracts/ before you change any code.
