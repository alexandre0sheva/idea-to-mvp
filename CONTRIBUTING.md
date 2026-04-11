# Contributing

Thanks for contributing.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff
cp .env.example .env
```

## Run Locally

```bash
python app.py
```

## Quality Checks

```bash
ruff check .
pytest
```

## Pull Requests

- Keep changes focused and explain motivation.
- Add tests for behavior changes when possible.
- Update `README.md` or docs when setup/runtime behavior changes.
- Never commit secrets (`.env`, API keys, credentials).
