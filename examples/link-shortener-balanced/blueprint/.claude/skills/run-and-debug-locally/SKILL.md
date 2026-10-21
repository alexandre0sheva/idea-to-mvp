---
name: run-and-debug-locally
description: "Use when you need to run the service locally, exercise endpoints by hand, or debug a failing test."
---

1. Install/refresh deps: `python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]"` (add new deps to `pyproject.toml` `dependencies`, tools to the `dev` extra, then reinstall).
2. Issue a key: `DATABASE_PATH=./data/shortener.db .venv/bin/python -m app.cli keys issue --owner dev`.
3. Start the server: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.
4. Exercise it: `curl -s -X POST localhost:8000/links -H 'Authorization: Bearer KEY' -H 'Content-Type: application/json' -d '{"target":"https://example.com"}'`, then `curl -si localhost:8000/SLUG` (expect 302) and `curl -s localhost:8000/links/SLUG/stats -H 'Authorization: Bearer KEY'`. Check `/live` and `/ready`.
5. Debug a failing test: `.venv/bin/python -m pytest tests/test_x.py::test_name -x -vv`; add `-s` to see prints, `--pdb` to drop into the debugger. Router-order bugs show up as 404s from the redirect catch-all.
6. Stop the server and finish with `bash scripts/check.sh`.
