# Idea-to-MVP Orchestrator

Turn a rough product idea into a **built, tested first version**. A LangGraph pipeline runs every stage with a dedicated agent: a multi-LLM panel debates the idea, an architect proposes options, a planner writes an agent-ready blueprint, and Claude Agent SDK agents implement the project in parallel and verify it. You approve every gate, and nothing that spends money runs without your say-so.

![The panel: three agents open in parallel, then debate; the stepper tracks the run](docs/assets/panel.png)

## Quick start (60 seconds)

You need [uv](https://docs.astral.sh/uv/); it installs the right Python (3.11 to 3.13) for you.

```bash
uv sync
DEMO_MODE=true uv run idea-to-mvp
```

Demo mode walks the whole pipeline (panel, gates, blueprint pack, a stand-in implementation, verification, delivery report) with canned outputs: no API keys, no spend. Type an idea, or open **Settings** and switch **Autopilot** on to ride through the gates.

For a real run:

```bash
cp .env.example .env            # add OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY
uv run idea-to-mvp doctor       # checks keys, model IDs, and the agent CLI before you spend anything
uv run idea-to-mvp models       # every profile's models, prices, and the cost of a typical run
uv run idea-to-mvp
```

Without the UI, the same pipeline runs from the command line (progress on stderr, a summary or `--json` on stdout; exit code 0 verified, 2 delivered with failures, 3 stopped at a gate, 1 error):

```bash
DEMO_MODE=true uv run idea-to-mvp run "a habit tracker" --autopilot --implement
uv run idea-to-mvp run "a habit tracker" --autopilot   # stops at the implement gate and prints the cost estimate
uv run idea-to-mvp sessions                            # saved sessions, from the UI or the CLI
uv run idea-to-mvp resume <session> --implement --max-total-usd 10
uv run idea-to-mvp publish <session> --dry-run        # what would go to a new GitHub repository; creates nothing
```

Publishing is opt-in too: set `GITHUB_TOKEN` (or sign in with `gh`), then use the iterate gate's *Publish to GitHub* or `publish <session>`. It shows the repository (private by default) and exactly what would be pushed, and creates and pushes only after you confirm; `.idea-to-mvp/` and secret files never leave the project folder.

Agents are only started with an explicit `--implement`. `--answers FILE` and `--arch 1|2` answer those gates yourself; `resume <session> --implement --feedback "..."` builds the next version.

To measure a configuration end to end, `idea-to-mvp bench` runs the eight golden ideas headless and scores them (verified deliveries, acceptance pass rate, fix attempts, cost, wall time); `bench --compare runA runB` sets two runs side by side. It spends money, so a real run needs `--live --max-total-usd N` (`--dry-run` prices it first); `DEMO_MODE=true uv run idea-to-mvp bench --name smoke` runs it offline. See `CONTRIBUTING.md`.

The implementation stage needs `ANTHROPIC_API_KEY` (it drives the Claude Agent SDK). Everything the app writes goes under `OUTPUT_DIR` (default `~/idea-to-mvp`, outside this repository).

`MODEL_PROFILE` picks the models of every role and agent session in one setting. Estimated cost of a typical run (a three-round panel and a three-task plan, from the prices in `src/idea_to_mvp/models.toml`; real runs vary):

| Profile | Models | Typical run |
|---|---|---|
| `fast` | the cheapest current models (Haiku 5.5, `gpt-6-luna`; `gemini-3.8-flash` for the Skeptic) | $0.17 |
| `balanced` (default) | Opus 5.5 plans, Sonnet 5.5 builds and verifies, Opus 5.5 takes merge conflicts and repeated fixes | $3.43 |
| `quality` | Opus 5.5 for the key panel roles, planning, and every agent session; Fable 5.1 for the hard cases | $5.47 |

## What you get

**Gates that ask the right question.** The MVP questions come prefilled with suggested answers; the architecture options sit side by side with a recommendation.

![The architecture gate: two option cards with a recommendation](docs/assets/architecture-gate.png)

**Agents you can watch.** Independent plan tasks run in parallel git worktrees while a live board, log, and cost meter follow them.

![The implementation board with task cards, cost meter, and live log](docs/assets/implementation-board.png)

**A delivery you can run.** Verification lanes for tests, quality, requirement coverage, and held-out acceptance tests the building agents never saw, a requirement-by-requirement table, how to run the project, a zip, a `v0.1` git tag, and a form to ask for v0.2.

![The delivery dashboard: verdict per lane, requirements table, cost and time](docs/assets/delivery-dashboard.png)

## The pipeline

```
Idea ─ (optional web research) ─→ Panel ─→ Summary + 5 MVP questions
  🔒 you answer  ─→ Architect: two options
  🔒 you choose  ─→ Strategy (and a starter template) ─→ Blueprint pack (PRD, architecture, plan, subagents, agent harness), reviewed by a critic
  🔒 you approve the blueprint and the spend
  ─→ Implementation (parallel tasks) ─→ Verification (4 lanes, fix loop) ─→ Delivery report
  🔁 ask for changes ─→ v0.2, same machinery
```

Every 🔒 is a real pause: the whole session is one checkpointed LangGraph thread, stored in SQLite, so you can close the app and pick up where you stopped.

## Why it's built this way

- **One durable graph with human gates.** `interrupt()` pauses and `Command(resume=...)` continues a single checkpointed thread, and the UI is a pure projection of that state, so restarts, Stop, and Continue need no extra bookkeeping.
- **Parallel where the work is independent.** `Send` fans out the panel's openings, the blueprint's documents, the plan's tasks (in git worktrees), and the verification lanes, each bounded by concurrency and budget caps.
- **Typed hand-offs and loops that check themselves.** Agents exchange validated Pydantic objects instead of prose, the plan is machine-checked, and a critic/revise loop and a verify/fix loop catch problems before you do.
- **Autonomous agents, but confined.** An OS sandbox, a tool-call guard, a scrubbed environment, and whole-run cost caps bound what the implementation agents can do; [SECURITY.md](SECURITY.md) says exactly what is and is not protected.
- **Offline-first, so it stays testable.** Every model call has a demo fixture, which lets CI run the entire pipeline (and three golden blueprint ideas) without a key.

## Learn more

- [docs/architecture.md](docs/architecture.md): the graph, the state contract, the LangGraph patterns used (each mapped to code and a test), and how to add an agent or a gate
- [.env.example](.env.example): every setting, with its default and meaning
- [SECURITY.md](SECURITY.md): the agent-execution threat model and how to report a vulnerability
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and quality checks
- [CHANGELOG.md](CHANGELOG.md): what changed, including upgrade notes

Before a real implementation run, check `IMPLEMENTER_MAX_TOTAL_USD` (the whole-run cost cap); the implementation gate shows the agents' models, sandbox status, and budget before anything starts. Never commit `.env` or API keys.

## License

MIT. See [LICENSE](LICENSE).
