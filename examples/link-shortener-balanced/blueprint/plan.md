## Contract Registry

- C1: Settings (src/app/config.py) — Frozen dataclass from load_settings(): DATABASE_PATH=./data/shortener.db, BASE_URL=http://127.0.0.1:8000, COUNT_FAIL_MODE=strict|open, SQLITE_SYNCHRONOUS=NORMAL|FULL, SQLITE_BUSY_TIMEOUT_MS=150, CREATE_RATE_LIMIT_PER_MIN=60, THREAD_LIMIT=32. Invalid enums raise ConfigError naming the variable. get_settings dependency is overridable in tests.
- C2: SQLite schema (src/app/schema.sql) — Idempotent CREATE TABLE IF NOT EXISTS links(slug TEXT PK, target, owner_id, created_at, clicks DEFAULT 0, disabled DEFAULT 0) + owner_id index; api_keys(key_hash TEXT PK, owner_id, label, created_at). Applied by db.init_db() at startup and by `cli db init`.
- C3: Repository and db layer (src/app/repository.py, db.py) — db.connect(settings): per-call sqlite3 connection, isolation_level=None, journal_mode=WAL, synchronous, busy_timeout. check_sqlite_version() requires >=3.35.0. Repository: create_link (raises SlugTaken), redirect_and_count (UPDATE...RETURNING, raises StoreBusy), get_active_target, get_link_for_owner, owner_for_key_hash, set_disabled, insert_api_key, delete_api_key, list_api_keys, ping(timeout_ms), backup(dest). The only module issuing SQL.
- C4: Auth (src/app/auth.py) — hash_key(raw) returns the sha256 hex digest. generate_key() returns a secrets token. require_owner dependency parses `Authorization: Bearer <key>` and does an uncached lookup. It returns owner_id or 401 {"detail":"unauthorized"} with WWW-Authenticate: Bearer.
- C5: App assembly and error envelope (src/app/main.py, errors.py) — Lifespan does the version check, init_db, and AnyIO thread limiter. Routers are included in the order health, links, redirect. Domain errors map to JSON {"detail": code} plus Retry-After where applicable. JSON responses send Cache-Control: no-store.
- C6: Public HTTP API — POST /links returns 201 {slug,short_url,target,created_at} with Location, or 401/422/429/503. GET /links/{slug}/stats returns 200 {slug,target,clicks,created_at,disabled} to the owner and 404 otherwise. GET /{slug} returns a counted 302 with Cache-Control: no-store, or 404 not_found. On StoreBusy, strict returns 503 with Retry-After: 1 and open returns an uncounted 302. HEAD /{slug} never counts. /live and /health return 200. /ready returns 200 or 503.
- C7: Operator CLI (python -m app.cli) — Subcommands: keys issue --owner [--label] (prints the plaintext once), keys revoke, keys list, links disable/enable <slug>, db init, db backup --out DIR. Exit 0 on success and non-zero with a message on error or not-found.
- C8: Deployment artifacts — Dockerfile (python:3.12-slim, 1 uvicorn worker), compose.yaml with a /data volume and the C1 env vars, and scripts/backup.sh (cli db backup, BACKUP_COPY_CMD, prune copies older than 7 days).

## Tasks

### T01. Config, storage, repository, auth, app wiring
- Goal: Build the foundation every endpoint uses: validated settings, idempotent schema, per-request WAL connections, the repository API, hashed Bearer auth, the error envelope, and router order.
- Workstream: core-api-storage.
- Depends on: none.
- Requirements: R1, R2, R3, R4, R5, R6, R7, R8, R31.
- Contracts in: none.
- Contracts out: C1, C2, C3, C4, C5.
- Implementation scope: Add src/app/config.py, db.py, schema.sql (as package data), repository.py, models.py, auth.py, and errors.py. Update main.py with a lifespan that runs the version check, init_db (creating the parent dir), and the thread limiter. Add stub routers routes/health.py (keep /health), routes/links.py, and routes/redirect.py in that order. Add a utc_now_iso() helper. Map OperationalError locked/busy to StoreBusy and IntegrityError to SlugTaken. Add tests/conftest.py with a tmp_path DB app client, a key issuer, and a link seeder.
- Acceptance criteria:
  - Defaults match R1; COUNT_FAIL_MODE=lenient or SQLITE_SYNCHRONOUS=OFF fails startup with a ConfigError naming the variable
  - Starting twice on the same DB file succeeds and preserves rows; a missing parent dir is created
  - On a repository-created connection: journal_mode is wal, synchronous matches the configured value, busy_timeout equals SQLITE_BUSY_TIMEOUT_MS
  - Monkeypatched sqlite_version 3.34.1 makes startup fail with an error mentioning 3.35
  - created_at parses as UTC ISO 8601 ending in Z or +00:00
  - Missing header, 'Basic x', 'Bearer ', and an unknown key each return 401 {"detail":"unauthorized"} with WWW-Authenticate: Bearer; a valid key resolves its owner_id
  - The plaintext key appears in no api_keys column; after its hash is deleted, the next request returns 401
  - redirect_and_count increments by exactly 1 and returns the target, returns None for missing or disabled links, and raises StoreBusy under a held write lock
  - Error responses use the {"detail": code} envelope and JSON responses carry Cache-Control: no-store
  - tests/test_health.py still passes
- Required unit/integration tests:
  - tests/test_config.py
  - tests/test_db.py
  - tests/test_repository.py
  - tests/test_auth.py
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Storage, auth, and config modules; conftest fixtures; routers registered in their final order for T02, T03, and T04 to fill.

### T02. Link creation and owner-scoped stats
- Goal: Implement POST /links (slugs, reserved words, collision retry, validation, rate limit) and GET /links/{slug}/stats with owner privacy.
- Workstream: core-api-storage.
- Depends on: T01.
- Requirements: R5, R9, R10, R11, R12, R13, R14, R15, R16, R23, R24.
- Contracts in: C1, C3, C4, C5.
- Contracts out: C6.
- Implementation scope: Add src/app/slugs.py: secrets base62 generation, a RESERVED frozenset including health, and reserved regeneration that does not consume a retry. Add src/app/validation.py: scheme, length <=2048, host, whitespace and control chars, localhost/*.localhost, and ipaddress literal checks. Literal checks cover inet_aton forms (integer, octal, hex, short) and bracketed IPv6 including IPv4-mapped, with no DNS. Add src/app/ratelimit.py: per-key 60s window with an injectable clock. Fill routes/links.py: Pydantic body with extra=forbid; 3 attempts on SlugTaken, then 503 with Retry-After: 1 and slug_generation_failed. Stats returns 404 for both missing and foreign slugs. Add OpenAPI descriptions.
- Acceptance criteria:
  - A valid POST returns 201 {slug,short_url,target,created_at}; Location == short_url == BASE_URL.rstrip('/')+'/'+slug; the row has the caller's owner_id, clicks=0, disabled=0
  - An extra field 'slug' returns 422; the same target posted twice gives two distinct slugs matching ^[0-9A-Za-z]{7}$
  - One collision then success returns 201; three collisions return 503 with Retry-After: 1 and slug_generation_failed
  - A generator yielding 'openapi' then a valid slug returns the valid slug
  - Each R13/R14 reject case returns 422 with a detail naming the failed rule: ftp, 2049 chars, no host, whitespace, localhost, foo.localhost, 127.0.0.1, 10.0.0.1, 192.168.1.1, 172.16.0.1, 169.254.1.1, 0.0.0.0, 2130706433, 0177.0.0.1, 0x7f.0.0.1, 127.1, [::1], [fe80::1], [fc00::1], [::ffff:127.0.0.1]
  - 2048 chars, 8.8.8.8, [2606:4700::1111], and example.com are accepted
  - The target is stored and returned exactly as submitted
  - Request N+1 within the window returns 429 rate_limited with integer Retry-After >=1 and inserts no row; another key is unaffected
  - Stats returns 200 {slug,target,clicks,created_at,disabled} to the owner, identical 404 not_found for foreign and missing slugs, and 401 for a bad key
- Required unit/integration tests:
  - tests/test_slugs.py
  - tests/test_validation.py
  - tests/test_ratelimit.py
  - tests/test_links_api.py
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: POST /links and the stats route with tests; validation, slugs, and ratelimit modules.

### T03. Redirect, HEAD, and COUNT_FAIL_MODE
- Goal: Implement GET and HEAD /{slug} with atomic count-before-redirect, 404 rules, strict/open lock-timeout behavior, and OpenAPI click semantics.
- Workstream: core-api-storage.
- Depends on: T01.
- Requirements: R17, R18, R19, R20, R21, R22.
- Contracts in: C1, C3, C5.
- Contracts out: C6.
- Implementation scope: Fill routes/redirect.py. Check the path against ^[0-9A-Za-z]{7}$ before touching the DB. GET calls redirect_and_count; HEAD calls get_active_target. On StoreBusy: strict returns 503 store_busy with Retry-After: 1; open falls back to get_active_target and returns an uncounted 302. Responses send Cache-Control: no-store. The app OpenAPI description defines a click. Tests seed links via the repository.
- Acceptance criteria:
  - GET on an enabled slug returns 302 with Location == target and Cache-Control: no-store; clicks increases by exactly 1
  - 50 concurrent threaded GETs leave clicks == 50
  - HEAD returns 302 with the same headers and leaves clicks unchanged
  - Missing, disabled, 'abc', 'abcdefgh', and a lowercased variant of a mixed-case slug each return 404 not_found with no counter change
  - With StoreBusy patched in: strict returns 503 with Retry-After: 1; open returns 302 with clicks unchanged
  - The OpenAPI description states that bots and retries count, HEAD never counts, and open mode is a lower bound
- Required unit/integration tests:
  - tests/test_redirect_api.py
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Redirect routes and documented click semantics.

### T04. Health checks, operator CLI, backup, Docker
- Goal: Provide /live and /ready, CLI key/link/db commands, consistent online backups, and the container deployment.
- Workstream: ops-cli-deploy.
- Depends on: T01.
- Requirements: R7, R8, R25, R26, R27, R28, R29, R30, R32, R33.
- Contracts in: C1, C2, C3, C4, C5.
- Contracts out: C7, C8.
- Implementation scope: routes/health.py: /live (async, no DB), /health as an alias, and /ready (ping with 50ms timeout, 503 not_ready on failure). Add src/app/cli.py (argparse) using Connection.backup for db backup. Add scripts/backup.sh, a Dockerfile, compose.yaml, and README ops notes.
- Acceptance criteria:
  - /live and /health return 200 {status:ok} even when the repository raises
  - /ready returns 200 ready normally and 503 not_ready when ping raises
  - keys issue prints the plaintext once and stores only the sha256; the issued key authenticates
  - keys revoke makes the next request 401
  - links disable makes GET return 404; links enable restores the 302; an unknown slug exits non-zero
  - db backup writes an openable timestamped copy containing the existing links while the DB is in WAL mode
- Required unit/integration tests:
  - tests/test_health_routes.py
  - tests/test_cli.py
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: CLI, health routes, Dockerfile, compose.yaml, backup.sh, README ops section.

### T05. Benchmarks, crash reconciliation, launch readiness
- Goal: Verify the performance gates and crash semantics, then document the COUNT_FAIL_MODE and synchronous decisions for launch.
- Workstream: tests-benchmarks.
- Depends on: T02, T03, T04.
- Requirements: R17.
- Contracts in: C1, C6, C7, C8.
- Contracts out: none.
- Implementation scope: bench/locustfile.py covers 200 RPS on one hot slug, 200 RPS across 100 slugs, and a 10x burst. bench/kill9_reconcile.py compares 302s observed by the client with stored clicks after kill -9. Write docs/BENCHMARKS.md comparing FULL vs NORMAL and recording the gate decisions. Add a README launch checklist (key issuance, backup cron, failure-mode docs). Add tests/test_e2e_flow.py covering the full journey.
- Acceptance criteria:
  - E2E: create via API, 3 GETs and 1 HEAD, then stats shows clicks == 3; the CLI-disabled link returns 404
  - BENCHMARKS.md records p95 <200ms, a 503 rate <0.1%, exact counts, and responsive /live under each scenario for FULL and NORMAL, with the chosen settings
  - kill -9 reconciliation shows stored clicks >= client-observed 302s
- Required unit/integration tests:
  - tests/test_e2e_flow.py
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Bench scripts, BENCHMARKS.md, reconciliation results, and the launch checklist.

## Project Commands

- Install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
- Test: `.venv/bin/python -m pytest`
- Test report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
- Lint: `.venv/bin/ruff check .`
- Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
