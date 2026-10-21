# Shortlink: Invite-Only URL Shortener

A small, auditable HTTP API that creates short links, redirects visitors, and counts every redirect exactly.

## Problem & users

Hosted shorteners often don't explain how they count clicks. This service is a single-node shortener for **3–10 invited developers**, run by one **operator**. Anonymous **visitors** follow the links.

A **click** is one committed `GET /{slug}` redirect. Clicks include bots and retries, so they are not unique visitors. `HEAD` requests never count.

## MVP features

- `POST /links` (Bearer API key) returns `201` with a random 7-character base62 slug and a short URL. Target URLs must be http/https, at most 2048 characters, and not localhost or a private/loopback/link-local IP literal.
- `GET /{slug}` returns `302` with `Cache-Control: no-store`. The click is counted atomically before the redirect.
- `HEAD /{slug}` returns the redirect without counting a click.
- `GET /links/{slug}/stats` is owner-only. Other owners get `404`.
- Per-key creation rate limit returns `429` with `Retry-After`.
- `COUNT_FAIL_MODE=strict|open`:
  - `strict` (default) returns `503` with `Retry-After: 1` when the database is locked.
  - `open` redirects without counting.
- `/live` (alias `/health`) and `/ready` health checks.
- Operator CLI: issue and revoke keys, disable and enable links, init the database, and back it up.
- Docker image with a persistent database volume.

## Tech stack

- Python 3.12
- FastAPI with sync handlers
- uvicorn (1 worker)
- stdlib `sqlite3` (SQLite ≥ 3.35, WAL, `synchronous=NORMAL`)
- pytest and ruff
- locust for benchmarks
- Docker

## Local setup

```bash
python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Configuration is set through environment variables. Defaults:

```
DATABASE_PATH=./data/shortener.db   BASE_URL=http://127.0.0.1:8000
COUNT_FAIL_MODE=strict              SQLITE_SYNCHRONOUS=NORMAL
SQLITE_BUSY_TIMEOUT_MS=150          CREATE_RATE_LIMIT_PER_MIN=60
```

Try it (CLI subcommand names are provisional):

```bash
.venv/bin/python -m app.cli keys issue --owner dana   # prints the key once
curl -X POST localhost:8000/links -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"target":"https://example.com"}'
```

Run with Docker:

```bash
docker compose up --build
```

## Test & lint

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Benchmark scripts and the `kill -9` reconciliation script live in `bench/`. Results are recorded in `docs/BENCHMARKS.md`.

## Project layout

```
src/app/
  main.py         app, lifespan, router order (redirect last)
  config.py       env settings, validated at startup
  db.py           per-request connections, PRAGMAs, schema bootstrap
  schema.sql      links, api_keys
  repository.py   the only module that runs SQL
  auth.py  ratelimit.py  validation.py  slugs.py  errors.py  models.py
  routes/         health.py, links.py, redirect.py
  cli.py          operator CLI
tests/            pytest suites
scripts/          check.sh, backup.sh
bench/            load and crash-reconciliation scripts
docs/             BENCHMARKS.md
Dockerfile  compose.yaml
```

Out of scope for the MVP: custom slugs, dashboards, unique-visitor analytics, DNS checks, and billing.
