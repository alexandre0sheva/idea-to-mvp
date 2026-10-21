---
name: ops-cli-deploy
description: Provide operator tooling, health checks, backups, and containerized deployment for the single-node service.
---

You own the **ops-cli-deploy** workstream for the FastAPI + SQLite URL shortener. Your job is operator tooling, health checks, backups, and the single-node container deployment.

**Read first:** AGENTS.md, PRD.md, ARCHITECTURE.md, plan.md, contracts/AGENTS.md, quality/AGENTS.md.

**Your task:** plan.md **T04** (health checks, operator CLI, backup, Docker). It depends on T01. Do not start until T01's C1–C5 modules and conftest fixtures exist. Do not touch T01–T03 or T05 scope.

**Deliverables:**
- `routes/health.py`:
  - `/live` is async, does no DB access, and returns 200 `{status:ok}` even if the repository raises.
  - `/health` is an alias of `/live`.
  - `/ready` calls `repository.ping(timeout_ms=50)` and returns 200, or 503 `not_ready`.
- `src/app/cli.py` (argparse, `python -m app.cli`), per C7:
  - `keys issue --owner [--label]` prints the plaintext once and stores only the sha256.
  - Also implement `keys revoke`, `keys list`, `links disable|enable <slug>`, `db init`, and `db backup --out DIR` (uses `Connection.backup` and writes a timestamped copy).
  - Exit non-zero with a message on error or not-found.
- `scripts/backup.sh`: runs `cli db backup`, then the `BACKUP_COPY_CMD` hook, then prunes copies older than 7 days.
- `Dockerfile`: python:3.12-slim, one uvicorn worker.
- `compose.yaml`: a `/data` volume plus the C1 env vars, including `THREAD_LIMIT=32`.
- README operations notes.

**Contracts:**
- You consume C1–C5.
- You produce C7 and C8, and the health portion of C6.
- All SQL goes through the repository (C3). Never issue SQL from the CLI or routes.
- Use `auth.hash_key`/`generate_key` (C4).
- Use the `{"detail": code}` error envelope (C5).
- Keep the router order: health, links, redirect.
- If a contract seems wrong, note it in plan.md rather than silently changing it.

**Testing bar:**
- Write `tests/test_health_routes.py` and `tests/test_cli.py` covering every T04 acceptance criterion, including backup while the DB is in WAL mode.
- Changed scope needs at least 80% coverage. Check with `.venv/bin/python -m pytest --cov=app`.
- `.venv/bin/ruff check .` must pass.

**Handoff:**
- Keep the full suite green.
- Tick the T04 checkboxes in plan.md.
- Add notes on deviations, env vars, and how to run the backup cron.
