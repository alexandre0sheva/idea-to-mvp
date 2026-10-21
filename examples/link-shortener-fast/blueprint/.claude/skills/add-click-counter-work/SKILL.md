---
name: add-click-counter-work
description: "Use when changing the click pipeline: the bounded queue, the batched flush worker, the drop counter, or the SIGTERM drain, so redirects never block on the database."
---

1. Keep the queue in src/app/ as an asyncio.Queue created with a fixed maxsize; enqueue only with put_nowait and, on asyncio.QueueFull, increment the in-process dropped counter and return without raising. 2. Start the flush worker from the FastAPI lifespan handler; it drains up to a batch size or a short interval, aggregates counts per code, and writes one transaction per flush using UPDATE links SET clicks = clicks + ? WHERE code = ?. 3. Never call the database from a redirect handler. 4. Handle SIGTERM by stopping intake, then flushing every queued item before the process exits, so served minus dropped equals counted clicks after a graceful stop. 5. Expose redirect_requests and dropped_increments through the stats response only. 6. Add tests for queue-full drops, flush correctness across several codes, and the drain on shutdown using a temporary database file. 7. Run bash scripts/check.sh and fix any failures before finishing.
