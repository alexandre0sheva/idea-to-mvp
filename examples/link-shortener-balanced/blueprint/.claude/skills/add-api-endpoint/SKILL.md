---
name: add-api-endpoint
description: "Use when adding or changing an HTTP route (links, redirect, health) so it follows the router order, auth, error envelope, and repository boundary of this FastAPI app."
---

1. Pick the router module under `src/app/routes/`: `health.py` (/live, /health, /ready), `links.py` (POST /links, GET /links/{slug}/stats), or `redirect.py` (GET/HEAD /{slug}). Never add a new top-level path after `redirect` in `src/app/main.py`; routers are included health, links, redirect (catch-all last). Add any new fixed path word to `RESERVED` in `src/app/slugs.py`.
2. Write handlers as sync `def` (only `/live` is `async def` and must not touch the DB). Declare request/response Pydantic models in `src/app/models.py` (request bodies use `model_config = ConfigDict(extra='forbid')`).
3. Protect owner endpoints with `owner_id: str = Depends(require_owner)` from `src/app/auth.py`; read config via `Depends(get_settings)` from `src/app/config.py`.
4. Do storage only through functions in `src/app/repository.py`; if you need new SQL, add a function there (see the add-repository-function skill).
5. Raise domain exceptions from `src/app/errors.py` (StoreBusy, SlugTaken, not-found, rate-limited) and let the handlers map them to `{"detail": code}` with `Retry-After` where applicable; make sure JSON responses keep `Cache-Control: no-store`. Stats for missing and foreign slugs must return the identical 404 `not_found`.
6. Add `summary`/`description` to the route decorator so OpenAPI documents the contract (click semantics for redirects).
7. Add tests in the task's `tests/test_*_api.py` using the `tests/conftest.py` fixtures (tmp_path DB client, key issuer, link seeder); assert status, body, headers, and DB side effects (e.g. clicks unchanged on HEAD/404).
8. Run one file fast: `.venv/bin/python -m pytest tests/test_links_api.py -q`, then finish with `bash scripts/check.sh`.
