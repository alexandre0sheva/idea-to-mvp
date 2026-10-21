# AGENTS.md: Application Workstream (URL Shortener MVP)

This file is for agents implementing product features: endpoints, storage, auth, validation, operator CLI, health checks, and their tests. Read it before every task. Each task covers one slice. Finish it, verify it, and hand it off.

**Source of truth:** `ARCHITECTURE.md`. If this file and the architecture disagree, the architecture wins. Flag the conflict in your handoff notes; do not resolve it silently.

---

## 1. Ground rules

- **Stack is fixed:** Python 3.12, FastAPI, stdlib `sqlite3`, one uvicorn worker. Do not add an ORM, async DB driver, Redis, Alembic, or a background worker.
- **Scaffold is fixed:** `python-fastapi`, src layout, package `app`, app object in `src/app/main.py`.
  - Do not recreate or reconfigure `pyproject.toml` tooling, `.gitignore`, `scripts/check.sh`, or the test runner.
  - Only add dependencies.
- **Commands.** Use exactly these:
  - Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
  - Test: `.venv/bin/python -m pytest`
  - Test report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
  - Lint: `.venv/bin/ruff check .`
  - Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- **Scope is MVP only.** Do not build:
  - custom slugs
  - dashboards
  - per-click event tables
  - background or batched counting
  - DNS lookups or blocklists
  - link previews
  - billing
  - rate limiting on redirects

  If a task seems to need one of these, stop and note it.

---

## 2. How to slice work

Each task delivers one vertical slice. A slice is complete when it includes:

1. The code.
2. Its tests.
3. Any contract or doc update it requires.

Rules:

- **One task, one behavior.** Examples: "POST /links happy path + 401" or "HEAD /{slug} never counts". Do not combine an unrelated refactor with a feature.
- **Build in dependency order:**
  1. config
  2. db
  3. schema
  4. repository
  5. errors/models
  6. auth
  7. validation and slugs
  8. routes
  9. CLI
  10. health
  11. ops files
- **Stub forward, don't reach forward.** If your slice needs a module that does not exist yet:
  - Create it with the minimal interface signature from `ARCHITECTURE.md` §4.1.
  - Mark the stub with `# TODO(T<id>)`.
  - Do not implement another task's logic.
- **Keep it shippable.** After every task, `pytest` and `ruff` pass and the app starts.
- **Ownership by workstream:**

| Workstream | Owns |
|---|---|
| core-api-storage | `config.py`, `db.py`, `schema.sql`, `repository.py`, `models.py`, `errors.py`, `auth.py`, `ratelimit.py`, `validation.py`, `slugs.py`, `routes/links.py`, `routes/redirect.py`, `logging.py` |
| ops-cli-deploy | `routes/health.py`, `cli.py`, `scripts/backup.sh`, `Dockerfile`, `compose.yaml`, README ops section |
| tests-benchmarks | `tests/` suites beyond per-slice tests, `bench/`, `docs/BENCHMARKS.md` |

---

## 3. How to consume contracts

Treat these as frozen interfaces. Code against them as written.

| Contract | Location | Rule |
|---|---|---|
| HTTP endpoints, status codes, headers | ARCHITECTURE §4.2 | Exact status codes and headers. No extra endpoints. |
| Repository interface | ARCHITECTURE §4.1 | Function names, args, return types, and raised exceptions are fixed. |
| Schema | ARCHITECTURE §3, `schema.sql` | Column names and types are fixed. |
| Config / env vars | ARCHITECTURE §5 | Read only through `app.config`. Never call `os.environ` elsewhere. |
| Error envelope | ARCHITECTURE §6, `app.errors` | Every non-2xx/3xx JSON response uses it. |
| URL validation rules | ARCHITECTURE §7, `app.validation` | Pure function. No network. |
| Click semantics | ARCHITECTURE §1 | Stated in OpenAPI descriptions verbatim in meaning. |

### Non-negotiable invariants

**Layering**
- Only `app/repository.py` contains SQL. Routes, CLI, and health checks call repository functions.
  - Exception: `ping()` lives in the repository too.
- The repository has no HTTP types and no policy. It raises `StoreBusy`, `StoreUnavailable`, or `SlugTaken`. Routes decide what those mean, including strict vs. open handling.

**Database connections**
- One `sqlite3` connection per request, opened via `db.connect()` and closed at the end of the request.
- Never share connections across threads. Never pool or cache them.
- Connection settings:
  - `isolation_level=None`
  - `busy_timeout = BUSY_TIMEOUT_MS` (default 150)
  - WAL mode
  - `synchronous` taken from config (default NORMAL)
- Startup refuses to run if `sqlite3.sqlite_version < 3.35`.

**Handlers**
- Handlers are sync `def`. The one exception is `/live`, which is `async def` and touches no DB.

**Redirect (`GET /{slug}`)**
- Counting is exactly one statement: `UPDATE links SET clicks = clicks + 1 WHERE slug = ? AND disabled = 0 RETURNING target`.
- The click commits before the 302 is returned.
- Zero rows returned gives 404.
- The 302 response sets `Cache-Control: no-store`.

**HEAD (`HEAD /{slug}`)**
- Calls `get_active_target`. It never writes.
- Status and headers match GET.

**Count-fail policy (`StoreBusy` on redirect)**
- `strict`: 503 with `Retry-After: 1`.
- `open`: an uncounted 302, but only if the read succeeds. If the read also fails, return 503.

**Stats (`GET /links/{slug}/stats`)**
- A missing slug and another owner's slug both return 404.
- The two responses must be byte-identical in body, so callers cannot tell them apart.

**Routing**
- Router order in `main.py` is health, then links, then redirect last.
- `/health` stays as an alias of `/live`.

**Slugs**
- 7 characters, base62, generated with `secrets`.
- Retry up to 3 times on `SlugTaken`. If all retries fail, return 500.
- Never generate a reserved word.
- `RESERVED` must include every first path segment the app serves: `links`, `live`, `ready`, `health`, `docs`, `redoc`, `openapi.json`, plus any others listed in the architecture.

**API keys**
- Store only the sha256 hex digest of the key.
- The raw key is shown once, by the CLI at issue time.
- The raw key is never logged, stored, or echoed in errors.

**URL validation**
- Allow only `http` and `https`, up to 2048 characters.
- Reject `localhost`.
- Reject private, loopback, and link-local IP literals using `ipaddress`.
- Also catch octal, hex, integer, and IPv6 forms, such as `http://2130706433/` and `http://0177.0.0.1/`.
- Do not resolve DNS.

**Changing a contract.** Do not do it inside a feature task. Write a "Contract change request" in your handoff:
- what changes
- why
- which callers are affected

The lead agent applies it.

---

## 4. Avoiding cross-task conflicts

Some files are shared by several tasks. Edit them minimally and append-only where you can.

| Hot file | Rule |
|---|---|
| `src/app/main.py` | Add only your `include_router` line in its required position, plus any lifespan or handler hook your task needs. Do not reorder existing lines. |
| `src/app/schema.sql` | Append only. Use `CREATE ... IF NOT EXISTS`. Never edit or drop an existing column or table. |
| `src/app/repository.py` | Add new functions. Do not change existing signatures. |
| `src/app/config.py` | Add new fields with safe defaults and validation. Do not rename existing fields. |
| `src/app/errors.py`, `models.py` | Add new types. Do not change existing shapes. |
| `pyproject.toml` | Add dependencies only, in sorted position. Do not touch tool config. |

Checklist before editing:

- [ ] I touch only files my task owns, plus minimal additions to hot files.
- [ ] I did not reformat or reorder code I am not changing.
- [ ] I did not rename public functions, env vars, columns, routes, or CLI commands.
- [ ] Tests for other slices still pass unchanged. If they fail, I fix my code, not their assertions.
- [ ] New tests use an isolated temp DB (`tmp_path`) and env overrides, not a shared file.

---

## 5. Acceptance criteria

A task is done only when every applicable box is checked.

### Always
- [ ] `.venv/bin/python -m pytest` passes.
- [ ] `.venv/bin/ruff check .` passes.
- [ ] The app starts with the run command, and `GET /live` returns 200.
- [ ] Every new behavior has at least one happy-path test and one failure-path test.
- [ ] Every new status code is asserted, including headers where the contract specifies them (`Retry-After`, `Cache-Control`, `Location`).

### Per area
- **Create link:**
  - 201 with `{slug, short_url, target, created_at}`
  - 401 for a missing or unknown key
  - 422 for each validation class
  - 429 with `Retry-After`
  - collision retry tested by monkeypatching `new_slug`
  - a reserved slug is never returned
- **Redirect:**
  - 302 with `Location` and `Cache-Control: no-store`
  - count incremented by exactly 1
  - 404 for a missing slug and for a disabled slug
  - strict mode: `StoreBusy` gives 503 with `Retry-After: 1`, and the count is unchanged
  - open mode: `StoreBusy` gives a 302, and the count is unchanged
- **HEAD:** same status and headers as GET, and the count is unchanged after N HEADs.
- **Concurrency:** N threads × M GETs on one slug produce a final count of exactly N×M.
- **Stats:**
  - the owner sees correct `clicks`
  - a non-owner gets a 404 body identical to a missing slug's 404 body
  - 401 for a bad key
- **Validation:** a table-driven test covers the schemes, the length boundary (2048 passes, 2049 fails), `localhost`, and the private, loopback, and link-local IP forms, including octal, integer, and IPv6. Valid public hosts pass.
- **Health:**
  - `/live` works even with the DB missing or locked
  - `/ready` returns 503 when the DB is locked beyond 50 ms
  - `/health` still passes `tests/test_health.py`
- **CLI:**
  - key issue prints the raw key once and stores only the hash
  - revoke deletes the hash, and the owner's links survive
  - disable and enable change redirect behavior
  - backup produces a readable, consistent DB copy
- **OpenAPI:** endpoint descriptions state the click semantics: bots and retries count, HEAD never counts, and open mode gives a lower bound.

---

## 6. Migration discipline

There is no migration framework.

- `schema.sql` is idempotent. It runs at startup and via `python -m app.cli db init`.
- Changes are additive only:
  - new tables
  - new indexes
  - new nullable columns, or columns with a default
- An `ALTER TABLE ... ADD COLUMN` cannot be `IF NOT EXISTS` in SQLite. Put it in a separate guarded step in `db.py` that checks `PRAGMA table_info` first.
- Never drop, rename, or retype a column. Never rewrite existing rows at startup.
- `/ready` and request handlers must never run schema statements. Bootstrap happens only in lifespan and the CLI.
- Every schema change needs a test that:
  - applies the schema twice to the same DB with no error
  - opens a DB created by the previous schema and passes the bootstrap

Checklist:
- [ ] The change is additive and idempotent.
- [ ] `ARCHITECTURE.md` §3 is updated, via a contract change request if a column is new.
- [ ] Repository functions that read the new column handle old rows (NULL or default).

---

## 7. Observability expectations

- **Format:** one JSON line per event, written to stdout via `app.logging`. Do not use `print`.
- **Request log** (middleware), one line per request with these fields:
  - `ts`
  - `level`
  - `request_id` (honor an incoming `X-Request-ID` or generate one; echo it in the response header)
  - `method`
  - `route` (the route template, not the raw path)
  - `status`
  - `duration_ms`
  - `owner_id` (when authenticated)
- **Domain events.** Log these explicitly, with stable `event` names:
  - `store_busy`: include `op` and `count_fail_mode`, plus the outcome (`503` or `uncounted_redirect`)
  - `slug_collision`: include the attempt number
  - `slug_retries_exhausted`
  - `rate_limited`: include `owner_id`
  - `ready_failed`
  - `startup`: include the SQLite version, `synchronous`, `count_fail_mode`, `thread_limit`, and `busy_timeout_ms`
  - CLI actions: `key_issued`, `key_revoked`, `link_disabled`, `link_enabled`, `backup_done`, `backup_failed`
- **Never log:**
  - raw API keys
  - full `Authorization` headers
  - DB file contents
- **Target URLs:** log only at debug level, or log the host only.
- **Benchmark support:** `store_busy` counts and `duration_ms` must be enough to compute the 503 rate and p95 from logs alone. Do not add a metrics stack for the MVP.

---

## 8. Before handoff

Update everything your change touches, then write handoff notes.

- [ ] Code, tests, and lint all pass with the exact commands in §1.
- [ ] **OpenAPI:** route summaries, response models, and documented error codes are accurate.
- [ ] **`ARCHITECTURE.md`:** update it only through an approved contract change. Otherwise, list proposed changes in your notes.
- [ ] **`schema.sql`**, and its idempotency test, if storage changed.
- [ ] **Config:** any new env var has a default, validation in `config.py`, and an entry in the README env table.
- [ ] **README:** the ops section covers any new CLI command, env var, or operational behavior, such as backup and `COUNT_FAIL_MODE`.
- [ ] **`docs/BENCHMARKS.md`:** updated if your change touches the redirect path, db PRAGMAs, or the thread limit. Note "re-benchmark required" if you could not run the benchmark.
- [ ] No leftover `TODO` markers without a task id, no debug prints, and no unused stubs.

**Handoff note template** (put it in the PR or task output):

```
Task: T<id> <title>
Changed: <files>
Contracts touched: none | <list + approval ref>
Schema change: none | <additive change>
New env vars / CLI commands: none | <list>
Tests added: <names>
Known gaps / follow-ups: <list with task ids>
Contract change requests: none | <what, why, affected callers>
```
