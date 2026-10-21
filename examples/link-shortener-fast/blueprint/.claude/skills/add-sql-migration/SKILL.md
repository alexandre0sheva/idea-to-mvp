---
name: add-sql-migration
description: "Use when a change needs a new or altered SQLite table, column, or index, so it is applied as a plain-SQL migration at app startup."
---

1. Check the current schema in the migration files under src/app/ (or the migrations folder the repository module reads) to find the next number. 2. Add a new numbered .sql file in that folder with only plain SQL, using CREATE TABLE IF NOT EXISTS or additive ALTER statements; never edit a migration that has already shipped. 3. Make sure the migration runner applies pending files in order at startup and records which ones ran. 4. Keep the connection settings in the repository layer: PRAGMA journal_mode=WAL and busy_timeout set on every new connection. 5. Add a test that starts from an empty temporary database file, runs migrations, and checks the resulting tables and columns. 6. Add a test that running migrations twice changes nothing. 7. Run bash scripts/check.sh and confirm it passes.
