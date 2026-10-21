# Blueprint review

1 blocker(s) and 4 warning(s) were still open when this pack was written. Decide whether to accept them before starting implementation.

- **blocker** `plan.md`: C4 says require_owner returns only owner_id, but R16 and the ARCHITECTURE ratelimit component require limiting per API key (key_hash), so change C4 to return Principal(key_hash, owner_id) as ARCHITECTURE specifies, letting T02 key the limiter per key rather than per owner.
- **warning** `.claude/agents/core-api-storage.md`: The rule hard-codes a busy_timeout of 150ms, but R3 and C3 require the configured SQLITE_BUSY_TIMEOUT_MS value (150 is only the default), so reword it to say the configured value.
- **warning** `ARCHITECTURE.md`: The db component uses timeout=BUSY_TIMEOUT_MS/1000, which does not match the env var name SQLITE_BUSY_TIMEOUT_MS used in the PRD and C1, so rename it to the PRD name.
- **warning** `PRD.md`: R2 defines api_keys without the label column that ARCHITECTURE, C2, and the CLI --label option rely on, so either add label TEXT to R2 or drop label from the architecture and plan.
- **warning** `.claude/agents/tests-benchmarks.md`: The agent is told to run the plan's test_report command, but no such command is visible in plan.md, so either name the exact command in the agent file or confirm it is defined in plan.md.
