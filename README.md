# Idea-to-MVP Orchestrator

`idea-to-mvp` turns a rough product idea into a **built, tested first version of your product**. A LangGraph pipeline runs every stage with a dedicated agent — a multi-LLM panel debates the idea, a summarizer distills it, an architect proposes options, a strategy agent picks how implementation agents should be organized, a planner writes an agent-ready blueprint, and Claude Agent SDK agents then implement the project and verify its test suite. You approve every gate along the way.

## Pipeline

```
Idea
 → Panel discussion       PM (OpenAI) · Tech Lead (Anthropic) · Skeptic (Google), round-aware debate
 → Summarizer             executive brief + exactly 5 MVP decision questions
 🔒 Answers gate           you answer the questions
 → Architect              two architecture options anchored to your answers
 🔒 Architecture gate      you pick Option A (fast) or Option B (scale)
 → Strategy agent         auto-selects the execution mode: Subagents vs Agent Team
 🔒 Blueprint gate         you approve generating the execution pack
 → Planner                blueprint pack: PRD, ARCHITECTURE, AGENTS guides, plan.md,
                          .claude/agents/* subagent definitions, STRATEGY.json
 🔒 Implementation gate    explicit confirmation — this spends real API tokens
 → Implementer            Claude Agent SDK agents build the project in generated_projects/
 → Verifier               runs the generated project's own test suite, bounded fix loop
 → Delivery report        file tree, verification verdict, how to run your product
```

The whole session is one continuously checkpointed LangGraph thread; every 🔒 gate is a real `interrupt()` pause resumed with `Command(resume=...)`.

## Execution strategies

The strategy agent inspects the architecture and workstreams and chooses how the implementation runs:

- **Subagents** — one lead Claude Agent SDK session delegating to specialized subagents (one per workstream, defined in the blueprint's `.claude/agents/*.md`). Best for small/coupled MVPs.
- **Agent Team** — one focused agent session per workstream, run sequentially over the shared workspace. Best for larger MVPs with independent workstreams.

The decision and reasoning are recorded in the blueprint's `STRATEGY.json`.

## Quick Start

### Requirements

- Python 3.11+
- API keys for the providers you use (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`)
- The implementation stage requires `ANTHROPIC_API_KEY` (it drives the Claude Agent SDK)

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Fill in the provider API keys and adjust model selections if needed.

### Run

```bash
python app.py
```

Describe your idea, choose rounds per speaker, and follow the gates. At the final gate you decide whether implementation agents build the product — the cost warning shows the model and per-session budget cap before anything runs.

## Outputs

| Folder | Contents |
|---|---|
| `exports/` | Saved conversation Markdown files (any time, via *Save to Markdown*) |
| `project_blueprints/` | Blueprint packs: `README.md`, `PRD.md`, `ARCHITECTURE.md`, `AGENTS.md` guides, `plan.md`, `.claude/agents/*.md`, `STRATEGY.json` |
| `generated_projects/` | Implemented projects (a copy of the blueprint plus the code the agents built and tested) |

## Configuration

All settings load from `.env` (see `.env.example`). Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `*_PROVIDER`, `*_MODEL` | see `.env.example` | Provider/model per discussion & pipeline role |
| `DISCUSSION_MAX_TOKENS`, `SKEPTIC_MAX_TOKENS`, `SUMMARY_MAX_TOKENS` | 900/1200/2000 | Output budgets |
| `IMPLEMENTER_MODEL` | `claude-opus-4-8` | Model for implementation/verification agents |
| `IMPLEMENTER_MAX_TURNS` / `VERIFIER_MAX_TURNS` | 120 / 40 | Agent session turn caps |
| `IMPLEMENTER_MAX_BUDGET_USD` | 10.0 | Hard cost cap per agent session |
| `MAX_FIX_ATTEMPTS` | 2 | Verification fix-loop bound |
| `IMPLEMENTER_PERMISSION_MODE` | `bypassPermissions` | Agent SDK permission mode (see security note) |
| `ENABLE_CHECKPOINTER` | `true` | LangGraph in-memory checkpointing |

### Cost & security notes

- The implementation stage runs autonomous agents with **file and shell access inside the workspace** (`generated_projects/<project>/`). The default `bypassPermissions` mode is what makes unattended building possible — only run it on machines/projects where that is acceptable, and review `IMPLEMENTER_MAX_BUDGET_USD` before starting.
- Nothing implementation-related runs without your explicit confirmation at the implementation gate.
- Never commit `.env` files or API keys.

## Development

```bash
ruff check .
pytest
```

## Project Layout

- `app.py` — Gradio UI entrypoint
- `graph.py` — LangGraph construction (nodes, gates, routing)
- `agents.py` — node functions, model runtimes, LLM invocation
- `roles.py` — declarative role registry (providers, models, token budgets, system prompts)
- `blueprints.py` — blueprint v2 document prompts and pack generation
- `implementer.py` — Claude Agent SDK implementation/verification sessions
- `submit_service.py` — stateful submit flow: starts/resumes the graph thread, maps events to UI
- `state.py` — shared graph state contract
- `render.py` — UI rendering helpers and the pipeline stage tracker
- `exporter.py` — Markdown export builder
- `config.py` — Pydantic settings from `.env`

## Security

See `SECURITY.md` for vulnerability reporting guidance.

## License

This project is licensed under the MIT License. See `LICENSE`.
