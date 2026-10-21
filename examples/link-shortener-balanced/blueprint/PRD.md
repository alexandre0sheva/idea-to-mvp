# PRD.md: Invite-Only URL Shortener (MVP)

## 1. Problem Statement

A small group of invited developers needs a dependable, auditable way to turn long URLs into short links and to know exactly how many times each link was followed. Existing hosted shorteners are opaque about how they count clicks, mix in analytics that nobody asked for, and change behavior without notice.

This MVP is a single-node HTTP API that does three things well:

1. **Create.** Authenticated developers create short links.
2. **Redirect.** Anyone can follow a short link. Each followed redirect is counted atomically before the redirect is returned.
3. **Report.** Link owners, and only owners, can read the click count.

Failure behavior is an explicit, documented API contract that operators switch through configuration. It is never a hidden fallback.

**Definition of a "click":** one committed `GET /{slug}` redirect, which means the counter increment was committed to the database and a `302` was returned. Clicks include bots, crawlers, and client retries. They are not unique visitors. `HEAD` requests are never counted.

**Stack (fixed by user preference):** Python 3.12, FastAPI with sync `def` handlers, stdlib `sqlite3` (SQLite ≥ 3.35), one uvicorn worker, and Docker. This builds on the existing `python-fastapi` scaffold: the `app` package lives in `src/app/`, tests live in `tests/`, and the project uses pytest and ruff.

---

## 2. Target Users and Personas

| Persona | Who | Primary needs |
|---|---|---|
| **Dana, Integrator Developer** (primary) | One of 3–10 invited developers. She calls the API from her own scripts and services. | Create short links programmatically, get stable short URLs, and read exact click counts for her own links only. |
| **Oscar, Operator** (primary) | Runs the single VM or container. Issues keys and handles abuse. | Issue and revoke API keys, disable abusive links, take backups, check health, and tune the failure mode. |
| **Vic, Visitor** (secondary, anonymous) | Anyone who clicks a short link. | Fast, correct redirects with no authentication. |

**Expected scale for the first 3 months:**

| Dimension | Expected value |
|---|---|
| Keys and owners | 3–10 keys, each mapped to one `owner_id` |
| Total links | Up to about 100k |
| Average redirect rate | 20 redirects/s |
| Burst redirect rate | 200 redirects/s on a single hot link |
| Tolerated 503 rate | Under 0.1% |

---

## 3. User Stories

### Integrator (Dana)
- **US1, create link:** As an integrator, I want to `POST` a target URL with my API key and receive a short URL, so that I can share compact links from my product.
- **US2, read stats:** As an integrator, I want to fetch the click count for a link I own, so that I can measure how often it is followed.
- **US3, owner privacy:** As an integrator, I want other key holders to be unable to see that my links' stats exist, so that my usage data stays private.
- **US4, clear errors:** As an integrator, I want precise error codes for invalid targets, bad keys, and rate limiting, so that my client can handle failures programmatically.
- **US5, documented semantics:** As an integrator, I want the OpenAPI docs to define exactly what a "click" is and how failure modes behave, so that I can interpret the numbers correctly.
- **US6, retry guidance:** As an integrator, I want a `Retry-After` header on transient failures, so that my clients back off instead of amplifying load.

### Visitor (Vic)
- **US7, redirect:** As a visitor, I want a short link to redirect me to its target immediately, so that I reach the content without friction.
- **US8, link checking:** As a link-preview tool or monitor, I want `HEAD /{slug}` to report the redirect without counting it, so that checks do not inflate stats.
- **US9, dead links:** As a visitor, I want unknown or disabled links to return a clear 404, so that I am not sent to removed or abusive content.

### Operator (Oscar)
- **US10, issue key:** As an operator, I want to issue an API key for an `owner_id` from a CLI and see the plaintext key exactly once, so that only hashes are stored.
- **US11, revoke key:** As an operator, I want to revoke a key from the CLI with immediate effect, so that I can cut off a misbehaving integrator.
- **US12, disable/enable link:** As an operator, I want to disable and re-enable a link from the CLI, so that I can stop phishing or abuse without deleting data.
- **US13, backup:** As an operator, I want to take a consistent online backup of the database to a file, so that I can copy it off-box nightly and restore after a failure.
- **US14, health checks:** As an operator, I want separate liveness and readiness endpoints, so that a slow database does not cause my orchestrator to restart a healthy process.
- **US15, failure mode:** As an operator, I want to choose between strict and open counting through configuration, so that I can trade count accuracy against availability based on benchmark results.
- **US16, abuse controls:** As an operator, I want per-key creation rate limits, so that one key cannot flood the database with links.
- **US17, deploy:** As an operator, I want a Docker image and compose file with a persistent DB volume, so that I can run the service on a single small VM.

---

## 4. Functional Requirements

Priorities:
- **P0** is required for MVP and must be covered by automated tests.
- **P1** is required for launch readiness but may be verified by scripts, benchmarks, or manual review.

### 4.1 Configuration and Storage

R1 (P0): The service reads configuration from environment variables with these defaults: `DATABASE_PATH=./data/shortener.db`, `BASE_URL=http://127.0.0.1:8000`, `COUNT_FAIL_MODE=strict`, `SQLITE_SYNCHRONOUS=NORMAL`, `SQLITE_BUSY_TIMEOUT_MS=150`, `CREATE_RATE_LIMIT_PER_MIN=60`. Tests can override each value, and an invalid `COUNT_FAIL_MODE` (anything other than `strict` or `open`) or an invalid `SQLITE_SYNCHRONOUS` (anything other than `NORMAL` or `FULL`) causes startup to fail with a clear error.

R2 (P0): The schema is defined in a plain SQL file (`src/app/schema.sql`) and applied idempotently at startup with `CREATE TABLE IF NOT EXISTS`. It defines:
- `links(slug TEXT PRIMARY KEY, target TEXT NOT NULL, owner_id TEXT NOT NULL, created_at TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0)`
- `api_keys(key_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_at TEXT NOT NULL)`

Starting the app twice against the same database file succeeds and preserves existing data. The parent directory of `DATABASE_PATH` is created if missing.

R3 (P0): All database access goes through a repository module (`src/app/repository.py`). Route handlers do not execute SQL directly. Each request opens a short-lived `sqlite3` connection and closes it before the response completes. Every connection sets `journal_mode=WAL`, `synchronous` to the configured value, and `busy_timeout` to the configured milliseconds. A test verifies these values by reading the pragmas on a repository-created connection.

R4 (P0): At startup the service verifies `sqlite3.sqlite_version >= 3.35.0` and refuses to start with a clear error otherwise. A test monkeypatches the version to trigger the error.

R5 (P0): `created_at` values are stored and returned as ISO 8601 UTC timestamps with a `Z` or `+00:00` suffix.

### 4.2 Authentication

R6 (P0): Authenticated endpoints accept the API key only as `Authorization: Bearer <key>`. The service computes the SHA-256 hex digest of the presented key and looks it up in `api_keys.key_hash`. A missing, malformed, or unknown key returns `401` with a JSON body `{"detail": "unauthorized"}` and a `WWW-Authenticate: Bearer` header.

R7 (P0): Plaintext API keys are never stored in the database. A test issues a key and asserts that the plaintext does not appear in any `api_keys` column.

R8 (P0): Key lookups are not cached. When a key's hash is deleted, the next request using that key returns `401`.

### 4.3 Link Creation: `POST /links`

R9 (P0): `POST /links` requires authentication (R6) and accepts the JSON body `{"target": "<url>"}`. Unknown fields, including `slug`, are rejected with `422`. On success it returns `201` with:
- the JSON body `{"slug", "short_url", "target", "created_at"}`, where `short_url` equals `BASE_URL` with any trailing slash stripped, followed by `/` and the slug
- a `Location` header equal to `short_url`

The new row stores the caller's `owner_id`, `clicks=0`, and `disabled=0`.

R10 (P0): Slugs are exactly 7 characters from the base62 alphabet `[0-9A-Za-z]`, generated with the `secrets` module. Each `POST` creates a new slug, even when the same target was submitted before.

R11 (P0): Slug uniqueness is enforced by the primary key. On an insert collision the service generates a new slug and retries, for at most 3 total attempts. If all 3 attempts collide, it returns `503` with `Retry-After: 1` and `{"detail": "slug_generation_failed"}`. Tests force collisions by monkeypatching the generator, covering two cases: one collision followed by success, and three collisions resulting in 503.

R12 (P0): Generated slugs that match a reserved word, compared case-insensitively, are discarded and regenerated without consuming a collision retry. The reserved set is `links`, `live`, `ready`, `health`, `docs`, `redoc`, `openapi`, `favicon`, `static`, `admin`, and `api`. A test monkeypatches the generator to yield `"openapi"` and then a valid slug, and asserts the valid slug is returned.

R13 (P0): Target validation rejects the request with `422` and a `detail` naming the failed rule when any of the following is true:
- (a) the scheme, compared case-insensitively, is not `http` or `https`
- (b) the URL is longer than 2048 characters (a 2048-character URL is accepted, 2049 is rejected)
- (c) the host is missing or empty
- (d) the URL contains whitespace or ASCII control characters
- (e) the host is `localhost` or ends with `.localhost`, compared case-insensitively

R14 (P0): Target validation rejects IP-literal hosts that are private, loopback, link-local, or unspecified, using Python's `ipaddress` module. No DNS resolution is performed.

| Form | Rejected examples |
|---|---|
| IPv4 dotted decimal | `127.0.0.1`, `10.0.0.1`, `192.168.1.1`, `172.16.0.1`, `169.254.1.1`, `0.0.0.0` |
| Legacy IPv4 forms, normalized the way `socket.inet_aton` parses them | integer `2130706433`, octal `0177.0.0.1`, hex `0x7f.0.0.1`, short `127.1` |
| Bracketed IPv6 | `[::1]`, `[fe80::1]`, `[fc00::1]`, `[::ffff:127.0.0.1]` |

Public literals such as `8.8.8.8` and `[2606:4700::1111]` are accepted. Hostnames such as `example.com` are accepted without lookup.

R15 (P0): Valid targets are stored and later returned in `Location` exactly as submitted. No normalization is applied.

R16 (P0): Link creation is rate-limited per API key using an in-memory sliding or fixed 60-second window. The limit is `CREATE_RATE_LIMIT_PER_MIN`. Requests over the limit return `429` with a `Retry-After` header (integer seconds ≥ 1) and `{"detail": "rate_limited"}`, and no row is inserted. Limits for one key do not affect another key.

### 4.4 Redirect: `GET /{slug}` and `HEAD /{slug}`

R17 (P0): `GET /{slug}` for an existing, enabled link returns `302` with `Location` set to the stored target and `Cache-Control: no-store`. It increments `clicks` by exactly 1 using a single atomic statement before the response is sent: `UPDATE links SET clicks = clicks + 1 WHERE slug = ? AND disabled = 0 RETURNING target`. No authentication is required.

R18 (P0): `GET /{slug}` returns `404` with `{"detail": "not_found"}` and does not change any counter when any of the following is true:
- the slug does not exist
- the link is disabled
- the path segment does not match `^[0-9A-Za-z]{7}$`

Slugs are case-sensitive, so a lowercased variant of an existing mixed-case slug that does not itself exist returns 404.

R19 (P0): `HEAD /{slug}` returns the same status and headers as `GET` (`302`, `Location`, and `Cache-Control: no-store` for enabled links, `404` otherwise), with no body, and never changes `clicks`.

R20 (P0): Concurrent counting is exact. When N concurrent `GET /{slug}` requests all return `302` (a test uses N ≥ 50 across ≥ 8 threads against a file-backed database), the stored `clicks` equals N. More generally, after any mix of outcomes, stored `clicks` equals the number of `302` responses returned for that slug.

### 4.5 Count Failure Modes

R21 (P0): In `COUNT_FAIL_MODE=strict` (the default), a redirect whose counter `UPDATE` fails with a lock or busy error returns `503` with `Retry-After: 1` and `{"detail": "count_unavailable"}`. This includes `sqlite3.OperationalError` "database is locked" after `busy_timeout`. The request issues no redirect and adds no count. A test holds a `BEGIN IMMEDIATE` write lock on a separate connection, or injects the error at the repository, and asserts this behavior.

R22 (P0): In `COUNT_FAIL_MODE=open`, a redirect whose counter `UPDATE` fails with a lock or busy error falls back to a read-only lookup (`SELECT target FROM links WHERE slug=? AND disabled=0`). The outcome depends on the lookup:

| Lookup result | Response | Count |
|---|---|---|
| Finds the link | `302` with `Location` and `Cache-Control: no-store` | Not counted |
| Finds nothing | `404` | None |
| Also fails | `503` with `Retry-After: 1` | None |

R23 (P0): Lock or busy errors on `POST /links` and on stats return `503` with `Retry-After: 1` in both modes.

### 4.6 Owner Stats: `GET /links/{slug}/stats`

R24 (P0): `GET /links/{slug}/stats` requires authentication (R6). For a link owned by the caller's `owner_id`, it returns `200` with `{"slug", "target", "clicks", "created_at", "disabled"}`, where `disabled` is a boolean. This works for disabled links too, and the response reflects their counts and `disabled: true`.

R25 (P0): Stats for a slug that does not exist, or that is owned by a different `owner_id`, return `404` with `{"detail": "not_found"}`. The two responses are indistinguishable in status and body. A missing or invalid key returns `401` (R6) before any ownership check.

### 4.7 Documentation

R26 (P0): The OpenAPI schema at `/openapi.json` documents every endpoint and response code listed in this PRD. The descriptions for `GET /{slug}` and for the stats endpoint state:
- a click is a committed `GET` redirect
- clicks include bots and retries
- clicks are not unique visitors
- `HEAD` is not counted
- in `open` mode, stats are a lower bound

A test asserts that the description text contains the words "click", "bots", "HEAD", and "lower bound".

### 4.8 Health Endpoints

R27 (P0): `GET /live` is an `async` handler that returns `200` with `{"status": "ok"}` and performs no database access. A test points `DATABASE_PATH` at an unusable location, or patches the repository to raise, and asserts that `/live` still returns `200`. The scaffold's existing `GET /health` route is kept and behaves identically to `/live`.

R28 (P0): `GET /ready` runs `SELECT 1` against the database with a 50 ms budget. It returns `200` with `{"status": "ready"}` on success. On error or timeout it returns `503` with `{"status": "unavailable"}`. A test injects a repository failure and asserts the `503`.

### 4.9 Operator CLI (`python -m app.cli`)

R29 (P0): `keys create --owner <owner_id>` generates a key with `secrets.token_urlsafe(32)`, stores only its SHA-256 hash with the `owner_id` and `created_at`, prints the plaintext key to stdout exactly once, and exits 0. The printed key authenticates successfully against `POST /links`.

R30 (P0): `keys list` prints one line per key containing a key ID (the first 12 hex characters of the hash), the `owner_id`, and `created_at`. It never prints plaintext keys or full hashes.

R31 (P0): The `keys revoke` command deletes key hashes from `api_keys` and accepts either of two options:
- `--key-id <prefix>` deletes the single key whose hash starts with the prefix. An ambiguous or unmatched prefix exits non-zero and deletes nothing.
- `--owner <owner_id>` deletes all keys for that owner.

Revoked keys return `401` on their next request. The owner's existing links and counts are kept.

R32 (P0): `links disable <slug>` sets `disabled=1` and `links enable <slug>` sets `disabled=0`. Both exit 0 on success. An unknown slug exits non-zero. After `disable`, `GET` and `HEAD` return `404` and do not count. After `enable`, redirects and counting resume, and the prior click count is preserved.

R33 (P0): `backup --dest <path>` writes a consistent copy of the live database using Python's `sqlite3.Connection.backup` API while the service may be running. A test asserts three things about the resulting file: it opens, `PRAGMA integrity_check` returns `ok`, and it contains the same `links` and `api_keys` row counts as the source.

R34 (P1): `scripts/backup.sh` runs the CLI backup to a timestamped file and then, if `BACKUP_COPY_CMD` is set, runs it with the backup file path as an argument to copy it off-box. The README documents a nightly cron example and the restore procedure: stop the service, replace the DB file, and start the service.

### 4.10 Runtime, Deployment, and Observability

R35 (P1): At startup the app sets the AnyIO default thread limiter to 32 tokens, configurable through `THREAD_LIMIT`. Route handlers that touch the database are sync `def` functions.

R36 (P1): A `Dockerfile` and `docker-compose.yml` run the service with exactly one uvicorn worker, mount a named persistent volume at the directory containing `DATABASE_PATH`, and expose a container healthcheck that uses `/live`.

R37 (P1): The service writes one access log line per request containing the method, path, status, and latency in ms. API keys and `Authorization` header values never appear in logs.

R38 (P1): The README documents setup using the project's fixed commands, every environment variable from R1, R35, and R34, the CLI usage, the click semantics, the strict and open modes, and the backup and restore steps.

### 4.11 Verification Tooling

R39 (P1): Load-test scripts (locust or k6, stored under `bench/`) cover three scenarios:
- (a) 200 RPS for 60 s on one hot slug
- (b) 200 RPS for 60 s spread across 100 slugs
- (c) a 10× burst of 2000 RPS for 10 s on one slug

Each run reports p95 latency, the 503 rate, the total number of `302` responses, and `/live` p95 latency during the run, and compares the reported `302` total to the stored `clicks`.

R40 (P1): Every scenario in R39 is run with `SQLITE_SYNCHRONOUS=NORMAL` and with `FULL`. The results are recorded in `bench/RESULTS.md`, together with the resulting decision on `COUNT_FAIL_MODE`.

R41 (P1): A `kill -9` reconciliation script performs these steps:
1. Drives redirect traffic and records every client-observed `302`.
2. Sends `SIGKILL` to the server mid-run.
3. Restarts the server.
4. Asserts that each slug's stored `clicks` is greater than or equal to its client-observed `302` count.

R42 (P1): A URL-validation fuzz test generates at least 1000 random and adversarial target strings, including mixed-case schemes, encoded hosts, IPv4 legacy forms, IPv6 forms, and over-length inputs. It asserts that the validator never raises an unhandled exception and never accepts any non-http(s), localhost, or private, loopback, link-local, or unspecified literal.

---

## 5. API Contract Summary

| Method & Path | Auth | Success | Errors |
|---|---|---|---|
| `POST /links` | Bearer | `201` + JSON + `Location` | `401`, `422`, `429`, `503` |
| `GET /{slug}` | none | `302` + `Location` + `Cache-Control: no-store` (counted) | `404`, `503` |
| `HEAD /{slug}` | none | `302` same headers (not counted) | `404` |
| `GET /links/{slug}/stats` | Bearer | `200` JSON | `401`, `404`, `503` |
| `GET /live`, `GET /health` | none | `200` | — |
| `GET /ready` | none | `200` | `503` |

All error bodies are JSON with a `detail` field, or `status` for the health endpoints.

---

## 6. Non-Goals / Out of Scope

These are explicitly not built in the MVP. Do not implement them.

- Custom or vanity slugs, and user-chosen slugs in any form.
- Link editing, deletion through the API, or link expiration/TTL.
- Owner-initiated disabling through the API. Disabling is CLI-only.
- Dashboards, web UI, or any HTML pages.
- Per-click event tables, referrer, geo, or user-agent analytics, unique-visitor counting, and bot filtering.
- Background, batched, or asynchronous counting (Redis, queues, write-behind).
- DNS resolution of target hosts, domain blocklists, Safe Browsing or reputation lookups, and link previews.
- Self-service signup, API key self-rotation, OAuth, and user accounts.
- Billing, quotas beyond creation rate limits, and pricing tiers.
- Rate limiting of redirects.
- Multiple workers, multiple nodes, high availability, Postgres, and Redis. The repository layer keeps these swappable later.
- Custom domains per owner and TLS termination. TLS is handled by the host or proxy if needed.
- Link listing endpoints, such as listing all of an owner's links.

---

## 7. Success Metrics

### Launch gates
Measured by R39–R41 on the target VM-class host.

| ID | Metric | Target |
|---|---|---|
| M1 | Count exactness | Stored `clicks` equals client-observed `302` count for scenarios (a) and (b), with zero discrepancy. |
| M2 | Redirect latency | p95 below 200 ms at 200 RPS in scenarios (a) and (b). |
| M3 | Availability under strict mode | 503 rate below 0.1% in scenarios (a) and (b). If breached under `NORMAL`, switch to `open` and document it. |
| M4 | Health responsiveness | `/live` p95 below 50 ms during all scenarios, including the 10× burst. The service recovers to M2 levels within 10 s after the burst ends. |
| M5 | Crash safety | `kill -9` reconciliation passes, meaning stored ≥ observed for every slug. |
| M6 | Test quality | All P0 requirements are covered by passing pytest tests, `ruff check` is clean, and line coverage of the `app` package is at least 85%. |

### Adoption (first 4–8 weeks)

| ID | Metric | Target |
|---|---|---|
| M7 | Integrator onboarding | 3 integrators onboarded, each creating links through the API in at least 3 separate weeks. |
| M8 | Semantics acceptance | Zero integrator-reported disputes about count semantics or failure behavior, confirmed in a check-in. |
| M9 | Abuse response | Time from abuse report to link disable under 15 minutes during operator hours. |
| M10 | Data durability | Nightly off-box backups succeed on at least 95% of nights, with one successful restore drill. |

---

## 8. Open Product Questions

1. **Failure mode default:** Should production stay on `strict`? The decision is pending the R39/R40 benchmark results and is flipped to `open` only if M3 is breached.
2. **`synchronous` mode:** `NORMAL` is accepted, given a tolerated loss of a few seconds of clicks on host crash. Does the `FULL` benchmark show headroom large enough to prefer it anyway?
3. **Rate limit value:** Is 60 creations per minute per key right for the integrators' real workflows? Should the limit be configurable per key?
4. **Burst gate:** Should the 10× burst (2000 RPS) carry hard latency or 503 gates, or remain observational with only the M4 health criterion?
5. **Key lifecycle:** Do integrators need multiple concurrent keys per owner for rotation? The CLI supports this, but the process is undefined. Is key expiry needed?
6. **Link lifecycle:** Should disabled links ever be purged? What retention applies to links of revoked owners?
7. **Public hostname:** What `BASE_URL` and domain will be used? Who terminates TLS (reverse proxy vs. platform)? What is the plan if the domain is blacklisted for phishing?
8. **Abuse intake:** How will third parties report abusive links to the operator, for example through an abuse email address listed in the README?
9. **Stats granularity:** Will integrators need time-bucketed counts, such as per day? That would require an event table, which is currently out of scope.
10. **Scale trigger:** At what observed peak RPS or link count should migration to Option B (Postgres + Redis, multiple replicas) begin?
