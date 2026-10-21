# quality/AGENTS.md: Testing and Verification Standard

This file defines how every task in this repository is verified before it counts as done. It applies to the lead agent and to every subagent: `core-api-storage`, `ops-cli-deploy`, and `tests-benchmarks`. No task is complete until its author has met every **MUST** in this file and recorded the evidence.

The product is an invite-only URL shortener. It uses FastAPI with sync handlers, stdlib `sqlite3` in WAL mode, and one uvicorn worker, built on the `python-fastapi` scaffold. Its central promise is **count accuracy with an explicit failure contract**. The tests exist to protect that promise.

---

## 1. Non-negotiable rules (summary)

1. **Every task ships unit tests and integration tests** for the behavior it adds or changes. Changing code without tests is not allowed. Docs-only and config-only changes are the exceptions listed in §4.3.
2. **Coverage of the changed scope must be at least 80%** (line coverage), measured as described in §4. The whole-project floor is also 80%.
3. **There is one top-level test command that verifies the whole project:**
   ```
   .venv/bin/python -m pytest
   ```
   It MUST be documented in `README.md` under a "Testing" heading, together with the install, lint, and coverage-report commands from §2. A fresh clone that follows the README MUST be able to run it green with no extra setup. It needs no network, no running server, and no pre-existing database.
4. **Agents run verification themselves** and paste real command output into the handoff (§9). Do not claim "tests pass" without output from the current commit.
5. **Never weaken a test to make it pass.** Do not delete tests, loosen assertions, or add `skip` or `xfail` to hide failures. If a test is wrong, fix it and explain why in the handoff.
6. **Lint is clean** (`.venv/bin/ruff check .` exits 0) on every task.

---

## 2. Canonical commands

These commands are fixed by the scaffold. Use them exactly. Do not invent alternatives in docs or CI.

| Purpose | Command |
|---|---|
| Install | `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"` |
| **Top-level test (whole project)** | `.venv/bin/python -m pytest` |
| Test + reports + coverage | `.venv/bin/python -m pytest --junitxml=.idea-to-mvp/reports/junit.xml --cov=app --cov-report=xml:.idea-to-mvp/reports/coverage.xml` |
| Lint | `.venv/bin/ruff check .` |
| Run locally | `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| Changed-scope coverage (after the report run) | `.venv/bin/python -m coverage report -m --include="<comma-separated changed src files>"` |

Configuration requirements, set up by the first task that touches `pyproject.toml` and kept in place afterward:

- `[tool.coverage.report] fail_under = 80`, so the report command fails if whole-project coverage drops below 80%.
- `[tool.coverage.run] branch = true` is recommended. Line coverage is the gate, but agents should read branch misses.
- pytest `testpaths = ["tests"]`. Load-test scripts (`bench/`) and the crash-reconciliation script (`scripts/`) are **not** collected by pytest. They are release-readiness tools (§8), not part of the default suite.
- Register custom markers (for example `concurrency`) in `pyproject.toml`. Never use a marker to exclude tests from the default run.

`scripts/check.sh` comes from the scaffold. Agents may run it as a convenience, but the commands above are the source of truth. Do not re-create or restructure it.

---

## 3. Required tests per task

Every task MUST add or update both layers below for each behavior it touches.

### 3.1 Unit tests (pure logic, no HTTP)

These target functions directly and run fast (under ~50 ms each). Required units in this project:

| Unit | Must cover |
|---|---|
| Config loader (R1) | Every default value. Each env override. Invalid `COUNT_FAIL_MODE` and invalid `SQLITE_SYNCHRONOUS` raise a clear error. |
| SQLite version guard (R4) | A monkeypatched `sqlite3.sqlite_version` below 3.35.0 refuses startup with a clear message. Versions 3.35.0 and above pass. |
| Slug generator (R10, R12) | Length is exactly 7. The alphabet is `[0-9A-Za-z]` only. It uses `secrets`. Reserved words are rejected case-insensitively (`OpenAPI`, `LIVE`). |
| URL validator (R13, R14) | Every rule (a)–(e). The 2048/2049 length boundary. Every rejected IP form in the R14 table: dotted, integer, octal, hex, short, and bracketed IPv6 including `::ffff:127.0.0.1`. The public literals `8.8.8.8` and `[2606:4700::1111]` are accepted. `example.com` is accepted **with no DNS call**: monkeypatch `socket.getaddrinfo` and `socket.gethostbyname` to raise. The `detail` names the failed rule. |
| API-key hashing (R6, R7) | SHA-256 hex digest of the key. The plaintext is never part of the stored row. |
| Rate limiter (R16) | Under limit, at limit, and over limit. Window reset. Per-key isolation. A `Retry-After` of at least 1. Use an **injected clock**, never `time.sleep`. |
| Timestamp formatting (R5) | ISO 8601 UTC with a `Z` or `+00:00` suffix. |

### 3.2 Integration tests (real FastAPI app plus a real SQLite file)

These use `TestClient` against the real app wired to a temporary SQLite file (§6). Do not mock the repository in integration tests. The point is to exercise real SQL, pragmas, and transactions.

Integration tests are required for:

- **Schema and repository (R2, R3):** Idempotent startup run twice on the same file keeps data. The parent directory of `DATABASE_PATH` is created. Pragmas read from a repository-created connection report `journal_mode=wal`, the configured `synchronous` value, and the configured `busy_timeout`. No route module executes SQL: add a test or lint rule asserting that `sqlite3` and `execute(` appear only in `repository.py` (plus CLI and backup modules if they call the repository).
- **Auth (R6–R8):** A missing header, a non-Bearer scheme, a malformed header, and an unknown key each return 401 with the body `{"detail":"unauthorized"}` and `WWW-Authenticate: Bearer`. After the key's hash is deleted, the very next request returns 401 (no caching).
- **Create (R9–R12, R15, R16):** A 201 response whose body, `Location`, and `short_url` handle a trailing slash in `BASE_URL`. Unknown fields, including `slug`, return 422. Posting the same target twice produces two slugs. A forced collision: one collision then success, and three collisions returning 503 with `Retry-After: 1` and `slug_generation_failed`. Reserved-word regeneration does not consume a retry. A rate-limit response of 429 inserts **no row** (assert the row count).
- **Redirect (R17, R18):** A 302 response with exact `Location`, which matches the submitted target with no normalization, plus `Cache-Control: no-store`, and `clicks` incremented by exactly 1. Each of these returns 404 `{"detail":"not_found"}` with no counter change: an unknown slug, a disabled link, a malformed segment (wrong length or non-base62), and a lowercased variant of a mixed-case slug. Use `follow_redirects=False` on every redirect test.
- **HEAD /{slug}:** Returns the redirect status and `Location` and **never** changes `clicks`. Assert the before and after counts.
- **Stats:** The owner sees the exact count. A different owner's key gets 404 with a body **byte-identical** to the 404 for a nonexistent slug (US3). Unauthenticated requests get 401.
- **Concurrency exactness:** N threads (at least 8) each fire M `GET /{slug}` requests against one file-backed DB. The final `clicks` equals the number of observed 302 responses exactly. Mark this test `concurrency`, but it MUST stay in the default run.
- **COUNT_FAIL_MODE under lock contention:** A second connection holds `BEGIN IMMEDIATE` (a write lock) longer than `SQLITE_BUSY_TIMEOUT_MS`. In `strict` mode, `GET /{slug}` returns 503 with `Retry-After: 1` and the count stays unchanged. In `open` mode it returns 302 to the correct target, uncounted. Release the lock in a `finally` block. Keep the busy timeout small in tests so the test stays fast.
- **Health:** `/live` returns 200 even when the DB path is unusable, which proves it does no DB access. `/ready` returns 200 normally and a non-200 response when the DB is locked or unavailable past its 50 ms budget.
- **Operator CLI:** Issuing a key prints the plaintext once, and the DB holds only the hash. A revoked key's next API call returns 401. Disabling a link makes it return 404, and re-enabling it restores the redirect while preserving `clicks`. Invoke the CLI through its entry function or `subprocess` against a temp DB.
- **Backup:** The `.backup`-based script produces a file that opens as a valid SQLite DB containing the same `links` and `api_keys` rows, taken while the app holds an open connection.

A task only needs the rows relevant to its scope. However, once a test exists, every later task keeps it green (§7).

---

## 4. Coverage goals

### 4.1 Gates

| Scope | Gate | How measured |
|---|---|---|
| **Changed scope of the task** | **at least 80% line coverage** | `coverage report -m --include=<changed files under src/app>` after the report run |
| Whole project (`--cov=app`) | at least 80% | `fail_under = 80` in pyproject; the report command fails below it |
| Safety-critical modules: URL validator, redirect/count path, auth, repository | target 95%+ | Same report. Uncovered lines MUST be justified in the handoff. |

"Changed scope" means every file under `src/app/` that the task added or modified. Include CLI and backup modules if they live under `src/app/`. For scripts outside the package, the handoff states how they were exercised.

### 4.2 Rules

- Coverage is a floor, not the goal. A line that executes without an assertion on its outcome does not count as tested in review.
- Do not use `# pragma: no cover` except for `if __name__ == "__main__":` blocks and truly unreachable defensive branches. Each use needs a one-line justification comment.
- If changed scope falls below 80%, the task is **not done**. Add tests. Do not narrow the measurement.

### 4.3 Exemptions

Changes that touch only docs (`*.md`), the Dockerfile or compose file, or bench scripts are exempt from the coverage gate. They still require the full suite and lint to pass. Docker changes require the smoke check in §8.

---

## 5. Contract tests

The HTTP contract is a product feature, as US4, US5, and US6 make explicit. Contract tests live in `tests/contract/` (or files named `test_contract_*.py`) and assert **exact** status codes, header names and values, and JSON bodies. Partial matches are not enough.

Required contract assertions:

| Endpoint / condition | Status | Required headers | Exact body |
|---|---|---|---|
| `POST /links` success | 201 | `Location` == `short_url` | keys exactly `{slug, short_url, target, created_at}` |
| Auth failure (any) | 401 | `WWW-Authenticate: Bearer` | `{"detail":"unauthorized"}` |
| Invalid target | 422 | none | `detail` identifies the rule |
| Unknown field in body | 422 | none | FastAPI validation shape |
| Rate limited | 429 | `Retry-After` integer ≥ 1 | `{"detail":"rate_limited"}` |
| Slug generation exhausted | 503 | `Retry-After: 1` | `{"detail":"slug_generation_failed"}` |
| `GET /{slug}` success | 302 | `Location`, `Cache-Control: no-store` | none |
| Not found / disabled / malformed | 404 | none | `{"detail":"not_found"}` |
| Strict-mode count failure | 503 | `Retry-After: 1` | as specified in PRD |
| Stats, non-owner | 404 | none | identical to nonexistent-slug 404 |

Also required:

- **OpenAPI contract test:** fetch `/openapi.json` and assert all of the following:
  - All four product endpoints are present.
  - The redirect operation's description defines a click as a committed `GET` redirect, says that bots and retries are included, says that clicks are not unique visitors, and says that `HEAD` is not counted.
  - `COUNT_FAIL_MODE` strict and open behavior are described, including that open-mode stats are a lower bound.
- **Response-shape lock:** if a task changes any row of the table above, it MUST update the PRD reference and README in the same task and call out the change in the handoff as **CONTRACT CHANGE**. Silent contract drift is a defect.
- Where the PRD specifies a shape this table does not repeat, such as the stats body or the strict 503 body, the PRD is authoritative. Contract tests assert the PRD shape exactly.

---

## 6. Fixture guidance

Shared fixtures live in `tests/conftest.py`. Use them. Do not reinvent setup per file.

- **Isolated DB per test:** a `db_path` fixture built on `tmp_path`, with `DATABASE_PATH` set via `monkeypatch.setenv`. Tests MUST NEVER touch `./data/shortener.db` or any shared path. Use a real file, not `:memory:`, because WAL, per-request connections, and lock contention only behave realistically on a file.
- **App/client factory:** a `client` fixture that builds the app *after* env overrides are applied. Use an app factory or settings reload so config is not frozen at import time, and use `TestClient` as a context manager so startup and shutdown hooks run. Provide variants or parametrization for `COUNT_FAIL_MODE=strict|open` and `SQLITE_SYNCHRONOUS=NORMAL|FULL`.
- **Keys and owners:** an `api_key` factory fixture that issues a key for a given `owner_id` through the same code path the CLI uses, and returns `(plaintext, owner_id)`. Provide at least two owners for scoping tests.
- **Links:** a `make_link` fixture that creates a link through `POST /links` (preferred) or the repository, and returns the slug. Use direct repository writes only for states the API cannot produce, such as `disabled=1` or a preset `clicks`.
- **Deterministic slugs:** monkeypatch the slug generator with an iterator of predetermined values to force collisions and reserved words. Never rely on randomness to hit a branch.
- **Clock:** the rate limiter and timestamp code take an injectable clock. Tests advance a fake clock and never sleep to wait for windows.
- **Lock holder:** a `held_write_lock(db_path)` context-manager fixture that opens a separate connection, runs `BEGIN IMMEDIATE`, yields, and always rolls back and closes.
- **No network:** an autouse fixture SHOULD make socket name resolution raise, enforcing "no DNS resolution" globally.
- **Test data:** keep URL-validation cases in a parametrized table (one case per line, with an id) so new fuzz findings become one-line additions.
- Fixtures clean up everything they create. Tests MUST pass in any order and in isolation (`pytest path::test_name`).

---

## 7. Regression checks

- **Every task runs the full top-level suite** (`.venv/bin/python -m pytest`), not just its own tests. A task that breaks an existing test is not done.
- **The lead agent re-runs the full suite, lint, and report command** after integrating any subagent's work, on the integrated tree, before accepting it.
- **Bug fixes start with a failing test** that reproduces the bug. The handoff shows that the test failed before the fix and passes after it.
- **Count invariants are permanent regression tests** and may never be removed:
  1. `HEAD` never counts.
  2. 404 paths never count.
  3. Concurrent `GET`s count exactly.
  4. Strict-mode 503 does not count.
  5. Non-owner stats are indistinguishable from nonexistent stats.
  6. No plaintext keys are stored.
  7. No DNS lookups are made.
- **Scaffold health test:** `tests/test_health.py` from the scaffold must stay green, or be deliberately replaced by the `/live` and `/ready` tests in the task that introduces them, with a note in the handoff. `health` is a reserved slug, so make sure route ordering never lets `/{slug}` shadow operational routes. Add a test that `/live`, `/ready`, `/docs`, and `/openapi.json` are not treated as slugs.
- **Flaky tests are defects.** Concurrency and lock tests must be deterministic: use explicit synchronization (barriers, events) and bounded timeouts. A test that fails intermittently gets fixed or escalated in the handoff, never retried until green.

---

## 8. Release-readiness checks

These run before the MVP is declared launch-ready, mainly by `tests-benchmarks` and `ops-cli-deploy`, and are verified by the lead. They cover the P1 requirements and are **not** part of the default pytest run. Each produces a recorded artifact.

| Check | Pass criteria | Artifact |
|---|---|---|
| Full suite + lint + coverage report | All green. Whole-project coverage ≥ 80%. | `.idea-to-mvp/reports/junit.xml`, `coverage.xml` |
| Load test: 200 RPS on one hot slug | Stored `clicks` == observed 302s exactly. p95 < 200 ms. 503 rate < 0.1%. `/live` and `/ready` stay responsive during the run. | `docs/benchmarks.md` (or equivalent) with raw numbers |
| Load test: 200 RPS across 100 slugs | Same gates | same |
| 10× burst | Same gates, or the breach is documented with the `COUNT_FAIL_MODE` decision | same |
| `synchronous` FULL vs. NORMAL | Both measured on the same scenarios, with the chosen default justified against the gates. NORMAL is the default per the user's durability tolerance. | same |
| `kill -9` reconciliation | After killing the server mid-load, stored count ≥ client-observed 302s for every slug | script output in benchmarks doc |
| URL fuzz | Fuzzed hosts and encodings produce no accepted private/loopback/link-local/unspecified literal and no 500s. Each new finding is added to the parametrized validator table. | fuzz summary + new test cases |
| Docker smoke | `docker compose up` starts one worker with the DB on a persistent volume. `/live` and `/ready` return 200. The create → redirect → stats flow works. Data survives a container restart. | commands + output in handoff |
| Backup/restore drill | A backup is taken while serving traffic. Restoring it into a fresh container serves the same links and counts. | commands + output |
| OpenAPI review | Click semantics and failure modes read correctly to a new integrator | reviewer note |
| README completeness | Contains install, **the single top-level test command**, lint, coverage report, run, config env vars, operator CLI usage, backup/restore, and health endpoints | README diff |

The `COUNT_FAIL_MODE` default stays `strict` unless the 503 gate is breached. Flipping it requires a benchmark artifact that shows the breach.

---

## 9. Task handoff: verification evidence and residual risk

Every task, from any agent, ends with this block in its handoff or PR description. The lead rejects handoffs that are missing it.

```markdown
## Verification
- Commit: <sha>
- Install: <ok / command output tail>
- Full suite: `.venv/bin/python -m pytest` → <N passed, 0 failed> (paste summary line)
- Lint: `.venv/bin/ruff check .` → <All checks passed!>
- Report run: <command> → whole-project coverage <xx%>
- Changed-scope coverage: `coverage report -m --include=...` → <per-file % and total, ≥80%>
- New/changed tests: <list test files and what each proves (unit / integration / contract)>
- Requirements covered: <R-numbers / US-numbers>
- Contract change: <none | CONTRACT CHANGE: describe + README/PRD updated>
- Manual / script checks (if any): <commands + output>

## Residual risk
| Risk | Why it is not fully covered | Likelihood / impact | Mitigation or follow-up | Owner |
|---|---|---|---|---|
```

### How to document residual risk

- **Always fill the table.** If there is genuinely none, write "None identified" and give the reason (for example, "pure refactor, all paths covered").
- List anything the automated tests **cannot** prove, with concrete scope. Typical entries for this project:
  - The in-memory rate limiter resets on restart and is per-process only. It is acceptable with one worker but breaks if workers are added.
  - `synchronous=NORMAL` can lose the last few seconds of clicks on host crash, which is accepted per the user's stated durability tolerance.
  - Hot-slug lock contention above the benchmarked 200 RPS is untested.
  - Phishing targets on public hostnames are not detected. The mitigation is operator disable.
  - Clicks include bots by design.
- List every uncovered line in safety-critical modules, with the reason.
- List any skipped check (for example, "Docker not available in agent environment") and say exactly who must run it and when. A skipped release-readiness check blocks launch until someone runs it.
- Do not use risk documentation as a substitute for tests that could reasonably have been written. Reviewers will send those back.
- The lead aggregates open residual risks into `quality/RISKS.md` (create it if absent) before release, and marks each as accepted, mitigated, or blocking.

---

## 10. Definition of done (checklist)

- [ ] Unit **and** integration tests added or updated for all changed behavior
- [ ] Changed-scope coverage ≥ 80%, whole project ≥ 80%
- [ ] `.venv/bin/python -m pytest` green on the integrated tree
- [ ] `.venv/bin/ruff check .` clean
- [ ] Contract tests updated if any status, header, or body changed, and the change is flagged
- [ ] Count invariants (§7) still green
- [ ] README documents the single top-level test command and stays accurate
- [ ] Handoff includes the Verification and Residual risk sections with real output
