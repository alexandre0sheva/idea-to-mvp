# Idea-to-MVP Orchestrator

`idea-to-mvp` turns a rough product idea into a **built, tested first version of your product**. A LangGraph pipeline runs every stage with a dedicated agent — a multi-LLM panel debates the idea, a summarizer distills it, an architect proposes options, a strategy agent picks how implementation agents should be organized, a planner writes an agent-ready blueprint, and Claude Agent SDK agents then implement the project and verify its test suite. You approve every gate along the way.

## Pipeline

```
Idea
 → Panel discussion       PM (OpenAI) · Tech Lead (Anthropic) · Skeptic (Google): opening statements in
                          parallel, a moderator ends the debate early when it converges
 → Summarizer             executive brief + exactly 5 MVP decision questions
 🔒 Answers gate           you answer the questions
 → Architect              two architecture options anchored to your answers
 🔒 Architecture gate      you pick Option A (fast) or Option B (scale)
 → Strategy agent         auto-selects the execution mode: Subagents vs Agent Team
 🔒 Blueprint gate         you approve generating the execution pack
 → Planner                blueprint pack: PRD, ARCHITECTURE, AGENTS guides, plan.md,
                          .claude/agents/* subagent definitions, STRATEGY.json
 🔒 Implementation gate    explicit confirmation — this spends real API tokens
 → Implementer            Claude Agent SDK agents build the project in OUTPUT_DIR/projects/
 → Verifier               three parallel lanes (tests, quality, requirement coverage), bounded fix loop
 → Delivery report        file tree, verification verdict, how to run your product,
                          project zip, git tag v0.1
 🔁 Iterate               request changes, get v0.2 (new tasks, same engine)
```

The whole session is one continuously checkpointed LangGraph thread (stored in SQLite, so sessions survive restarts); every 🔒 gate is a real `interrupt()` pause resumed with `Command(resume=...)`.

## Execution strategies

The strategy agent inspects the architecture and workstreams and chooses how the implementation runs:

- **Subagents** — one lead Claude Agent SDK session delegating to specialized subagents (one per workstream, defined in the blueprint's `.claude/agents/*.md`). Best for small/coupled MVPs.
- **Agent Team** — one fresh agent session per plan task. Tasks whose dependencies allow it run in parallel, each in its own git worktree (`IMPLEMENTER_MAX_PARALLEL`, `1` for one at a time), and are merged back in task order; a merge conflict gets a bounded resolver session. Best for larger MVPs with independent workstreams.

The decision and reasoning are recorded in the blueprint's `STRATEGY.json`.

## Quick Start

### Try it without API keys

```bash
uv sync
DEMO_MODE=true uv run idea-to-mvp
```

The Settings accordion has an **Autopilot** switch (answers the questions with their suggestions, takes the recommended architecture, and generates the pack; it always stops at the implementation gate because that spends money) and the **Stop** button halts a run without losing its progress — *Continue* picks it up at the last checkpoint. The UI follows your system's light or dark theme. While the agents build, the *Implementation* tab shows a live task board, log, and cost meter; when they finish, a delivery dashboard shows the verdict of each verification lane, how every requirement fared, and how to run the project, with the project and blueprint downloadable as zips.

Demo mode walks the whole pipeline — panel, gates, blueprint pack, a stand-in implementation, verification, delivery report — with canned outputs. No keys, no spend.

### Requirements

- [uv](https://docs.astral.sh/uv/) (installs the right Python for you; 3.11–3.13 supported)
- API keys for the providers you use (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) — not needed for demo mode
- The implementation stage requires `ANTHROPIC_API_KEY` (it drives the Claude Agent SDK)

### Install

Uses [uv](https://docs.astral.sh/uv/) (Python 3.11+ is fetched automatically):

```bash
uv sync
```

### Configure

```bash
cp .env.example .env
```

Fill in the provider API keys and adjust model selections if needed. `.env.example` is the reference for every setting.

### Run

```bash
uv run idea-to-mvp
```

Describe your idea, choose rounds per speaker, and follow the gates. At the final gate you decide whether implementation agents build the product — the cost warning shows the model and per-session budget cap before anything runs.

### Troubleshooting

Run `uv run idea-to-mvp doctor` to verify your API keys, model IDs, and the agent CLI before a run (`--offline` checks configuration only). Optional LangSmith tracing is described in `.env.example`.

## Outputs

Everything is written under `OUTPUT_DIR` (default `~/idea-to-mvp`, deliberately outside this repository):

| Folder | Contents |
|---|---|
| `sessions.db` | Checkpoints and the session list: reopen the app, pick a session under *Saved sessions*, and continue where you stopped |
| `exports/` | Saved conversation Markdown files (any time, via *Save to Markdown*) |
| `blueprints/` | Blueprint packs: `README.md`, `PRD.md`, `ARCHITECTURE.md`, `AGENTS.md` guides, `plan.md`, `.claude/agents/*.md`, `STRATEGY.json` |
| `projects/` | Implemented projects (a copy of the blueprint plus the code the agents built and tested) |

## Configuration

All settings load from `.env`; [`.env.example`](.env.example) documents every variable and its default (providers and models per role, token budgets, implementation caps).

### Cost & security notes

- The implementation stage runs autonomous agents that write files and run commands on your machine, confined by an OS sandbox, a tool-call guard, and a scrubbed environment (`projects/<project>/` under `OUTPUT_DIR`, a git repository). This reduces the risk; it does not remove it — read [SECURITY.md](SECURITY.md) for what is and is not protected, and review `IMPLEMENTER_MAX_TOTAL_USD` (the whole-run cost cap) before starting.
- Nothing implementation-related runs without your explicit confirmation at the implementation gate.
- Never commit `.env` files or API keys.

## Development

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

## Project Layout

Source lives in `src/idea_to_mvp/`; the module map, pipeline internals, and conventions are in [docs/architecture.md](docs/architecture.md).

## Security

See `SECURITY.md` for vulnerability reporting guidance.

## License

This project is licensed under the MIT License. See `LICENSE`.
