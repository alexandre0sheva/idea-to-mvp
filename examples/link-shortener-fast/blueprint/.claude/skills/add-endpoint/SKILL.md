---
name: add-endpoint
description: "Use when adding or changing an HTTP route in the FastAPI app, such as link creation, redirect, or stats, to put it in its own router module with validated models and TestClient tests."
---

1. Read src/app/main.py and the existing router modules under src/app/ to match naming and style. 2. Create a router module under src/app/ (for example src/app/links.py) with an APIRouter and typed Pydantic request and response models. 3. Put business logic in a service function and database calls in the repository module, never inside the route function. 4. Return explicit status codes: 201 for create, 302 with a Location header for redirect, 404 for unknown codes, 401 for a missing or wrong API key, 429 when the per-key rate limit is exceeded. 5. Include the router in src/app/main.py with app.include_router. 6. Add tests under tests/ using fastapi.testclient.TestClient for success, invalid input, auth failure, and the not-found case. 7. Run bash scripts/check.sh and fix any failures before finishing.
