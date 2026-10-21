---
name: run-app-locally
description: "Use when starting the service on a developer machine to try the API, check health, or reproduce a bug by hand before writing a test."
---

1. Create the environment once with the project install command: python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]". 2. Export the shared API key as an environment variable (the name the app reads from its settings module) and set the SQLite database path to a local file ignored by git. 3. Start the server with the project run command: .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000. 4. Check liveness with a request to /health and expect a 200 response. 5. Create a link with a POST to /links including the API key header, then open the returned code path to confirm the 302. 6. Stop the server with Ctrl+C and confirm the log shows the click queue was drained. 7. Run bash scripts/check.sh before committing any code change.
