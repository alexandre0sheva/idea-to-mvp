---
name: add-cli-command
description: "Use when adding or changing an operator command in `python -m app.cli` (keys, links disable/enable, db init/backup)."
---

1. Open `src/app/cli.py` (argparse with nested subparsers: `keys issue|revoke|list`, `links disable|enable`, `db init|backup`). Add the subparser and a handler function; keep `main(argv: list[str] | None = None) -> int` so tests can call it directly.
2. Load settings with `load_settings()` and call only `repository`/`db`/`auth` functions (e.g. `auth.generate_key()`, `auth.hash_key()`, `repository.set_disabled()`); no raw SQL in the CLI.
3. Print the plaintext API key exactly once on `keys issue`; store only the sha256 hash. Never log raw keys.
4. Return 0 on success; print a message to stderr and return non-zero on error or not-found (e.g. unknown slug).
5. Backups use `sqlite3.Connection.backup()` into a timestamped file under `--out`; never copy the live `.db`/`-wal` files.
6. Test in `tests/test_cli.py` by setting `DATABASE_PATH` to a tmp_path via monkeypatch, calling `main([...])`, using `capsys` for output, and verifying the effect through the API client (e.g. revoked key -> 401, disabled link -> 404).
7. Try it manually: `.venv/bin/python -m app.cli keys issue --owner alice`. Finish with `bash scripts/check.sh`.
