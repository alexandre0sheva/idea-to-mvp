# Short Link Service

A small internal URL shortener: create a short link, redirect to its target, and count redirect requests per link.

## Problem and users

Our internal team needs short links with rough click counts. Only trusted team members can create links, using one shared API key. There is no public signup. Counts are approximate by design.

## MVP features

- `POST /links`: create a link with a random, collision-checked code. Requires the API key and is rate-limited per key (fixed window). Targets must be `http` or `https`, at most 2048 characters, with a valid hostname, and must not point at this service's own domain.
- `GET /{code}`: 302 redirect to the target. Unknown codes return 404. Each request enqueues a click without blocking the redirect.
- `GET /links/{code}/stats`: returns `redirect_requests`, `dropped_increments`, and an approximate-count note.
- `GET /health`: liveness check.

Out of scope for v1: custom aliases, link deletion or expiry, bot filtering, dashboards, user accounts, per-event analytics.

## Tech stack

- Python, FastAPI, u
