# Phases 2–4: Blueprint v2, Strategy Agent, Agent SDK Implementation + Verification, UX & Docs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Executed inline by the planning author in the same session; tasks are TDD-ordered, each ends with the suite green and a LOCAL commit (never push).

**Goal:** Complete the idea-to-MVP pipeline: richer blueprints with auto-selected execution strategy (Subagents vs Agent Team), a real implementation stage driven by the Claude Agent SDK that builds the product in `generated_projects/`, a verification stage with a bounded fix loop, a delivery report, a stage-tracker UX, and full open-source docs.

**Architecture:** The Phase-1 linear interrupt graph is extended to:
`discussion ⟲ → summarizer → collect_answers[gate] → architect → arch_choice[gate] → strategy → planner_offer → plan_gate[gate] → plan_bundle → implement_gate[gate] → implementer → verifier → delivery_report → END` (declining at plan/implement gates routes to END). The Agent SDK work lives in a new `implementer.py`; blueprint generation moves to a new `blueprints.py`; the UI gains one generic decision radio whose choices are swapped per gate, plus a pipeline stage tracker.

**Tech stack:** Phase-1 stack + `claude-agent-sdk` 0.2.97 (verified installed; `query`, `ClaudeAgentOptions`, `AgentDefinition` introspected).

**Spec:** `docs/superpowers/specs/2026-06-11-orchestrator-overhaul-design.md` (Phases 2–4 roadmap).

---

## New/changed contracts

### State (`state.py`)
```python
Stage += "arch_choice" | "strategy" | "implement_gate" | "implementation" | "verification" | "report"

class ArchChoice(TypedDict): option: str; notes: str            # option: "A" | "B"
class Workstream(TypedDict): name: str; focus: str; deliverables: str
class ExecutionStrategy(TypedDict): mode: str; reasoning: str; workstreams: list[Workstream]  # mode: "subagents"|"agent_team"
class ImplementDecision(TypedDict): implement: bool; notes: str
class VerificationResult(TypedDict): passed: bool; attempts: int; report: str

IdeaDiscussionState += arch_choice, execution_strategy, implement_decision,
                       workspace_dir, implementation_log, verification, delivery_report
```

### Interrupt payload kinds (UI contract)
| kind | payload extras | resume value |
|---|---|---|
| `answers` | summary, questions | str |
| `arch_choice` | architecture, question | `{"option": "A"\|"B", "notes": str}` |
| `plan_gate` | question, architecture | `{"generate": bool, "notes": str}` |
| `implement_gate` | question (incl. model + cost warning), workspace_note | `{"implement": bool, "notes": str}` |

### Settings (`config.py`)
```python
strategy uses architect_* settings (registry entry "strategy")
implementer_model: str = "claude-opus-4-8"
implementer_permission_mode: str = "bypassPermissions"   # sandboxed by cwd; documented caveat
implementer_max_turns: int = 120
implementer_max_budget_usd: float = 10.0
verifier_max_turns: int = 40
max_fix_attempts: int = 2
```

### `blueprints.py` (new)
Owns blueprint v2 prompts + file specs + `create_project_bundle(...)`. Takes a `generate_doc(system_prompt, instruction) -> str` callable injected by `agents.py` (no circular import). Writes, per bundle:
`README.md, PRD.md, ARCHITECTURE.md, AGENTS.md, contracts/AGENTS.md, application/AGENTS.md, quality/AGENTS.md, plan.md` (LLM-generated; plan.md gains Acceptance-criteria field), `.claude/agents/<workstream>.md` (one per workstream, YAML frontmatter `name`/`description` + body system prompt), `STRATEGY.json` (deterministic dump of execution_strategy).

### `implementer.py` (new)
```python
prepare_workspace(bundle_dir: Path, projects_root: Path) -> Path     # copies blueprint into generated_projects/<ts>-<slug>/
run_implementation(workspace, strategy, settings) -> str            # SDK; subagents mode = one lead query with agents={...}; agent_team = sequential query per workstream
run_verification(workspace, settings) -> dict                       # SDK; verdict parsed from "VERDICT: PASS|FAIL" sentinel
run_fix(workspace, report, settings) -> str                         # SDK; feed failure report back
_parse_verdict(text) -> bool | None                                  # pure, unit-tested
_build_agent_definitions(strategy) -> dict[str, AgentDefinition]     # pure-ish, unit-tested
```
All SDK calls run through one `_run_agent(prompt, *, workspace, settings, agents=None, max_turns)` helper using `asyncio.run(query(...))`, `permission_mode` from settings, `cwd=workspace`, `max_budget_usd` cap, collecting the final `ResultMessage.result`.

### UI modes (submit_service)
`idea, answers, arch_choice, plan_gate, implement_gate, done`. One `decision_radio` component; `_pack` swaps `choices`/`visible` per mode. Tracker HTML appended as output index 11 (existing 0–10 unchanged so Phase-1 POS_* constants stay valid).

---

## Tasks (each: tests first → implement → suite green + ruff → local commit)

### Task 1 — Phase 2 core: state, strategy role, arch-choice gate, strategy node, graph order
- `state.py`: contracts above. `roles.py`: `STRATEGY_SYSTEM` (strict-JSON output, mode heuristics, 2–5 kebab-case workstreams) + `"strategy"` RoleSpec on architect settings.
- `agents.py`: `arch_choice_node` (interrupt kind `arch_choice`), `strategy_node` (parse JSON w/ fence-stripping + deterministic fallback strategy), stage updates.
- `graph.py`: `architect → arch_choice → strategy → planner_offer`.
- Tests (`test_agents_routing.py` + new `test_strategy.py`): lifecycle now hits 3 interrupts before bundle (answers, arch_choice, plan_gate); strategy JSON parsing incl. fenced/invalid input fallback.

### Task 2 — Phase 2 blueprints v2
- New `blueprints.py` per contract; `agents.plan_bundle_node` passes strategy/arch-choice context and the `generate_doc` closure; old bundle prompts/specs removed from `agents.py`.
- New `tests/test_blueprints.py`: full file set incl. `.claude/agents/*.md` per workstream + valid `STRATEGY.json`; frontmatter check.

### Task 3 — Phase 3 SDK implementer module
- `config.py` settings above; `implementer.py` per contract.
- New `tests/test_implementer.py`: `prepare_workspace` copies bundle; `_parse_verdict` PASS/FAIL/None; `_build_agent_definitions` maps workstreams; verification fix-loop logic tested at node level in Task 4.

### Task 4 — Phase 3 graph nodes: implement gate, implementer, verifier, delivery report
- `agents.py`: `implement_gate_node` (cost warning text w/ model name), `route_after_implement_gate`, `implementer_node`, `verifier_node` (bounded fix loop, `max_fix_attempts`), `delivery_report_node` (deterministic markdown: workspace, file tree ≤ 60 entries, verdict, how-to-run pointer).
- `graph.py`: `plan_bundle → implement_gate → implementer|END; implementer → verifier → delivery_report → END`.
- Tests: lifecycle accept-path reaches `implement_gate`; accepting (with `agents.run_implementation`/`run_verification`/`run_fix` monkeypatched) produces verification + delivery report; declining ends; fix loop attempts counted.

### Task 5 — UI: generic decision gate + new modes
- `submit_service.py`: modes/choices per contract; `_apply_interrupt` handles 4 kinds; `_run` renders strategy/implementer/verifier/delivery-report events; tracker stage line (Task 6 adds visuals). `app.py`: radio renamed `decision_radio` w/ dynamic choices, outputs unchanged-order + tracker placeholder.
- `tests/test_submit_service.py`: arch-choice resume `{"option","notes"}`; implement-gate resume `{"implement","notes"}`; full mode walk.

### Task 6 — Phase 4 UX: stage tracker + CSS polish
- `render.py`: `stage_tracker(stage)` pill bar (9 stages), CSS for pills/cards; `app.py` header + tracker `gr.HTML` output (index 11).
- Tests: tracker marks active/done stages; smoke test still green.

### Task 7 — Phase 4 docs + final verification
- `README.md` full rewrite (pipeline diagram, all gates, implementation stage, cost/security caveats, env table), `.env.example` (+ new vars), `CLAUDE.md`, `CHANGELOG.md`, `.gitignore` (+`generated_projects/`), `requirements.txt` (+`claude-agent-sdk`).
- `pytest`, `ruff check .`, UI smoke import. Local merge to `main` per user instruction pattern (no push).

## Verification commands
`.venv/bin/pytest -q` · `.venv/bin/ruff check .` · `.venv/bin/python -c "from app import make_ui; from config import Settings; make_ui(settings=Settings()); print('UI OK')"`
