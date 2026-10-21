# Contracts Governance (`contracts/AGENTS.md`)

This file governs every contract in the project: HTTP API, database schema, Redis key layout, internal events, and operational configuration. Agents working in parallel must follow these rules. When this file conflicts with a task description, stop and escalate to the lead session. Do not resolve the conflict silently.

## 1. Architecture baseline

The chosen architecture is **Option B**: a stateless FastAPI API with an asynchronous click pipeline, Redis for rate limits, pending counters, and link cache, and PostgreSQL for links and aggregated counts. Shipping runs as one small deployment using the same contracts that will later scale to multiple replicas.

Deviation from the SQLite preference is accepted and recorded in `ARCHITECTURE.md`. Any agent that proposes further deviation (for example, SQLite for links, or a different queue) must state a reason and get lead approval before writing code.

**Known conflict to resolve in T01:** some workstream deliverables still reference SQLite WAL, `busy_timeout`, and a single uvicorn worker.
