---
name: add-repository-function
description: "Use when a feature needs new or changed SQL or schema, so the statement lives in the repository layer with the right connection, PRAGMAs, and error translation."
---

1. If the schema changes, edit `src/app/schema.sql` additively with `CREATE TABLE/INDEX IF NOT EXISTS` (no migration framework; it must stay idempotent because `db.init_db()` runs at every startup and from `python -m app.cli db init`).
2. In `src/app/repository.py`, add a function that opens `with db.connect(settings) as conn:` (one short-lived connection per call, `isolation_level=None`, never cached or shared across threads).
3. Use parameterized `?` SQL only. Keep hot-path writes single-statement, e.g. `UPDATE links SET clicks = clicks + 1 WHERE slug = ? AND disabled = 0 RETURNING target`.
4. Translate errors: `sqlite3.OperationalError` containing 'locked'/'busy' -> `StoreBusy`; `sqlite3.IntegrityError` on slug -> `SlugTaken`. Return plain dataclasses from `src/app/models.py` or primitives, never HTTP types, and make no strict/open policy decisions here.
5. Timestamps come from the `utc_now_iso()` helper (UTC ISO 8601).
6. Test in `tests/test_repository.py` against a tmp_path DB: happy path, missing row returns None, and StoreBusy by holding a write lock from a second connection (`BEGIN IMMEDIATE`) with a small busy timeout.
7. Run `.venv/bin/python -m pytest tests/test_repository.py -q`, then `bash scripts/check.sh`.
