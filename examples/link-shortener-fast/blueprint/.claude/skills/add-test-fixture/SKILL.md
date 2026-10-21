---
name: add-test-fixture
description: "Use when a test needs an app client, a temporary SQLite database, an API key, or a clock, so each test starts from a clean, isolated state."
---

1. Put shared fixtures in tests/conftest.py. 2. Create a fixture that points the app settings at a file in pytest's tmp_path, so every test gets a fresh SQLite database that is never the real data file. 3. Set the API key through monkeypatch.setenv before the app is imported or the settings are read. 4. Build the client with fastapi.testclient.TestClient used as a context manager so lifespan startup and shutdown run, which starts and drains the click queue. 5. For time-dependent or rate-limit tests, inject a fake clock through a parameter or monkeypatch rather than sleeping. 6. Keep assertions on public responses and stored rows, not on private attributes. 7. Run bash scripts/check.sh to confirm the new fixtures pass and nothing else changed.
