# contracts/AGENTS.md: Contract Governance for Parallel Agents

This file explains how agents create, use, and change data contracts in the URL shortener MVP: FastAPI + SQLite, single node, Option A.

When this file disagrees with code, **the contract wins**. Fix the code, or change the contract using the process below. Do not let the two drift.

`ARCHITECTURE.md` describes the design. This directory turns that design into **binding interfaces**. If a contract here differs from `ARCHITECTURE.md`, stop and raise it with the lead. Do not pick one silently.

---

## 1. Core rules

1. **Contract first.** Do not implement or consume an interface until its contract file exists and its status is `frozen` or `draft-approved`.
2. **One owner per contract file.** Only the owner edits it. Everyone else files a change request (§7).
3. **Code against the contract, never against another agent's implementation.** If you need something the contract does not promise, request it.
4. **Within MVP v1, changes are additive only.** Breaking changes need an explicit lead decision and a major version bump (§5).
5. **Invariants are untouchable** (§3). No agent may weaken them, including through config defaults.
6. **Every contract has a test.** A contract without a test in `tests/contract/` is not frozen.
7. **No hidden behavior.** Fallbacks, retries, and degraded modes must appear in a contract. `COUNT_FAIL_MODE` is the model to follow: an explicit switch with documented semantics.

---

## 2. Ownership

Execution mode is `agent_team`. The lead agent owns `core-api-storage` and therefore owns every shared, cross-cutting contract.

| Contract file | Owner | Primary consumers | Source of truth in code |
|---|---|---|---|
| `contracts/http-api.md` | core-api-storage (lead) | tests-benchmarks, ops (README) | `src/app/routes/links.py`, `routes/redirect.py`, OpenAPI |
| `contracts/errors.md` | core-api-storage (lead) | all | `src/app/errors.py` |
| `contracts/schema.md` | core-api-storage (lead) | ops (CLI, backup), tests | `src/app/schema.sql` |
| `contracts/repository.md` | core-api-storage (lead) | ops (CLI), routes, tests | `src/app/repository.py`, `models.py` |
| `contracts/config.md` | core-api-storage (lead) | ops (Docker/compose), tests | `src/app/config.py` |
| `contracts/health.md` | ops-cli-deploy | tests-benchmarks, deploy | `src/app/routes/health.py` |
| `contracts/cli.md` | ops-cli-deploy | operators, tests | `src/app/cli.py` |
| `contracts/backup.md` | ops-cli-deploy | operators | `cli db backup`, `scripts/backup.sh` |
| `contracts/bench.md` | tests-benchmarks | lead (gate decisions) | `bench/`, `docs/BENCHMARKS.md` |
| `contracts/CHANGELOG.md` | lead (append-only; any agent may append a proposal) | all | — |

Ownership rules:

- Non-owners do **not** edit owned files. Put proposals in `CHANGELOG.md` under `## Proposed`.
- Ops needs new config vars (for example, backup paths). Ops proposes them, and the lead adds them to `config.md` and `config.py`. Ops must not read `os.environ` directly.
- Ops needs new data access for the CLI. Ops proposes a repository method, and the lead adds it. **The CLI never issues raw SQL.**
- Tests-benchmarks owns test fixtures and bench output formats. It must not change product behavior to make a test pass.

---

## 3. Invariants (frozen for the life of v1)

Breaking any of these fails review automatically.

| # | Invariant |
|---|---|
| I1 | `GET /{slug}` commits the click **before** returning 302. No background or deferred counting. |
| I2 | The counter is a single statement: `UPDATE links SET clicks = clicks + 1 WHERE slug = ? AND disabled = 0 RETURNING target`. |
| I3 | `HEAD /{slug}` never writes. |
| I4 | A click is one committed redirect attempt. Bots, retries, and prefetchers count. There are no unique-visitor semantics. In `open` mode, counts are a documented lower bound. |
| I5 | `GET /links/{slug}/stats` returns byte-identical 404 responses for "missing" and "owned by someone else". |
| I6 | Every 503 carries `Retry-After: 1`. Every redirect and JSON response carries `Cache-Control: no-store`. |
| I7 | Raw API keys are never stored, logged, or returned after issuance. Only the `sha256` hex digest is persisted. |
| I8 | There is no FK between `links` and `api_keys`. Revoking a key never touches links. |
| I9 | `repository.py` is the **only** module that issues SQL. It returns dataclasses and contains no HTTP types or policy. |
| I10 | `/live` never touches the DB. `/ready` never writes or migrates. |
| I11 | The redirect router is registered **last**. Fixed paths always win over `/{slug}`. |
| I12 | Crash reconciliation: stored `clicks` ≥ client-observed 302s. |
| I13 | Target validation is pure: no DNS, no network, no blocklists. Rules: http/https only, ≤2048 chars, reject `localhost` and private/loopback/link-local IP literals (including octal and integer forms) via `ipaddress`. |

---

## 4. Boundaries

### 4.1 HTTP API boundary (external, public)

- Paths are **unversioned**. The catch-all `/{slug}` makes a `/v1` prefix pointless for redirects. API evolution is additive only.
- The frozen v1 surface is `POST /links`, `GET /{slug}`, `HEAD /{slug}`, `GET /links/{slug}/stats`, `GET /live` (with alias `/health`), and `GET /ready`.
- For each endpoint the contract specifies: method, path, auth, request schema, every status code, required headers, response schema, and side effects (counts or does not count).
- OpenAPI generated by FastAPI must match `http-api.md`. Click semantics (I4) must appear in the endpoint descriptions.
- Errors use the envelope defined in `errors.md`, which is the canonical copy of ARCHITECTURE §6. Error `code` strings are part of the contract. Clients may branch on them.

### 4.2 Storage boundary (internal)

- `schema.md` mirrors `src/app/schema.sql` column by column. `repository.md` lists every method signature, return type, and raised exception (`StoreBusy`, `StoreUnavailable`, `SlugTaken`).
- Routes and CLI depend on `repository.md`, **never** on table layout. This makes the repository interface the Postgres/Redis swap point. Keep it backend-neutral, with no SQLite types leaking into signatures.
- Policy, such as strict vs. open mode or retry counts, lives in routes. The repository only reports facts.

### 4.3 Config boundary

- `config.md` lists every env var with its type, default, allowed values, and validation. Known vars include `COUNT_FAIL_MODE` (`strict|open`, default `strict`), `BUSY_TIMEOUT_MS` (150), `THREAD_LIMIT` (32), `CREATE_RATE_PER_MIN`, `CREATE_BURST`, and `BACKUP_COPY_CMD`. The full list is in ARCHITECTURE §5.
- Invalid config fails at startup. Changing a default counts as a **behavior change** and needs a CHANGELOG entry.

### 4.4 Operator boundary (CLI, backup)

- `cli.md` specifies command names, args, stdout format, and exit codes. Scripts and operators depend on these.
- `key issue` prints the raw key **once** to stdout. Nothing else ever prints it.
- `backup.md` specifies that backups use the `sqlite3` `.backup()` API only. It also covers the timestamped filename pattern, the 7-day local retention, and the `BACKUP_COPY_CMD` hook.

### 4.5 Events boundary

- **There are no events in the MVP.** Event tables, queues, and background counting are explicitly cut.
- JSON request logs are diagnostic, not a contract. Do not build features that parse them.
- Adding any event or async pipeline requires a new contract file, a major-version decision, and a re-check of I1.

---

## 5. Versioning

Every contract file starts with a header (§10). Contract versions use semver:

| Change | Bump | Examples |
|---|---|---|
| Clarification only, no behavior change | patch `1.0.x` | Typo fix, added example |
| Additive, backward compatible | minor `1.x.0` | New optional response field, new endpoint, new CLI subcommand, new nullable/defaulted column, new error code on a **new** path |
| Breaking | major `2.0.0` | See §6.2 |

- **Schema version** is tracked with `PRAGMA user_version`. `schema.sql` sets it to `1`. Each additive migration file increments it by exactly 1. `db.py` refuses to start if the DB's `user_version` is higher than the code knows about.
- Lifecycle for each file: `draft` → `draft-approved` (consumers may start) → `frozen` (contract test passing) → `deprecated` (with replacement noted).
- No major bumps happen during the MVP without explicit user or lead sign-off recorded in `CHANGELOG.md`.

---

## 6. Compatibility rules

### 6.1 Allowed without a major bump

- Adding optional request fields with defaults, or new response fields. Clients must ignore unknown fields.
- Adding endpoints on **fixed paths**, but only after the reserved-slug check in §6.3.
- Adding nullable columns, or columns with a constant `DEFAULT`, via `ALTER TABLE ... ADD COLUMN`.
- Adding indexes.
- Adding repository methods. Existing signatures stay unchanged.
- Adding CLI subcommands or optional flags.

### 6.2 Breaking (forbidden in v1 without sign-off)

- Removing or renaming any field, path, column, repository method, CLI command, env var, or error code.
- Changing a status code, header, or response shape for an existing case.
- Changing click semantics (I4) or count timing (I1).
- Making an optional request field required, or tightening validation in a way that would reject targets already accepted, without a decision about existing links.
- Changing the slug alphabet or length so that **existing slugs stop resolving**. New slugs may use a different generator only if old slugs still resolve.
- Changing the key-hashing scheme, since that invalidates every issued key. If it must change, it needs dual-read support and a reissue plan.
- Changing a config default that alters failure behavior, such as `COUNT_FAIL_MODE`.

### 6.3 Reserved paths vs. existing slugs

Fixed routes shadow `/{slug}`, so adding a fixed route can hijack an existing link. Before you add any new top-level path:

1. Add the path segment to `slugs.RESERVED` in the same change.
2. Check the production DB (or the latest backup) for an existing slug with that value. In the MVP slugs are exactly 7 chars, so only 7-char paths can collide, but check anyway.
3. If a collision exists, stop and escalate. Do not ship.

---

## 7. Change protocol (parallel-safe)

1. **Propose.** Append an entry to `contracts/CHANGELOG.md` under `## Proposed`: requester, file, change, reason, bump type, and affected consumers.
2. **Decide.** The owner accepts or rejects it, records the decision, and moves it to a version heading. Breaking changes need lead approval and the reason.
3. **Contract first.** The owner edits the contract file and bumps the version **in a separate commit before code**.
4. **Implement.** The owner changes code and contract tests together. The change must be a single PR or commit series that passes `.venv/bin/ruff check .` and `.venv/bin/python -m pytest`.
5. **Notify.** Consumers update against the new version. Until they do, the previous behavior must still work. Additive changes guarantee this.
6. **Never** edit a frozen contract in the same commit as an unrelated feature.

Conflict rule: if two agents need incompatible changes to the same contract, the lead decides. Neither agent works around the contract in the meantime.

---

## 8. Migration protocol (SQLite)

The MVP has no migration framework. `schema.sql` is idempotent (`CREATE ... IF NOT EXISTS`) and runs at lifespan startup and in `cli db init`.

For any change after v1:

1. Create `src/app/migrations/NNN_<description>.sql`, with `NNN` zero-padded and strictly increasing.
2. Use additive DDL only: `ADD COLUMN` (nullable or with a constant default), `CREATE INDEX IF NOT EXISTS`, or `CREATE TABLE IF NOT EXISTS`. **Never** `DROP`, rename, or rebuild a table in v1.
3. Guard the file so it is idempotent: apply it only if `PRAGMA user_version` is below `NNN`, then set `user_version = NNN` in the same transaction.
4. Apply it at startup in lifespan (after `schema.sql`) and in `cli db init`. Never apply it from `/ready` or from request handlers.
5. The operator takes a `cli db backup` before deploying any migration. The migration PR must state this in its description.
6. Test it with (a) a fresh DB and (b) a DB at the previous `user_version` that contains rows. Both must pass the full suite.
7. Keep migrations short. Long locks on `links` break the 150 ms busy timeout and the 503 gate. Index builds over about 100k rows must be measured in the PR.
8. Update `schema.md`, `repository.md` if affected, and `CHANGELOG.md` in the same change.

Data backfills, if they are ever needed, run as an explicit CLI command in small batches. They never run at startup.

---

## 9. Required contract artifacts

These files must exist before the corresponding workstream starts consuming. The lead freezes the core set in the first task.

| Artifact | Required content | Frozen by |
|---|---|---|
| `contracts/http-api.md` | Every endpoint per §4.1, plus click semantics, 404 indistinguishability, and strict/open behavior | T01 (lead) |
| `contracts/errors.md` | Envelope shape, `code` registry (code → status → meaning), header rules (I6) | T01 (lead) |
| `contracts/schema.md` | Tables, columns, types, defaults, index, `user_version`, the no-FK rationale | T01 (lead) |
| `contracts/repository.md` | Method signatures, return dataclasses, exceptions, transaction semantics per method | T01 (lead) |
| `contracts/config.md` | Env var table, defaults, validation, behavior impact | T01 (lead) |
| `contracts/health.md` | `/live`, `/health` alias, `/ready` (50 ms), response bodies | ops |
| `contracts/cli.md` | Commands (`key issue/revoke/list`, `link disable/enable`, `db init`, `db backup`), args, output, exit codes | ops |
| `contracts/backup.md` | Method, filenames, retention, copy hook, restore steps | ops |
| `contracts/bench.md` | Scenarios (200 RPS hot slug, 200 RPS across 100 slugs, 10× burst), gates (exact counts, p95 < 200 ms, 503 < 0.1%, responsive health), report format, FULL vs. NORMAL comparison | tests-benchmarks |
| `contracts/CHANGELOG.md` | Proposed / accepted / rejected entries, newest first | lead |
| `tests/contract/test_*.py` | At least one test per contract file asserting its normative statements and invariants I1–I13 | owner + tests-benchmarks |

---

## 10. Review checklist

Run this on every PR that touches `contracts/`, `schema.sql`, `migrations/`, `repository.py`, `config.py`, `errors.py`, any route, or `cli.py`:

- [ ] The author owns the touched contract file, or a CHANGELOG entry approves the change.
- [ ] The contract file version was bumped correctly (§5) and the header is updated.
- [ ] The change is additive, or the major bump has recorded sign-off (§6).
- [ ] Invariants I1–I13 are intact. Contract tests for them pass.
- [ ] No SQL outside `repository.py`, and no `os.environ` reads outside `config.py`.
- [ ] Any new top-level path is in `RESERVED` and has been checked against existing slugs (§6.3).
- [ ] Every new error case has a `code` in `errors.md`, the correct status, and `Retry-After` when 503 or 429.
- [ ] OpenAPI output matches `http-api.md`, including the click-semantics text.
- [ ] Any migration follows §8: idempotent, `user_version` bumped, tested on fresh and previous DBs, backup noted.
- [ ] No raw API key appears in logs, errors, fixtures, or committed files.
- [ ] Any config default change is called out as a behavior change.
- [ ] `.venv/bin/ruff check .` and `.venv/bin/python -m pytest` pass.
- [ ] If the change touches the hot path: a benchmark rerun is scheduled, or the PR shows why it is not needed.

---

## 11. Contract file template

Copy this template for each new contract or endpoint section. The example is filled in for `GET /{slug}`.

```markdown
# Contract: Redirect

- **File:** contracts/http-api.md § Redirect
- **Version:** 1.0.0
- **Status:** frozen            <!-- draft | draft-approved | frozen | deprecated -->
- **Owner:** core-api-storage (lead)
- **Consumers:** tests-benchmarks, bench/, README
- **Code:** src/app/routes/redirect.py, repository.redirect_and_count / get_active_target
- **Contract tests:** tests/contract/test_redirect.py
- **Invariants:** I1, I2, I3, I4, I6, I11

## Interface
`GET /{slug}` and `HEAD /{slug}`. No auth. `slug` is a path segment, 7-char base62 for MVP-generated links.

## Behavior
| Case | GET | HEAD | Counts? |
|---|---|---|---|
| Active slug | 302, `Location: <target>` | 302, same headers, no body | GET yes (committed before response); HEAD no |
| Missing or disabled | 404 + error envelope (`code: "not_found"`) | 404 | no |
| Lock timeout, `COUNT_FAIL_MODE=strict` | 503 + `Retry-After: 1` (`code: "store_busy"`) | 503 + `Retry-After: 1` | no |
| Lock timeout, `COUNT_FAIL_MODE=open` | 302 uncounted (stats = lower bound) | 302 | no |

Required headers on all responses: `Cache-Control: no-store`.

## Semantics
A click is one committed redirect attempt. Bots and retries count; no uniqueness.

## Compatibility notes
- Adding response headers: minor bump.
- Changing any status above or when counting happens: breaking (major, sign-off required).

## Changelog
- 1.0.0: initial freeze (T01).
```
