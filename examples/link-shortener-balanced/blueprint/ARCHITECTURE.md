# ARCHITECTURE.md: URL Shortener MVP (Option A: Single-node FastAPI + SQLite monolith)

## 1. System overview

This is an invite-only URL shortener for 3 to 10 developers. It has one user-facing journey:

1. An authenticated developer creates a short link.
2. Anyone can follow the short link. The redirect counts the click atomically before the 302 is sent.
3. Only the link's owner can read its click count.

The whole service is one Python process: FastAPI with sync handlers on a single uvicorn worker. It runs in one container on one small VM. Its only datastore is a SQLite file on a persistent volume. All storage access goes through a repository layer, so a later move to Postgres or Redis stays inside one module.

**Design targets**

| Dimension | Target |
|---|---|
| Traffic | 20 redirects/s average; 200 redirects/s burst on one hot slug |
| Scale | Up to ~100k links, 3–10 owners |
| Error budget | <0.1% 503 rate; p95 latency <200 ms |
| Durability | Losing a few seconds of clicks on a host crash is acceptable (`synchronous=NORMAL`); nightly off-box backup |

**Click semantics (public contract, stated in OpenAPI)**

- A click is one committed `GET /{slug}` that returned 302.
  - The counter increment is committed in the same statement that resolves the target, before the 302 is sent.
- Bots, retries and prefetchers all count when they produce a committed 302. The service does not count unique visitors.
- `HEAD` requests never count.
- In `COUNT_FAIL_MODE=open`, a redirect that hits a lock timeout is served as an uncounted 302. The reported count is then a documented lower bound.

```
                         ┌───────────────────────────────┐
  Developer (API key) ──►│                               │
  Visitor / bot ────────►│  Reverse proxy (TLS)          │  operator-provided
  Health checker ───────►│  e.g. Caddy / platform LB     │
                         └──────────────┬────────────────┘
                                        │ HTTP
┌───────────────────────────────────────▼──────────────────────────────────────┐
│ Container: uvicorn (1 worker, AnyIO thread limit 32)                          │
│                                                                               │
│  app.main (FastAPI app, lifespan, error handlers, request-log middleware)     │
│     │                                                                         │
│     ├── routes.health ── /live (async, no DB)   /ready (SELECT 1, 50ms)       │
│     ├── routes.links  ── POST /links            GET /links/{slug}/stats       │
│     │      ├── auth        (Bearer key → sha256 → key_hash, owner_id)         │
│     │      ├── ratelimit   (in-process 60s window per API key)                │
│     │      ├── validation  (scheme/length/host/IP-literal checks)             │
│     │      └── slugs       (7-char secrets base62, reserved-word block)       │
│     └── routes.redirect ── GET/HEAD /{slug}   (registered LAST)               │
│            └── count-fail policy (strict → 503 / open → uncounted 302)        │
│                         │                                                     │
│  app.repository  ◄──────┘  the only module that issues SQL                    │
│     └── app.db  (per-request sqlite3 connection, PRAGMAs, schema bootstrap)   │
│                         │                                                     │
│  app.cli  (operator: keys, link disable/enable, db init, db backup) ──┐       │
└─────────────────────────┼─────────────────────────────────────────────┼───────┘
                          ▼                                             │
              ┌─────────────────────────┐   sqlite3 .backup API         │
              │ /data/shortener.db (WAL)│◄──────────────────────────────┘
              │ persistent Docker volume│──► nightly copy off-box (BACKUP_COPY_CMD)
              └─────────────────────────┘
```

## 2. Components, responsibilities, boundaries

The layout builds on the `python-fastapi` scaffold. The `app` package uses a src layout, and the app object is `app` in `src/app/main.py`.

```
src/app/
  main.py          FastAPI app, lifespan, router inclusion order, exception handlers, middleware
  config.py        Settings from env (frozen dataclass); validated at startup
  db.py            connect() context manager, PRAGMAs, schema bootstrap, version check
  schema.sql       Plain SQL schema (CREATE TABLE IF NOT EXISTS ...)
  repository.py    All SQL; returns plain dataclasses; raises StoreBusy on lock timeout
  models.py        Pydantic request/response models + dataclasses (Link, ApiKey)
  auth.py          API-key parsing, hashing, owner resolution dependency
  ratelimit.py     Per-API-key 60-second window limiter for POST /links
  validation.py    Target URL validation (pure functions, no I/O)
  slugs.py         Slug generation + reserved-word set
  errors.py        Error envelope, domain exceptions → HTTP mapping
  logging.py       JSON log formatter, request-id handling
  routes/health.py /live, /ready
  routes/links.py  POST /links, GET /links/{slug}/stats
  routes/redirect.py GET/HEAD /{slug}
  cli.py           Operator CLI (python -m app.cli ...)
scripts/backup.sh  Nightly backup wrapper (cron/host timer)
bench/             locust (or k6) scripts, kill -9 reconciliation script
docs/BENCHMARKS.md Results: FULL vs NORMAL, hot slug vs 100 slugs, 10× burst
Dockerfile, compose.yaml
```

The scaffold's `/health` route is kept as an alias of `/live`, so the existing `tests/test_health.py` keeps passing. `health` is also added to the reserved slugs.

| Component | Responsibility | Boundary / must not |
|---|---|---|
| **main** | Builds the app. Includes routers in this order: health, links, then redirect last, so the catch-all `/{slug}` cannot shadow real paths. In lifespan it applies the schema, checks the SQLite version, and sets the AnyIO thread limiter to `THREAD_LIMIT`. | No business logic. |
| **config** | Reads and validates env vars (see §5). Fails fast on bad values. | No I/O beyond env. |
| **db** | Opens one `sqlite3` connection per request with `timeout=BUSY_TIMEOUT_MS/1000` and `isolation_level=None`. Applies `journal_mode=WAL` (persisted), `synchronous`, `busy_timeout` and `foreign_keys=ON`. Refuses to start if `sqlite3.sqlite_version < 3.35`. | Never shares a connection across threads. Never caches connections. |
| **repository** | Owns every SQL statement. Translates a "database is locked" `OperationalError` into `StoreBusy` and a UNIQUE violation into `SlugTaken`. Its interface is in §4.1. | No HTTP types. No policy decisions such as strict vs. open. |
| **auth** | FastAPI dependency. Parses `Authorization: Bearer <key>`, computes the sha256 hex digest, looks it up, and returns `Principal(key_hash, owner_id)`. A missing or unknown key gives 401. | Never logs or stores the raw key. |
| **ratelimit** | In-memory limiter keyed by the API key's `key_hash`, not by `owner_id`. It allows `CREATE_RATE_LIMIT_PER_MIN` creates per key in a sliding 60-second window. Each key holds a deque of timestamps behind a `threading.Lock`, because sync handlers run on worker threads. Exhaustion gives 429 with `Retry-After` set to the seconds until the oldest entry leaves the window. A single process makes in-memory state correct. | Applies only to `POST /links`. One key's usage never affects another key, even when both keys belong to the same owner. Redirects are not rate-limited in the MVP. |
| **validation** | Pure function `validate_target(str) -> str`. Rules are in §7. | No DNS, no network, no blocklists. |
| **slugs** | `new_slug()` returns 7 chars drawn from `[0-9A-Za-z]` via `secrets.choice`, giving about 3.5×10¹² values. `RESERVED` is a frozenset. Generation never returns a reserved word. | No custom slugs in the MVP. |
| **routes.links** | Create: validate, generate a slug, insert, and retry up to 3 times on `SlugTaken`. If all 3 attempts collide, the response is **503** with `Retry-After: 1` and `{"detail": "slug_generation_failed"}`. Stats are owner-scoped. | Stats for a non-owned slug and for a missing slug must be indistinguishable (both 404). |
| **routes.redirect** | GET calls `repository.redirect_and_count`. HEAD calls `repository.get_active_target` with no write. On `StoreBusy` it applies `COUNT_FAIL_MODE`. | The click is committed *before* the 302 is returned. No background counting. |
| **routes.health** | `/live` is `async def` and never touches the DB, so threadpool saturation cannot fail liveness. `/ready` runs `SELECT 1` on a connection with a 50 ms timeout. | `/ready` must not run writes or migrations. |
| **cli** | Operator commands (§4.3). Uses the same `db` and `repository` modules. | Not exposed over HTTP. |
| **backup** | `cli db backup` uses `sqlite3.Connection.backup()` to write a consistent copy to a timestamped file. `scripts/backup.sh` then runs `BACKUP_COPY_CMD` to ship it off-box and prunes local copies older than 7 days. | Never copies the live `.db` or `-wal` files directly. |

## 3. Data model

There are two tables, defined in `src/app/schema.sql`.

**`links`**

| Field | Type | Notes |
|---|---|---|
| `slug` | TEXT PK | 7-char base62 |
| `target` | TEXT NOT NULL | Validated, ≤2048 chars |
| `owner_id` | TEXT NOT NULL | Logical owner. Not a FK. |
| `created_at` | TEXT NOT NULL | ISO-8601 UTC |
| `clicks` | INTEGER NOT NULL DEFAULT 0 | Monotonic counter |
| `disabled` | INTEGER NOT NULL DEFAULT 0 | 0/1, operator-controlled |

`links` also has an index on `owner_id`. It is cheap and supports operator queries and future owner listing.

**`api_keys`**

| Field | Type | Notes |
|---|---|---|
| `key_hash` | TEXT PK | sha256 hex of the raw key |
| `owner_id` | TEXT NOT NULL | One owner per key. An owner may hold several keys, which allows rotation. |
| `label` | TEXT | Operator note |
| `created_at` | TEXT NOT NULL | ISO-8601 UTC |

**Relationships**

- Many `api_keys` rows can share one `owner_id`, and many `links` rows can share that same `owner_id`.
- There is intentionally no foreign key between `links` and `api_keys`. Revoking a key (deleting its hash) must not delete or orphan-check that owner's links. Re-issuing a key for the same `owner_id` restores access to those links.

**Hot-path statements**

| Operation | Statement | Notes |
|---|---|---|
| Redirect | `UPDATE links SET clicks = clicks + 1 WHERE slug = ? AND disabled = 0 RETURNING target` | Zero rows means 404. Runs as a single-statement autocommit transaction. |
| HEAD, and the read used in open mode | `SELECT target FROM links WHERE slug = ? AND disabled = 0` | Read only, no count. |
| Create | `INSERT INTO links (slug, target, owner_id, created_at) VALUES (?,?,?,?)` | UNIQUE violation becomes `SlugTaken`. |
| Stats | `SELECT ... FROM links WHERE slug = ? AND owner_id = ?` | Owner-scoped. |

There is no migration framework. `schema.sql` is idempotent and is applied at startup and by `cli db init`. Any future change ships as an additive SQL file.

## 4. API surface

### 4.1 Repository interface

This is the swap point for a Postgres or Redis backend later.

```
create_link(slug, target, owner_id, created_at) -> Link        # raises SlugTaken
redirect_and_count(slug) -> str | None                          # raises StoreBusy
get_active_target(slug) -> str | None                           # raises StoreBusy
get_link_for_owner(slug, owner_id) -> Link | None
owner_for_key_hash(key_hash) -> str | None
set_disabled(slug, disabled: bool) -> bool
insert_api_key(key_hash, owner_id, label) / delete_api_key(key_hash) -> bool / list_api_keys()
ping(timeout_ms) -> None                                         # raises StoreBusy/StoreUnavailable
```

### 4.2 HTTP endpoints

- Authentication is `Authorization: Bearer <key>`.
- Errors use the envelope in §6.
- All JSON responses send `Cache-Control: no-store`.

| Method & path | Auth | Contract |
|---|---|---|
| `POST /links` | Key | Body `{"target": str}` → **201** `{slug, short_url, target, created_at}`. Errors: 401 bad or missing key; 422 invalid target; 429 rate-limited for this key (`Retry-After`); 503 store busy (`Retry-After: 1`); 503 `slug_generation_failed` after 3 collisions (`Retry-After: 1`). |
| `GET /{slug}` | None | **302** with `Location: target` and `Cache-Control: no-store`; the click is committed first. 404 if missing or disabled. Under lock timeout, strict mode gives **503** with `Retry-After: 1`, and open mode gives an uncounted 302. |
| `HEAD /{slug}` | None | Same status and headers as GET. Never counts. |
| `GET /links/{slug}/stats` | Key | **200** `{slug, target, clicks, created_at, disabled}` for the owner. **404** for a missing slug *or* another owner's slug. 401 for a bad key. |
| `GET /live` (`/health` alias) | None | **200** `{"status":"ok"}`. Async, no DB. |
| `GET /ready` | None | **200** `{"status":"ready"}` if `SELECT 1` succeeds within 50 ms, else **503** `{"status":"not_ready"}`. |
| `GET /docs`, `/openapi.json` | None | FastAPI-generated docs, including the click semantics from §1 and the strict/open failure contract. |

### 4.3 Operator CLI (`python -m app.cli`)

| Command | Contract |
|---|---|
| `keys issue --owner <id> [--label <text>]` | Generates a 32-byte `secrets.token_urlsafe` key, stores its sha256, and prints the raw key once. |
| `keys revoke --hash <hex>` (or `--key <raw>`) | Deletes the hash. Later requests with that key get 401. Exits non-zero if the hash is not found. |
| `keys list` | Prints `key_hash` prefix, owner_id, label and created_at. Never prints raw keys. |
| `links disable <slug>` / `links enable <slug>` | Sets `disabled`. A disabled slug redirects with 404 immediately. |
| `db init` | Applies `schema.sql` idempotently and sets WAL mode. |
| `db backup --out <dir>` | Writes a consistent copy via the `.backup` API and prints its path. |

## 5. Technology choices and configuration

| Choice | Justification |
|---|---|
| Python 3.12 + FastAPI | User's stated preference; it provides OpenAPI docs and dependency injection for auth. |
| Sync `def` handlers | `sqlite3` is blocking, so the threadpool is the honest concurrency model. |
| uvicorn, 1 worker, AnyIO limit ~32 | A single process keeps one writer and correct in-memory rate limits. The bounded thread count limits lock-queue depth. |
| stdlib `sqlite3` ≥3.35 | No ORM to learn, and `UPDATE … RETURNING` gives atomic count-and-resolve. |
| SQLite WAL + `synchronous=NORMAL` | Readers do not block the writer. The user accepts losing a few seconds of clicks on a host crash. |
| Plain `schema.sql` + repository | Easy to audit, with a contained swap path to Postgres. |
| pytest + locust/k6 | pytest comes from the scaffold. locust/k6 measure the 200 RPS gates. |
| Docker + volume | One deployable artifact on a small VM. |
| ruff | Comes from the scaffold. |

**Environment variables (validated in `config.py`)**

| Variable | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/shortener.db` | SQLite file on the volume |
| `PUBLIC_BASE_URL` | required | Prefix used to build `short_url` |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | `NORMAL` or `FULL`; benchmarked both ways |
| `BUSY_TIMEOUT_MS` | `150` | Lock wait before `StoreBusy` |
| `READY_TIMEOUT_MS` | `50` | `/ready` probe timeout |
| `COUNT_FAIL_MODE` | `strict` | `strict` (503) or `open` (uncounted 302) |
| `CREATE_RATE_LIMIT_PER_MIN` | `60` | Max `POST /links` per API key per 60-second window |
| `THREAD_LIMIT` | `32` | AnyIO threadpool size |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `BACKUP_DIR` / `BACKUP_COPY_CMD` | `/data/backups` / unset | Backup output directory and the off-box shipping command |

The commands are fixed by the scaffold:

- install: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"`
- test: `.venv/bin/python -m pytest`
- test_report: `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml`
- lint: `.venv/bin/ruff check .`
- run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`

## 6. Error handling and logging

**Error envelope.** Domain errors return `{"detail": "<code>"}` with a stable machine-readable code.

| Exception | HTTP response | `detail` | Extra headers |
|---|---|---|---|
| Missing or bad key | 401 | `invalid_api_key` | `WWW-Authenticate: Bearer` |
| Invalid target | 422 | `invalid_target` | (plus a `reason` field, e.g. `scheme`, `length`, `private_ip`) |
| Rate limit exceeded | 429 | `rate_limited` | `Retry-After` |
| Not found / not owned / disabled | 404 | `not_found` | |
| `StoreBusy` on a write | 503 | `store_busy` | `Retry-After: 1` |
| Slug retries exhausted | 503 | `slug_generation_failed` | `Retry-After: 1` |
| Unhandled exception | 500 | `internal_error` | Logged with stack trace; no internals leaked |

**Policy rules**

- A 503 always carries `Retry-After`, to avoid retry amplification.
- `StoreBusy` is handled in only one place: the redirect route applies `COUNT_FAIL_MODE`, and every other route maps it to 503.
- Pydantic body errors are normalized to `invalid_target` for `POST /links`.

**Logging**

- Format: one JSON line per request to stdout. Docker collects the logs.
- Fields: `ts`, `level`, `request_id` (taken from `X-Request-ID` or generated, and echoed back), `method`, `route` (the template, not the raw path), `slug`, `status`, `latency_ms`, `owner_id` when authenticated, and `outcome` (`counted`, `uncounted_open`, `busy_503`, `not_found`).
- Never logged: raw API keys, `Authorization` headers, or full query strings of targets. Targets are logged only as scheme and host.
- WARN events: `store_busy` events (with mode), slug collisions, and rate-limit hits. These events are the input for the 0.1% 503 gate and for lock-wait monitoring.
- Startup log: SQLite version, effective PRAGMAs, `COUNT_FAIL_MODE` and thread limit.

## 7. Security baseline

**Authentication**

- API keys are 256-bit random values issued only through the CLI.
- Only sha256 hashes are stored. Lookup uses the indexed hash; a timing-safe compare is unnecessary because the hash is the lookup key.
- Revoking a key means deleting its hash, which takes effect on the next request.

**Authorization**

- Stats are filtered by `owner_id` in SQL.
- A non-owner gets 404, which is indistinguishable from a missing slug, so slugs cannot be enumerated.

**Target validation (`validate_target`)**

1. Length ≤2048 characters, and the value parses with `urllib.parse`.
2. Scheme is exactly `http` or `https`, compared case-insensitively.
3. A non-empty host is present.
4. Hosts `localhost` and `*.localhost` are rejected, case-insensitive and ignoring a trailing dot.
5. If the host is an IP literal, it is rejected when it is private, loopback, link-local or unspecified.
   - Literals are recognized through `ipaddress`, including bracketed IPv6 and IPv4-mapped IPv6.
   - Legacy IPv4 forms are normalized first: single integer (`2130706433`), octal (`0177.0.0.1`), hex (`0x7f.1`) and short dotted forms.
6. No DNS resolution and no blocklists. Abuse is handled by the operator with `links disable`.

**Abuse controls**

- Per-API-key creation limit (`CREATE_RATE_LIMIT_PER_MIN` per 60 s).
- Operator disable takes effect immediately.
- Slugs are unguessable random values, with no sequential IDs.

**Transport and runtime**

- TLS terminates at the operator's reverse proxy, and the app listens on the private interface only.
- The container runs as a non-root user. The DB volume is the only writable mount.
- Redirect and JSON responses carry `Cache-Control: no-store`.

**Data at rest:** the DB holds no secrets beyond key hashes. Backups are shipped off-box over the operator's chosen authenticated channel.

## 8. Scaling path

The MVP ceiling is about 200 RPS of bursts on one hot slug, about 100k links, and one host with no HA. The steps below are taken in order, and each one is triggered by a measurement.

1. **Tune in place.**
   - Trigger: benchmark or monitoring shows rising lock waits or p95.
   - Benchmark `SQLITE_SYNCHRONOUS` and `THREAD_LIMIT`, and move to a larger VM with faster disk.
   - Switch `COUNT_FAIL_MODE=open` only if the 503 gate (<0.1%) is breached.
   - Add a dedicated executor for DB work only if measurements show threadpool starvation.
2. **Swap storage behind the repository.**
   - Trigger: write contention, or a need for HA or PITR.
   - Implement the §4.1 interface on Postgres. The `UPDATE … RETURNING` statement ports unchanged.
   - Migrate with a one-off export/import script from the latest `.backup`.
3. **Buffer counters.**
   - Trigger: a hot slug exceeds what a single row can absorb.
   - `redirect_and_count` becomes a Redis `INCR` plus a target cache, with batched flushes to Postgres.
   - This changes the click contract to eventually consistent, and the OpenAPI docs must say so.
4. **Go stateless and horizontal.**
   - Run multiple replicas behind the load balancer.
   - Move the rate limiter to Redis, keyed by `key_hash` with the same 60-second window semantics.
   - `/live` and `/ready` already fit orchestrator probes.

Features cut from the MVP (custom slugs, event-level analytics, dashboards) can be added as new tables and routes without touching the redirect hot path.

## 9. Rejected option (brief)

**Option B (stateless FastAPI fleet + Postgres + Redis buffered counters)** offers HA and thousands of RPS per hot slug. It was rejected for the MVP because it adds managed Postgres, Redis, a load balancer and an orchestrator for 3–10 users. It also makes counts eventually consistent unless strict synchronous writes are kept, and the stated 20/200 RPS load does not need it. The repository layer and the steps in §8 keep Option B reachable when traffic or HA needs justify it.
