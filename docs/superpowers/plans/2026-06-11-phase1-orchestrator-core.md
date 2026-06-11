# Phase 1: Orchestrator Core Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the simulated phase-flag pipeline with a single linear LangGraph using real `interrupt()`-based human-in-the-loop gates, a declarative role registry, overhauled prompts, and a simplified `SubmitService` — all on one checkpointer thread per session.

**Architecture:** One linear graph (`discussion ⟲ → summarizer → collect_answers[interrupt] → architect → planner_offer → plan_gate[interrupt] → plan_bundle|END`). The UI resumes the paused graph with `Command(resume=...)`; UI mode mirrors which interrupt the graph is paused at. Roles (provider/model/token/prompt wiring) live in a new `roles.py` registry.

**Tech Stack:** Python 3.11+, LangGraph 1.1.6 (`langgraph.types.interrupt`, `Command`), LangChain providers (openai/anthropic/google), Gradio, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-06-11-orchestrator-overhaul-design.md`

**IMPORTANT: commits are LOCAL ONLY. Never `git push` anything in this plan.**

---

## File structure

| File | Action | Responsibility after Phase 1 |
|---|---|---|
| `roles.py` | Create | All system prompts for pipeline roles + `RoleSpec` registry + speaker-order derivations |
| `state.py` | Rewrite | New state contract: `stage`, `plan_decision`; `phase`/`planning_request` removed |
| `agents.py` | Modify | Node functions, LLM invocation, blueprint generation. Prompts/wiring move out to `roles.py`; gains `collect_answers_node`, `plan_gate_node`, `route_after_plan_gate`; loses `route_from_start` |
| `graph.py` | Modify | New linear graph with the two interrupt nodes |
| `submit_service.py` | Rewrite | Thin stream-runner: starts/resumes one graph thread, maps node + interrupt events to UI updates |
| `app.py` | Modify | Adds plan-gate radio control; drops `questions_state`/`summary_state` |
| `tests/test_roles.py` | Create | Registry completeness + derivation tests |
| `tests/test_agents_routing.py` | Rewrite | Routing units + full graph interrupt/resume lifecycle with fake LLM |
| `tests/test_submit_service.py` | Rewrite | Stream-runner mode transitions per pause point |

`render.py`, `exporter.py`, `config.py`, `text_utils.py`, `tests/test_app_smoke.py`, `tests/test_text_utils.py`: unchanged.

---

### Task 1: Role registry (`roles.py`) with overhauled prompts

**Files:**
- Create: `roles.py`
- Create: `tests/test_roles.py`
- Modify: `agents.py` (delete prompt constants, import from `roles`, replace 3 cached runtime getters with generic `get_runtime`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_roles.py`:

```python
from config import Settings
from roles import DISCUSSION_ROLE_KEYS, ROLES, SPEAKER_ORDER, SPEAKER_NAME_TOKEN, TOKEN_TO_SPEAKER


def test_every_role_references_real_settings_fields() -> None:
    settings = Settings()
    for spec in ROLES.values():
        assert isinstance(getattr(settings, spec.provider_setting), str), spec.key
        assert isinstance(getattr(settings, spec.model_setting), str), spec.key
        assert isinstance(getattr(settings, spec.max_tokens_setting), int), spec.key
        assert spec.system_prompt.strip(), spec.key
        assert spec.display_name.strip(), spec.key


def test_expected_roles_present() -> None:
    assert set(ROLES) == {"pm", "tech_lead", "skeptic", "summarizer", "architect", "planner"}


def test_speaker_order_derives_from_registry() -> None:
    assert SPEAKER_ORDER == ["PM", "Tech Lead", "Skeptic"]
    assert list(DISCUSSION_ROLE_KEYS) == ["pm", "tech_lead", "skeptic"]
    assert SPEAKER_NAME_TOKEN == {"PM": "pm", "Tech Lead": "tech_lead", "Skeptic": "skeptic"}
    assert TOKEN_TO_SPEAKER == {"pm": "PM", "tech_lead": "Tech Lead", "skeptic": "Skeptic"}


def test_skeptic_prompt_requires_constructive_pairing() -> None:
    assert "cheapest test" in ROLES["skeptic"].system_prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_roles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'roles'`

- [ ] **Step 3: Create `roles.py`**

Full content. The prompts are the overhauled versions (changes vs. today: shared dedupe rule; constructive Skeptic; summarizer "Disagreements and resolutions" section; decision-leverage question test; architect anchored to user constraints + named tradeoff):

```python
from __future__ import annotations

from dataclasses import dataclass

SHARED_DISCUSSION_RULES = (
    "You are in a live panel with two other AIs. The human message is the anchor idea, "
    "and the remaining messages are prior panel turns.\n\n"
    "How to participate:\n"
    "- Read the whole thread and directly reference at least one concrete claim from another panelist.\n"
    "- Never restate a point already made; reference it by speaker name and either build on it or disagree with it.\n"
    "- Prioritize this order in every turn: (1) functionality and user workflows, (2) technical implementation, "
    "(3) business model/GTM.\n"
    "- Cover the full arc when relevant: problem and user workflow -> scope/MVP -> build/ops -> validation/launch "
    "-> business model/pricing -> risks.\n"
    "- Improve the idea with concrete tradeoffs, metrics, and what to cut.\n"
    "- Use concise Markdown with short sections and bullets.\n"
    "- Keep each turn compact: target 200-250 words, max 7 bullets total.\n"
    "- Avoid generic essays and avoid repeating the idea verbatim.\n"
    "- Keep outputs RAG-friendly: use explicit claims, assumptions, and decisions as bullet points.\n"
)

PM_SYSTEM = (
    "You are PM in an idea lab: product strategist and market shaper.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: core user journeys, MVP functionality, feature prioritization, and implementation-ready scope. "
    "After that, add business model implications only if they affect product decisions. "
    "Each turn, expose 1-2 risky assumptions and suggest 2-3 practical improvements.\n"
    "End with **Functionality recommendation** and **Implementation-scoped next test**.\n"
)

TECH_LEAD_SYSTEM = (
    "You are Tech Lead in an idea lab: pragmatic architect and delivery realist.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: architecture choices, stack constraints, integration complexity, operational reliability, "
    "and build sequencing for the highest-priority product functionality. "
    "Address business model only after feasibility and implementation are clear. "
    "React to PM/Skeptic claims with feasibility notes and hidden technical risk.\n"
    "End with **Tech / build notes** and **Validation steps**.\n"
)

SKEPTIC_SYSTEM = (
    "You are Skeptic in an idea lab: critical analyst focused on failure modes.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: weak assumptions in functionality, technical design gaps, edge cases, adoption friction, "
    "and legal/compliance concerns. Raise business-model concerns after product and implementation risks.\n"
    "Challenge overconfident claims and force evidence-backed decisions.\n"
    "Pair every objection with the cheapest test or design change that would retire the risk; "
    "never raise a problem without a concrete way to resolve it.\n"
    "End with **Pushback on functionality/implementation** and **Evidence needed next**.\n"
)

SUMMARY_SYSTEM = (
    "You are a neutral session synthesizer. You receive the user's idea and the full multi-agent discussion.\n"
    "Produce a concise Markdown brief the user can immediately execute:\n\n"
    "## Executive summary\n"
    "(3-4 concise sentences)\n\n"
    "## Functional specification snapshot\n"
    "(2-4 bullets: primary user workflows, core capabilities, clear MVP boundaries)\n\n"
    "## Technical implementation snapshot\n"
    "(2-4 bullets: architecture shape, key integrations, sequencing, operational constraints)\n\n"
    "## Disagreements and resolutions\n"
    "(2-4 bullets: where panelists disagreed, how or whether each disagreement was resolved, what remains open. "
    "This section feeds the architect; never omit it when any disagreement occurred.)\n\n"
    "## Refined idea\n"
    "(2-3 bullets: how the concept evolved after functionality + technical discussion)\n\n"
    "## SWOT snapshot\n"
    "- Strengths\n- Weaknesses\n- Opportunities\n- Threats\n\n"
    "## Business model notes\n"
    "(2-3 bullets only; add only notes that affect MVP decisions)\n\n"
    "## Risks & mitigations\n"
    "(3-5 bullets)\n\n"
    "## Recommended next steps\n"
    "(4-6 bullets; prioritize functionality then implementation then business tests)\n\n"
    "Hard limits: keep total output under 650 words and avoid repeating similar points."
)

QUESTIONS_SYSTEM = (
    "You are an MVP planning interviewer.\n"
    "Generate exactly 5 high-leverage questions that must be answered before implementing the MVP.\n"
    "Apply this decision-leverage test to every question: if all plausible answers would lead to the same "
    "architecture and scope, discard the question and write a stronger one.\n"
    "Questions must be:\n"
    "- directly actionable for scoping and architecture decisions\n"
    "- specific to this idea (user workflows, scope, data model, integrations, constraints, then business model)\n"
    "- answerable in free text\n"
    "- one line each, max 24 words\n"
    "Output format (strict):\n"
    "1. ...\n2. ...\n3. ...\n4. ...\n5. ...\n"
    "Do not output any extra sections or commentary."
)

ARCHITECT_SYSTEM = (
    "You are Architect, a principal system architect focused on turning validated MVP ideas into practical implementation plans.\n"
    "You receive: (1) original idea, (2) discussion summary, (3) clarification questions, (4) user answers.\n"
    "Your job is to produce exactly two architecture options at the right level for MVP planning.\n\n"
    "Output format (strict):\n"
    "## Option A - Fast implementation and maintainability\n"
    "- Proposed architecture style and key services/components\n"
    "- Recommended languages/frameworks/tools\n"
    "- State and persistence approach (high-level only)\n"
    "- Integrations and infrastructure\n"
    "- Security/reliability baseline\n"
    "- Tradeoffs and expected limits\n\n"
    "## Option B - Maximum performance and scale\n"
    "- Proposed architecture style and key services/components\n"
    "- Recommended languages/frameworks/tools\n"
    "- State and persistence approach (high-level only)\n"
    "- Integrations and infrastructure\n"
    "- Security/reliability baseline\n"
    "- Tradeoffs and expected limits\n\n"
    "## Shared components (if applicable)\n"
    "(List overlaps if the two options have common parts. Similarity is acceptable.)\n\n"
    "## Architect recommendation\n"
    "- Select one architecture as the ideal target for high performance and high load.\n"
    "- Explain why it is still suitable for quick MVP launch.\n"
    "- Name the single biggest tradeoff you are accepting with this recommendation.\n"
    "- Provide a phased rollout: MVP phase -> scale-up phase.\n\n"
    "Constraints:\n"
    "- Anchor each option to at least two explicit constraints taken from the user's answers; name them inline.\n"
    "- Keep output concise and practical (target 350-450 words total).\n"
    "- Do NOT be overly detailed. Prefer high-level choices over walkthroughs.\n"
    "- Keep each bullet to one short sentence and do not use sub-bullets.\n"
    "- Do not add implementation examples, edge-case catalogs, or long justification paragraphs.\n"
    "- Do NOT include database table schemas, ERDs, field-by-field models, or code/type definitions.\n"
    "- Focus on system shape and implementation decisions only."
)

PLAN_OFFER_SYSTEM = (
    "You are a pragmatic delivery lead.\n"
    "Based on the idea, summary, answers, and architecture, ask one strong yes/no question inviting the user "
    "to generate an agent-ready execution pack.\n"
    "That pack includes AGENTS.md guidance files plus a complete MVP `plan.md` with task order, data contracts, "
    "and testing expectations.\n"
    "Constraints:\n"
    "- 1-2 sentences total\n"
    "- under 45 words\n"
    "- direct, concrete, and implementation-focused\n"
    "- mention the pack contents in natural language\n"
    "- do not add greetings or extra commentary"
)


@dataclass(frozen=True)
class RoleSpec:
    """Declarative wiring for one pipeline agent.

    The three *_setting fields name attributes on config.Settings so the
    registry stays in sync with environment-driven configuration.
    """

    key: str
    display_name: str
    provider_setting: str
    model_setting: str
    max_tokens_setting: str
    system_prompt: str


ROLES: dict[str, RoleSpec] = {
    "pm": RoleSpec("pm", "PM", "pm_provider", "pm_model", "discussion_max_tokens", PM_SYSTEM),
    "tech_lead": RoleSpec(
        "tech_lead", "Tech Lead", "tech_lead_provider", "tech_lead_model", "discussion_max_tokens", TECH_LEAD_SYSTEM
    ),
    "skeptic": RoleSpec(
        "skeptic", "Skeptic", "skeptic_provider", "skeptic_model", "skeptic_max_tokens", SKEPTIC_SYSTEM
    ),
    "summarizer": RoleSpec(
        "summarizer", "Summarizer", "summarizer_provider", "summarizer_model", "summary_max_tokens", SUMMARY_SYSTEM
    ),
    "architect": RoleSpec(
        "architect", "Architect", "architect_provider", "architect_model", "summary_max_tokens", ARCHITECT_SYSTEM
    ),
    "planner": RoleSpec(
        "planner", "Planner", "architect_provider", "architect_model", "summary_max_tokens", PLAN_OFFER_SYSTEM
    ),
}

DISCUSSION_ROLE_KEYS: tuple[str, ...] = ("pm", "tech_lead", "skeptic")
SPEAKER_ORDER: list[str] = [ROLES[key].display_name for key in DISCUSSION_ROLE_KEYS]
SPEAKER_NAME_TOKEN: dict[str, str] = {ROLES[key].display_name: key for key in DISCUSSION_ROLE_KEYS}
TOKEN_TO_SPEAKER: dict[str, str] = {token: name for name, token in SPEAKER_NAME_TOKEN.items()}
```

- [ ] **Step 4: Update `agents.py` to use the registry**

4a. Delete these constants from `agents.py` (lines 26–160 in the current file): `SPEAKER_ORDER`, `SPEAKER_NAME_TOKEN`, `TOKEN_TO_SPEAKER`, `SHARED_DISCUSSION_RULES`, `PM_SYSTEM`, `TECH_LEAD_SYSTEM`, `SKEPTIC_SYSTEM`, `SUMMARY_SYSTEM`, `QUESTIONS_SYSTEM`, `ARCHITECT_SYSTEM`, `PLAN_OFFER_SYSTEM`. Keep `LOGGER` and the blueprint prompts (`ROOT_AGENTS_SYSTEM`, `CONTRACTS_AGENTS_SYSTEM`, `APPLICATION_AGENTS_SYSTEM`, `QUALITY_AGENTS_SYSTEM`, `PLAN_SYSTEM`) — those are Phase 2 scope.

4b. Extend the dual-import block:

```python
try:
    from .config import Provider, clear_settings_cache, get_settings
    from .roles import (
        DISCUSSION_ROLE_KEYS,
        QUESTIONS_SYSTEM,
        ROLES,
        SPEAKER_NAME_TOKEN,
        SPEAKER_ORDER,
        SUMMARY_SYSTEM,
        TOKEN_TO_SPEAKER,
    )
    from .state import IdeaDiscussionState
    from .text_utils import normalize_content
except ImportError:
    from config import Provider, clear_settings_cache, get_settings
    from roles import (
        DISCUSSION_ROLE_KEYS,
        QUESTIONS_SYSTEM,
        ROLES,
        SPEAKER_NAME_TOKEN,
        SPEAKER_ORDER,
        SUMMARY_SYSTEM,
        TOKEN_TO_SPEAKER,
    )
    from state import IdeaDiscussionState
    from text_utils import normalize_content
```

4c. Replace `get_discussion_runtimes`, `get_summarizer_runtime`, `get_architect_runtime` (current lines 331–395) with:

```python
@lru_cache(maxsize=None)
def get_runtime(role_key: str) -> AgentRuntime:
    settings = get_settings()
    spec = ROLES[role_key]
    configured_provider = getattr(settings, spec.provider_setting)
    model = getattr(settings, spec.model_setting)
    max_tokens = getattr(settings, spec.max_tokens_setting)
    return AgentRuntime(
        llm=_build_llm(configured_provider, model, max_tokens),
        provider=_resolve_provider(configured_provider, model),
        model=model,
        max_tokens=max_tokens,
        system_prompt=spec.system_prompt,
    )


def get_discussion_runtimes() -> dict[str, AgentRuntime]:
    return {ROLES[key].display_name: get_runtime(key) for key in DISCUSSION_ROLE_KEYS}
```

4d. Update all call sites inside `agents.py`:
- `summarizer_node`: `summarizer = get_summarizer_runtime()` → `summarizer = get_runtime("summarizer")`. Keep using `SystemMessage(content=SUMMARY_SYSTEM)` and `SystemMessage(content=QUESTIONS_SYSTEM)` (both now imported from `roles`).
- `_run_architect`: `architect = get_architect_runtime()` → `architect = get_runtime("architect")`.
- `planner_offer_node`: `architect = get_architect_runtime()` → `planner = get_runtime("planner")`; use `SystemMessage(content=planner.system_prompt)` instead of `SystemMessage(content=PLAN_OFFER_SYSTEM)`; rename local references accordingly (`_invoke_with_runtime(planner, ...)`, `isinstance(planner.llm, ChatOpenAI)`, `_log_openai_response("planner_offer", planner.model, response)`).
- `_create_project_bundle`: `runtime = get_architect_runtime()` → `runtime = get_runtime("architect")`.
- `clear_runtime_caches`:

```python
def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_runtime.cache_clear()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_roles.py -v && .venv/bin/pytest && .venv/bin/ruff check .`
Expected: all PASS, lint clean (the old suite still passes — `SPEAKER_ORDER` etc. are re-exported via `agents`).

- [ ] **Step 6: Commit (local only — do not push)**

```bash
git add roles.py agents.py tests/test_roles.py
git commit -m "feat: extract declarative role registry with overhauled prompts"
```

---

### Task 2: New state contract, interrupt gates, and linear graph

**Files:**
- Rewrite: `state.py`
- Modify: `agents.py` (gate nodes, round-aware discussion prompt, stage updates, delete `route_from_start`)
- Modify: `graph.py`
- Rewrite: `tests/test_agents_routing.py`

- [ ] **Step 1: Rewrite `tests/test_agents_routing.py` (failing first)**

Full new content:

```python
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

import agents
import graph as graph_module


class FakeRuntime:
    def __init__(self, system_prompt: str = "system") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = system_prompt


def _base_state(max_rounds: int = 3) -> dict[str, Any]:
    return {
        "user_idea": "idea",
        "discussion_history": [HumanMessage(content="idea")],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "plan_offer_question": "",
        "plan_decision": {"generate": False, "notes": ""},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "stage": "discussion",
        "next_speaker": "PM",
        "max_rounds": max_rounds,
        "turn_count": 0,
    }


@pytest.fixture()
def fake_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured_prompts: list[str] = []

    def fake_get_runtime(role_key: str) -> FakeRuntime:
        return FakeRuntime(system_prompt=f"system::{role_key}")

    def fake_invoke(runtime, messages, *, max_tokens=None):
        captured_prompts.append("\n".join(str(m.content) for m in messages))
        return AIMessage(content="1. What is the MVP scope?")

    monkeypatch.setattr(agents, "get_runtime", fake_get_runtime)
    monkeypatch.setattr(agents, "_invoke_with_runtime", fake_invoke)
    monkeypatch.setattr(agents, "_project_bundle_root", lambda: tmp_path / "project_blueprints")
    graph_module.clear_graph_cache()
    yield captured_prompts
    graph_module.clear_graph_cache()


def test_route_after_discussion_loops_until_max() -> None:
    state = _base_state(max_rounds=3)
    state["turn_count"] = 2
    assert agents.route_after_discussion(state) == "discussion"


def test_route_after_discussion_moves_to_summarizer() -> None:
    state = _base_state(max_rounds=3)
    state["turn_count"] = 3
    assert agents.route_after_discussion(state) == "summarizer"


def test_route_after_plan_gate() -> None:
    declined = _base_state()
    declined["plan_decision"] = {"generate": False, "notes": ""}
    assert agents.route_after_plan_gate(declined) == "__end__"
    accepted = _base_state()
    accepted["plan_decision"] = {"generate": True, "notes": "go"}
    assert agents.route_after_plan_gate(accepted) == "plan_bundle"


def test_full_pipeline_pauses_resumes_and_declines(fake_pipeline) -> None:
    g = graph_module.build_graph(True)
    config = {"configurable": {"thread_id": "lifecycle-decline"}}

    events = list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    interrupts = [e["__interrupt__"][0] for e in events if "__interrupt__" in e]
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "answers"
    assert len(interrupts[0].value["questions"]) == 5

    events = list(g.stream(Command(resume="1. Solo founders."), config=config, stream_mode="updates"))
    interrupts = [e["__interrupt__"][0] for e in events if "__interrupt__" in e]
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "plan_gate"
    assert interrupts[0].value["question"].strip()

    events = list(g.stream(Command(resume={"generate": False, "notes": ""}), config=config, stream_mode="updates"))
    assert not any("__interrupt__" in e for e in events)
    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["plan_decision"] == {"generate": False, "notes": ""}
    assert snapshot.values["user_answers"] == "1. Solo founders."
    assert snapshot.values["stage"] == "done"


def test_full_pipeline_accept_generates_bundle(fake_pipeline, tmp_path) -> None:
    g = graph_module.build_graph(True)
    config = {"configurable": {"thread_id": "lifecycle-accept"}}

    list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    list(g.stream(Command(resume="Answers."), config=config, stream_mode="updates"))
    list(g.stream(Command(resume={"generate": True, "notes": "keep it lean"}), config=config, stream_mode="updates"))

    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["plan_decision"] == {"generate": True, "notes": "keep it lean"}
    assert snapshot.values["project_bundle_files"] == [
        "AGENTS.md",
        "contracts/AGENTS.md",
        "application/AGENTS.md",
        "quality/AGENTS.md",
        "plan.md",
    ]
    bundle_dir = tmp_path / "project_blueprints"
    assert any(bundle_dir.iterdir())


def test_discussion_prompt_is_round_aware(fake_pipeline) -> None:
    state = _base_state(max_rounds=6)  # 2 rounds per speaker
    agents.discussion_node(state)
    assert "round 1 of 2" in fake_pipeline[-1]
    state["turn_count"] = 5  # last turn of final round
    agents.discussion_node(state)
    assert "round 2 of 2" in fake_pipeline[-1]
    assert "FINAL round" in fake_pipeline[-1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_agents_routing.py -v`
Expected: FAIL — `AttributeError: module 'agents' has no attribute 'route_after_plan_gate'` (and lifecycle tests fail on missing nodes).

- [ ] **Step 3: Rewrite `state.py`**

Full new content:

```python
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

Stage = Literal[
    "discussion",
    "summary",
    "answers",
    "architecture",
    "plan_gate",
    "plan_bundle",
    "done",
]
SpeakerName = Literal["PM", "Tech Lead", "Skeptic"]


class PlanDecision(TypedDict):
    """Structured result of the plan gate interrupt."""

    generate: bool
    notes: str


class IdeaDiscussionState(TypedDict):
    """State shared by all nodes in the orchestrator graph."""

    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    generated_questions: list[str]
    user_answers: str
    architecture: str
    plan_offer_question: str
    plan_decision: PlanDecision
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    stage: Stage
    next_speaker: SpeakerName
    max_rounds: int
    turn_count: int
```

- [ ] **Step 4: Update `agents.py` nodes**

4a. Add to imports: `from langgraph.types import interrupt` (top-level import, not in the try/except block) and extend the state import to include `PlanDecision`:

```python
    from .state import IdeaDiscussionState, PlanDecision   # and same in except branch
```

4b. In `discussion_node`, replace the `turn_prompt = (...)` assignment with:

```python
    speakers_count = len(SPEAKER_ORDER)
    total_rounds = max(1, state["max_rounds"] // speakers_count)
    current_round = min(total_rounds, state["turn_count"] // speakers_count + 1)
    round_rules = f"- This is your round {current_round} of {total_rounds} in this panel.\n"
    if current_round >= total_rounds:
        round_rules += (
            "- This is your FINAL round: converge instead of expanding. State your position on the open "
            "disagreements, what you would commit to building, and what you would cut. Do not open new threads.\n"
        )
    turn_prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        "Panel transcript so far (chronological):\n"
        f"{thread_md}\n\n"
        f"You are **{speaker}**. Write the next panel turn.\n"
        "Rules for this turn:\n"
        f"{round_rules}"
        "- Explicitly reference at least one prior panelist by role name (PM, Tech Lead, Skeptic).\n"
        "- Build on or challenge a concrete claim from the transcript.\n"
        "- Add net-new decisions/assumptions/tradeoffs rather than repeating prior text.\n"
        "- Keep it concise, structured, and decision-oriented.\n"
        "- Use at most 2 short sections and at most 6 bullets total.\n"
    )
```

4c. Add `"stage"` to each node's return value:
- `discussion_node` return: add `"stage": "discussion"`.
- `summarizer_node` return: `{"summary": summary_text, "generated_questions": questions, "stage": "summary"}`.
- `architect_node` return: `{"architecture": architecture, "stage": "architecture"}`.
- `planner_offer_node` return: `{"plan_offer_question": question or _default_plan_offer_question(), "stage": "plan_gate"}`.
- `plan_bundle_node` return: add `"stage": "done"`, and change the `planning_request=` argument to `planning_request=state.get("plan_decision", {}).get("notes", "")`.

4d. Delete `route_from_start` entirely. Add the two gate nodes and the new router (place them next to `route_after_discussion`):

```python
def collect_answers_node(state: IdeaDiscussionState) -> dict[str, Any]:
    answers = interrupt(
        {
            "kind": "answers",
            "summary": state.get("summary", ""),
            "questions": state.get("generated_questions", []),
        }
    )
    return {"user_answers": str(answers or "").strip(), "stage": "answers"}


def plan_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    decision = interrupt(
        {
            "kind": "plan_gate",
            "question": state.get("plan_offer_question") or _default_plan_offer_question(),
            "architecture": state.get("architecture", ""),
        }
    )
    if isinstance(decision, dict):
        generate = bool(decision.get("generate"))
        notes = str(decision.get("notes") or "").strip()
    else:
        generate = bool(decision)
        notes = ""
    plan_decision: PlanDecision = {"generate": generate, "notes": notes}
    return {"plan_decision": plan_decision, "stage": "plan_bundle" if generate else "done"}


def route_after_plan_gate(state: IdeaDiscussionState) -> Literal["plan_bundle", "__end__"]:
    if state.get("plan_decision", {}).get("generate"):
        return "plan_bundle"
    return "__end__"
```

- [ ] **Step 5: Rewrite `graph.py`**

Full new content:

```python
from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

try:
    from .agents import (
        architect_node,
        collect_answers_node,
        discussion_node,
        plan_bundle_node,
        plan_gate_node,
        planner_offer_node,
        route_after_discussion,
        route_after_plan_gate,
        summarizer_node,
    )
    from .config import get_settings
    from .state import IdeaDiscussionState
except ImportError:
    from agents import (
        architect_node,
        collect_answers_node,
        discussion_node,
        plan_bundle_node,
        plan_gate_node,
        planner_offer_node,
        route_after_discussion,
        route_after_plan_gate,
        summarizer_node,
    )
    from config import get_settings
    from state import IdeaDiscussionState


@lru_cache(maxsize=1)
def build_graph(enable_checkpointer: bool | None = None) -> CompiledStateGraph:
    if enable_checkpointer is None:
        enable_checkpointer = get_settings().enable_checkpointer
    builder = StateGraph(IdeaDiscussionState)
    builder.add_node("discussion", discussion_node)
    builder.add_node("summarizer", summarizer_node)
    builder.add_node("collect_answers", collect_answers_node)
    builder.add_node("architect", architect_node)
    builder.add_node("planner_offer", planner_offer_node)
    builder.add_node("plan_gate", plan_gate_node)
    builder.add_node("plan_bundle", plan_bundle_node)

    builder.add_edge(START, "discussion")
    builder.add_conditional_edges(
        "discussion",
        route_after_discussion,
        {"discussion": "discussion", "summarizer": "summarizer"},
    )
    builder.add_edge("summarizer", "collect_answers")
    builder.add_edge("collect_answers", "architect")
    builder.add_edge("architect", "planner_offer")
    builder.add_edge("planner_offer", "plan_gate")
    builder.add_conditional_edges(
        "plan_gate",
        route_after_plan_gate,
        {"plan_bundle": "plan_bundle", "__end__": END},
    )
    builder.add_edge("plan_bundle", END)

    if enable_checkpointer:
        return builder.compile(checkpointer=MemorySaver())
    return builder.compile()


def clear_graph_cache() -> None:
    build_graph.cache_clear()
```

- [ ] **Step 6: Run the task's tests**

Run: `.venv/bin/pytest tests/test_agents_routing.py tests/test_roles.py -v`
Expected: all PASS.

Note: `tests/test_submit_service.py` is expected to FAIL at this point (it still passes `phase` and the old positional args) — it is rewritten in Task 3. Do not "fix" it here.

- [ ] **Step 7: Commit (local only — do not push)**

```bash
git add state.py agents.py graph.py tests/test_agents_routing.py
git commit -m "feat: interrupt-based HITL gates on a single linear graph"
```

---

### Task 3: `SubmitService` rewrite + `app.py` wiring

**Files:**
- Rewrite: `submit_service.py`
- Modify: `app.py`
- Rewrite: `tests/test_submit_service.py`

- [ ] **Step 1: Rewrite `tests/test_submit_service.py` (failing first)**

Full new content:

```python
from langchain_core.messages import AIMessage
from langgraph.types import Command

from config import Settings
from submit_service import (
    MODE_ANSWERS,
    MODE_DONE,
    MODE_IDEA,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
    PLAN_CHOICE_SKIP,
    AppContext,
    SubmitService,
)

# Output tuple positions (see SubmitService._pack):
# 0 status, 1 chatbot, 2 input_tb, 3 rounds_sl, 4 plan_choice, 5 run_btn,
# 6 thread_id, 7 mode, 8 turns_state, 9 transcript_state, 10 chat_state
POS_THREAD = 6
POS_MODE = 7
POS_TRANSCRIPT = 9
POS_CHAT = 10


class FakeInterrupt:
    def __init__(self, value):
        self.value = value


class DummyGraph:
    def __init__(self, events):
        self._events = events
        self.calls = []

    def stream(self, payload, *args, **kwargs):
        self.calls.append(payload)
        yield from self._events


def _service(events):
    graph = DummyGraph(events)
    return SubmitService(AppContext(settings=Settings(), graph=graph)), graph


def test_clear_session_resets_core_state() -> None:
    service, _ = _service([])
    result = service.clear_session()
    assert result[POS_MODE] == MODE_IDEA
    assert result[POS_TRANSCRIPT] == []
    assert result[POS_CHAT] == []


def test_idea_mode_runs_to_answers_gate() -> None:
    events = [
        {
            "discussion": {
                "discussion_history": [AIMessage(content="Panel output", name="pm")],
                "turn_count": 1,
                "next_speaker": "Tech Lead",
            }
        },
        {"summarizer": {"summary": "Executive summary", "generated_questions": ["1. Who is the user?"]}},
        {"__interrupt__": (FakeInterrupt({"kind": "answers", "questions": ["1. Who is the user?"], "summary": "Executive summary"}),)},
    ]
    service, graph = _service(events)
    outputs = list(service.handle_submit("Build x", 1, PLAN_CHOICE_GENERATE, "thread-1", MODE_IDEA, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_ANSWERS
    assert any("Who is the user?" in m["content"] for m in final[POS_CHAT])
    assert isinstance(graph.calls[0], dict)
    assert graph.calls[0]["user_idea"] == "Build x"


def test_answers_mode_resumes_to_plan_gate() -> None:
    events = [
        {"architect": {"architecture": "## Option A\nSimple stack"}},
        {"planner_offer": {"plan_offer_question": "Generate the pack?"}},
        {"__interrupt__": (FakeInterrupt({"kind": "plan_gate", "question": "Generate the pack?", "architecture": "## Option A"}),)},
    ]
    service, graph = _service(events)
    outputs = list(
        service.handle_submit("1. Solo founders.", 1, PLAN_CHOICE_GENERATE, "thread-2", MODE_ANSWERS, [], [], [])
    )
    final = outputs[-1]
    assert final[POS_MODE] == MODE_PLAN_GATE
    assert isinstance(graph.calls[0], Command)
    assert graph.calls[0].resume == "1. Solo founders."
    assert any("Architect" in m["content"] for m in final[POS_CHAT])
    assert "Generate the pack?" in final[POS_CHAT][-1]["content"]


def test_plan_gate_generate_resumes_with_structured_decision() -> None:
    events = [
        {"plan_bundle": {"project_bundle_summary": "Generated pack in `dir`.", "project_bundle_dir": "dir"}},
    ]
    service, graph = _service(events)
    outputs = list(
        service.handle_submit("keep it lean", 1, PLAN_CHOICE_GENERATE, "thread-3", MODE_PLAN_GATE, [], [], [])
    )
    final = outputs[-1]
    assert final[POS_MODE] == MODE_DONE
    assert isinstance(graph.calls[0], Command)
    assert graph.calls[0].resume == {"generate": True, "notes": "keep it lean"}
    assert any("Generated pack" in m["content"] for m in final[POS_CHAT])


def test_plan_gate_skip_finishes_session() -> None:
    service, graph = _service([])
    outputs = list(service.handle_submit("", 1, PLAN_CHOICE_SKIP, "thread-4", MODE_PLAN_GATE, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_DONE
    assert graph.calls[0].resume == {"generate": False, "notes": ""}


def test_error_mid_run_reports_stage_and_keeps_mode() -> None:
    class ExplodingGraph:
        calls: list = []

        def stream(self, payload, *args, **kwargs):
            raise TimeoutError("model timed out")
            yield  # pragma: no cover

    service = SubmitService(AppContext(settings=Settings(), graph=ExplodingGraph()))
    outputs = list(service.handle_submit("Answers", 1, PLAN_CHOICE_GENERATE, "thread-5", MODE_ANSWERS, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_ANSWERS  # retryable
    assert any("error" in m["content"].lower() for m in final[POS_CHAT])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_submit_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'MODE_ANSWERS'`.

- [ ] **Step 3: Rewrite `submit_service.py`**

Full new content:

```python
from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gradio as gr
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

try:
    from .agents import display_speaker_name
    from .config import Settings
    from .exporter import save_session_markdown
    from .render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from .roles import SPEAKER_ORDER
    from .state import IdeaDiscussionState
    from .text_utils import normalize_message_content
except ImportError:
    from agents import display_speaker_name
    from config import Settings
    from exporter import save_session_markdown
    from render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from roles import SPEAKER_ORDER
    from state import IdeaDiscussionState
    from text_utils import normalize_message_content

LOGGER = logging.getLogger(__name__)

MODE_IDEA = "idea"
MODE_ANSWERS = "answers"
MODE_PLAN_GATE = "plan_gate"
MODE_DONE = "done"

PLAN_CHOICE_GENERATE = "Generate the execution pack"
PLAN_CHOICE_SKIP = "Skip for now"

_INPUT_LABELS: dict[str, tuple[str, str]] = {
    MODE_IDEA: ("Describe your idea", "Describe your product idea..."),
    MODE_ANSWERS: ("Your answers to MVP decision questions", "1. ...\n2. ..."),
    MODE_PLAN_GATE: ("Planning notes (optional)", "Anything the planner should account for..."),
    MODE_DONE: ("Describe your next idea", "Describe your product idea..."),
}
_BUTTON_LABELS: dict[str, str] = {
    MODE_IDEA: "Run discussion",
    MODE_ANSWERS: "Submit answers",
    MODE_PLAN_GATE: "Send decision",
    MODE_DONE: "Run discussion",
}


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    graph: CompiledStateGraph


def _initial_state(idea: str, rounds: int) -> IdeaDiscussionState:
    return {
        "user_idea": idea,
        "discussion_history": [HumanMessage(content=idea)],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "plan_offer_question": "",
        "plan_decision": {"generate": False, "notes": ""},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "stage": "discussion",
        "next_speaker": SPEAKER_ORDER[0],
        "max_rounds": rounds * len(SPEAKER_ORDER),
        "turn_count": 0,
    }


def _error_hint(stage: str, exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "authentication" in name or "permission" in name or "unauthorized" in name:
        return f"{stage} failed due to API authentication/permission. Check provider keys and model access."
    if "ratelimit" in name or "rate_limit" in name:
        return f"{stage} hit a rate limit. Retry shortly or lower request frequency."
    if "timeout" in name:
        return f"{stage} timed out. Retry with fewer rounds or try another model."
    return f"{stage} failed with `{type(exc).__name__}`: {exc}"


class SubmitService:
    """Bridges Gradio submits to one continuously checkpointed graph thread.

    The graph pauses at `interrupt()` gates; the UI mode mirrors the gate the
    graph is paused at and the next submit resumes it with `Command(resume=...)`.
    """

    def __init__(self, context: AppContext):
        self._context = context
        self._project_dir = Path(__file__).resolve().parent

    # ------------------------------------------------------------------ UI

    def _pack(
        self,
        *,
        status: str,
        mode: str,
        thread_id: str,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
        clear_input: bool = False,
    ) -> tuple[Any, ...]:
        label, placeholder = _INPUT_LABELS.get(mode, _INPUT_LABELS[MODE_IDEA])
        input_update = (
            gr.update(value="", label=label, placeholder=placeholder)
            if clear_input
            else gr.update(label=label, placeholder=placeholder)
        )
        return (
            gr.update(value=status),
            chat_state,
            input_update,
            gr.update(visible=mode in (MODE_IDEA, MODE_DONE)),
            gr.update(visible=mode == MODE_PLAN_GATE),
            gr.update(value=_BUTTON_LABELS.get(mode, _BUTTON_LABELS[MODE_IDEA])),
            thread_id,
            mode,
            turns_state,
            transcript_state,
            chat_state,
        )

    def clear_session(self) -> tuple[Any, ...]:
        return self._pack(
            status="",
            mode=MODE_IDEA,
            thread_id=str(uuid.uuid4()),
            turns_state=[],
            transcript_state=[],
            chat_state=[],
            clear_input=True,
        )

    # ------------------------------------------------------- chat helpers

    @staticmethod
    def _append_thinking(
        chat_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        speaker: str,
        message: str,
    ) -> None:
        chat_state.append({"role": "assistant", "content": thinking_block(speaker, message)})
        transcript_state.append({"kind": "thinking", "speaker": speaker, "content": message})

    @staticmethod
    def _replace_or_append(
        chat_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_html: str,
        kind: str,
        speaker: str,
        content: str,
    ) -> None:
        entry = {"kind": kind, "speaker": speaker, "content": content}
        message = {"role": "assistant", "content": chat_html}
        if transcript_state and (transcript_state[-1].get("kind") or "") == "thinking":
            transcript_state[-1] = entry
            chat_state[-1] = message
        else:
            transcript_state.append(entry)
            chat_state.append(message)

    # ------------------------------------------------------ event mapping

    def _apply_discussion(
        self,
        update: dict[str, Any],
        max_turns: int,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str | None:
        history = update.get("discussion_history") or []
        message = history[-1] if history else None
        if not isinstance(message, AIMessage):
            return None
        speaker = display_speaker_name(message.name) if message.name else "Panelist"
        content = normalize_message_content(message)
        self._replace_or_append(chat_state, transcript_state, turn_block(speaker, content), "discussion", speaker, content)
        turns_state.append({"speaker": speaker, "content": content})
        turn = int(update.get("turn_count", 0))
        if turn < max_turns:
            nxt = update.get("next_speaker", SPEAKER_ORDER[0])
            self._append_thinking(chat_state, transcript_state, nxt, f"{nxt} is drafting the next panel turn...")
            return f"Turn {turn}/{max_turns} complete. Next: {nxt}."
        self._append_thinking(
            chat_state, transcript_state, "Summarizer", "Synthesizing the discussion into a summary and MVP questions..."
        )
        return f"Turn {turn}/{max_turns} complete. Summarizer is preparing the brief."

    def _apply_summary(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        summary = (update.get("summary") or "").strip()
        self._replace_or_append(chat_state, transcript_state, summary_block(summary), "summary", "Summary", summary)
        return "Summary ready. Preparing MVP decision questions..."

    def _apply_architect(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        architecture = (update.get("architecture") or "").strip() or "Architect returned an empty architecture proposal."
        self._replace_or_append(chat_state, transcript_state, architect_block(architecture), "architect", "Architect", architecture)
        self._append_thinking(chat_state, transcript_state, "Planner", "Preparing the execution-pack question...")
        return "Architecture ready. Planner is preparing the next step..."

    def _apply_plan_bundle(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        summary = (update.get("project_bundle_summary") or "").strip()
        summary = summary or (update.get("project_bundle_dir") or "").strip()
        summary = summary or "Project pack generation returned no visible summary."
        self._replace_or_append(chat_state, transcript_state, turn_block("Planner", summary), "project_bundle", "Planner", summary)
        return "Project pack ready."

    def _apply_interrupt(
        self,
        interrupts: Any,
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> tuple[str, str]:
        value = interrupts[0].value if interrupts else None
        payload = value if isinstance(value, dict) else {}
        kind = payload.get("kind")
        if kind == "answers":
            questions = [str(q) for q in payload.get("questions") or []]
            block = questions_block(questions)
            if block:
                self._replace_or_append(
                    chat_state, transcript_state, block, "questions", "Questions", "\n".join(questions)
                )
            return MODE_ANSWERS, "Answer the MVP decision questions to continue."
        if kind == "plan_gate":
            question = str(payload.get("question") or "").strip() or "Generate the execution pack?"
            self._replace_or_append(
                chat_state, transcript_state, turn_block("Planner", question), "planner_offer", "Planner", question
            )
            return MODE_PLAN_GATE, "Choose whether to generate the execution pack. Notes are optional."
        return MODE_DONE, "Pipeline paused at an unknown gate. Use Clear to restart."

    # ------------------------------------------------------------ running

    def _run(
        self,
        *,
        payload: Any,
        max_turns: int,
        thread_id: str,
        mode: str,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        config = {"configurable": {"thread_id": thread_id}}
        next_mode = MODE_DONE
        final_status = "Pipeline finished. Use Clear to start a fresh session, or describe a new idea."
        try:
            for event in self._context.graph.stream(payload, config=config, stream_mode="updates"):
                if "__interrupt__" in event:
                    next_mode, final_status = self._apply_interrupt(event["__interrupt__"], transcript_state, chat_state)
                    continue
                status: str | None = None
                if "discussion" in event:
                    status = self._apply_discussion(event["discussion"], max_turns, turns_state, transcript_state, chat_state)
                elif "summarizer" in event:
                    status = self._apply_summary(event["summarizer"], transcript_state, chat_state)
                elif "architect" in event:
                    status = self._apply_architect(event["architect"], transcript_state, chat_state)
                elif "plan_bundle" in event:
                    status = self._apply_plan_bundle(event["plan_bundle"], transcript_state, chat_state)
                # collect_answers / plan_gate / planner_offer updates need no chat output:
                # their visible content arrives via the interrupt payloads.
                if status:
                    yield self._pack(
                        status=status,
                        mode=mode,
                        thread_id=thread_id,
                        turns_state=turns_state,
                        transcript_state=transcript_state,
                        chat_state=chat_state,
                    )
        except Exception as exc:
            LOGGER.exception("Pipeline run failed")
            hint = _error_hint("Pipeline", exc)
            chat_state.append({"role": "assistant", "content": f"### Orchestrator error\n\n{hint}"})
            transcript_state.append({"kind": "system", "speaker": "Orchestrator error", "content": hint})
            next_mode = mode if mode in (MODE_ANSWERS, MODE_PLAN_GATE) else MODE_IDEA
            final_status = "Run failed. See the error in chat; you can retry your last input."
        yield self._pack(
            status=final_status,
            mode=next_mode,
            thread_id=thread_id,
            turns_state=turns_state,
            transcript_state=transcript_state,
            chat_state=chat_state,
        )

    # ------------------------------------------------------------- submit

    def handle_submit(
        self,
        user_text: str,
        rounds: int,
        plan_choice: str,
        thread_id: str,
        mode: str,
        turns_state: list[dict[str, str]] | None = None,
        transcript_state: list[dict[str, str]] | None = None,
        chat_state: list[dict[str, str]] | None = None,
    ) -> Generator[tuple[Any, ...], None, None]:
        text = (user_text or "").strip()
        thread_id = (thread_id or "").strip() or str(uuid.uuid4())
        mode = (mode or MODE_IDEA).strip() or MODE_IDEA
        turns_state = list(turns_state or [])
        transcript_state = list(transcript_state or [])
        chat_state = list(chat_state or [])

        if mode in (MODE_IDEA, MODE_DONE):
            if not text:
                yield self._pack(
                    status="Please describe your idea first.",
                    mode=MODE_IDEA,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                )
                return
            if mode == MODE_DONE:
                thread_id = str(uuid.uuid4())
            turns_state, transcript_state, chat_state = [], [], []
            state = _initial_state(text, int(rounds))
            chat_state.append({"role": "user", "content": text})
            transcript_state.append({"kind": "idea", "speaker": "You", "content": text})
            self._append_thinking(
                chat_state, transcript_state, state["next_speaker"],
                f"{state['next_speaker']} is drafting the next panel turn...",
            )
            yield self._pack(
                status="Running panel discussion...",
                mode=MODE_IDEA,
                thread_id=thread_id,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
                clear_input=True,
            )
            yield from self._run(
                payload=state,
                max_turns=state["max_rounds"],
                thread_id=thread_id,
                mode=MODE_IDEA,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        if mode == MODE_ANSWERS:
            if not text:
                yield self._pack(
                    status="Please answer the questions before submitting.",
                    mode=mode,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                )
                return
            chat_state.append({"role": "user", "content": text})
            transcript_state.append({"kind": "user_answers", "speaker": "You", "content": text})
            self._append_thinking(
                chat_state, transcript_state, "Architect", "Turning your answers into two architecture options..."
            )
            yield self._pack(
                status="Answers captured. Architect is designing system options...",
                mode=mode,
                thread_id=thread_id,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
                clear_input=True,
            )
            yield from self._run(
                payload=Command(resume=text),
                max_turns=0,
                thread_id=thread_id,
                mode=mode,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        if mode == MODE_PLAN_GATE:
            generate = (plan_choice or "").strip() == PLAN_CHOICE_GENERATE
            decision_label = PLAN_CHOICE_GENERATE if generate else PLAN_CHOICE_SKIP
            shown = decision_label + (f" — {text}" if text else "")
            chat_state.append({"role": "user", "content": shown})
            transcript_state.append({"kind": "planner_decision", "speaker": "You", "content": shown})
            if generate:
                self._append_thinking(
                    chat_state, transcript_state, "Planner",
                    "Generating `AGENTS.md` guidance files and a task-only `plan.md`...",
                )
                yield self._pack(
                    status="Generating the agent-ready project pack...",
                    mode=mode,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                    clear_input=True,
                )
            yield from self._run(
                payload=Command(resume={"generate": generate, "notes": text}),
                max_turns=0,
                thread_id=thread_id,
                mode=mode,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        yield self._pack(
            status=f"Unknown mode `{mode}`. Use Clear to reset the session.",
            mode=MODE_IDEA,
            thread_id=thread_id,
            turns_state=turns_state,
            transcript_state=transcript_state,
            chat_state=chat_state,
        )

    # -------------------------------------------------------------- export

    def save_conversation(
        self,
        user_text: str,
        thread_id: str,
        mode: str,
        transcript_state: list[dict[str, str]],
    ) -> tuple[Any, Any]:
        draft_input = (user_text or "").strip()
        transcript_state = transcript_state or []
        if not transcript_state and not draft_input:
            return gr.update(value="Nothing to save yet."), gr.update(value=None, visible=False)

        try:
            export_path = save_session_markdown(
                project_dir=self._project_dir,
                transcript_state=transcript_state,
                thread_id=thread_id,
                mode=mode,
                draft_input=draft_input,
            )
        except Exception as exc:
            LOGGER.exception("Conversation export failed")
            return (
                gr.update(value=_error_hint("Conversation export", exc)),
                gr.update(value=None, visible=False),
            )

        return (
            gr.update(value=f"Saved conversation to `{export_path}`."),
            gr.update(value=str(export_path), visible=True),
        )
```

- [ ] **Step 4: Update `app.py`**

Replace the component/state/wiring section of `make_ui` (everything from `input_tb = ...` through the `clear_btn.click(...)` calls) with:

```python
        input_tb = gr.Textbox(label="Describe your idea", value=DEFAULT_IDEA, lines=5)
        rounds_sl = gr.Slider(
            minimum=1,
            maximum=6,
            value=resolved_settings.default_rounds,
            step=1,
            label="Rounds per speaker",
        )
        plan_choice = gr.Radio(
            choices=[PLAN_CHOICE_GENERATE, PLAN_CHOICE_SKIP],
            value=PLAN_CHOICE_GENERATE,
            label="Execution pack decision",
            visible=False,
        )
        with gr.Row():
            run_btn = gr.Button("Run discussion", variant="primary")
            save_btn = gr.Button("Save to Markdown")
            clear_btn = gr.Button("Clear")
        export_file = gr.File(label="Saved Markdown file", visible=False)

        thread_id_state = gr.State(str(uuid.uuid4()))
        mode_state = gr.State("idea")
        turns_state = gr.State([])
        transcript_state = gr.State([])
        chat_state = gr.State([])

        submit_outputs = [
            status_md,
            chatbot,
            input_tb,
            rounds_sl,
            plan_choice,
            run_btn,
            thread_id_state,
            mode_state,
            turns_state,
            transcript_state,
            chat_state,
        ]
        run_btn.click(
            service.handle_submit,
            inputs=[input_tb, rounds_sl, plan_choice, thread_id_state, mode_state, turns_state, transcript_state, chat_state],
            outputs=submit_outputs,
        )
        save_btn.click(
            service.save_conversation,
            inputs=[input_tb, thread_id_state, mode_state, transcript_state],
            outputs=[status_md, export_file],
        )
        clear_btn.click(service.clear_session, outputs=submit_outputs)
        clear_btn.click(
            lambda: gr.update(value=None, visible=False),
            outputs=[export_file],
        )
```

And extend the `submit_service` import line at the top of `app.py`:

```python
    from .submit_service import AppContext, PLAN_CHOICE_GENERATE, PLAN_CHOICE_SKIP, SubmitService
```
(with the same names in the `except ImportError` branch, without the leading dot).

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check .`
Expected: ALL tests pass now (roles, routing/lifecycle, submit service, smoke, text utils), lint clean.

- [ ] **Step 6: Commit (local only — do not push)**

```bash
git add submit_service.py app.py tests/test_submit_service.py
git commit -m "feat: resume-driven submit service mirroring graph interrupts"
```

---

### Task 4: Docs + final verification

**Files:**
- Modify: `README.md` (Workflow + Project Layout sections)
- Modify: `CLAUDE.md` (architecture sections)
- Modify: `CHANGELOG.md` (new entry)

- [ ] **Step 1: Update `README.md`**

In **Workflow**, replace the numbered list with:

```markdown
1. Enter an idea and run the panel discussion (PM, Tech Lead, Skeptic debate over the rounds you choose).
2. The pipeline pauses at the first gate: review the summary and answer the five MVP decision questions.
3. The architect produces two architecture options anchored to your answers.
4. The pipeline pauses at the second gate: choose whether to generate the execution pack (`AGENTS.md` files and `plan.md`), optionally adding planning notes.
5. Save the session to `exports/` as Markdown at any time.

The whole session runs as a single LangGraph thread; the gates are real `interrupt()` pauses that resume exactly where the graph stopped.
```

In **Project Layout**, add after the `agents.py` line:

```markdown
- `roles.py`: declarative role registry (providers, models, token budgets, system prompts)
```

- [ ] **Step 2: Update `CLAUDE.md`**

- In **Pipeline Phases**, replace items 3–5 with:

```markdown
3. **Answers gate** — the graph pauses at `collect_answers` (`interrupt()`); user answers resume it.
4. **Architect** — generates two implementation options anchored to the user's answers.
5. **Plan gate + Planner** — the graph pauses at `plan_gate`; a structured `{generate, notes}` decision resumes it and optionally creates the `project_blueprints/` pack.
```

- In **Key Files**, add a row: `| roles.py | RoleSpec registry: all system prompts + provider/model/token wiring per agent |`
- Replace the **State Machine** section diagram with:

```markdown
### State Machine (graph.py)

```
START → discussion (loop until turn_count ≥ max_rounds)
      → summarizer
      → collect_answers   [interrupt: questions out, answers in]
      → architect
      → planner_offer
      → plan_gate         [interrupt: offer out, {generate, notes} in]
      → plan_bundle | END
```

The graph runs on a single checkpointer thread per session; the UI resumes interrupts with `Command(resume=...)`. There is no `phase` field — UI mode mirrors the interrupt the graph is paused at.
```

- In **Multi-Provider LLM Abstraction**, replace the `get_discussion_runtimes()` sentence with: "`get_runtime(role_key)` returns an LRU-cached runtime for any role in `roles.ROLES` (PM=OpenAI, Tech Lead=Anthropic, Skeptic=Google by default)."

- [ ] **Step 3: Add `CHANGELOG.md` entry**

Add at the top (keep existing format of the file):

```markdown
## Unreleased

### Changed
- Pipeline now runs as one continuous LangGraph thread with real `interrupt()` gates for answers and the plan decision (no more phase-flag routing or per-phase threads).
- All role prompts and provider/model wiring moved to a declarative registry in `roles.py`.
- Discussion prompts are round-aware: panelists know their round and must converge in the final round; the Skeptic must pair every objection with the cheapest resolving test.
- The summarizer now reports panel disagreements and resolutions; architect options anchor to the user's stated constraints.
- The plan gate is a structured choice (radio + optional notes) instead of free-text yes/no parsing.
```

- [ ] **Step 4: Full verification**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check .`
Expected: all PASS, lint clean.

Run: `.venv/bin/python -c "from app import make_ui; from config import Settings; make_ui(settings=Settings()); print('UI OK')"`
Expected: `UI OK`

- [ ] **Step 5: Commit (local only — do not push)**

```bash
git add README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: document interrupt-based pipeline and role registry"
```

---

## Self-review checklist (done at plan-writing time)

- **Spec coverage:** graph+interrupts (Task 2), role registry (Task 1), prompt overhaul (Task 1 + round-awareness in Task 2), SubmitService simplification (Task 3), state extension (Task 2), tests (Tasks 1–3), docs note (Task 4). `phase`/`route_from_start`/`_is_affirmative`/second thread all removed (Tasks 2–3).
- **Known intermediate state:** after Task 2, `tests/test_submit_service.py` fails by design until Task 3 — flagged in Task 2 Step 6.
- **Type consistency:** `PlanDecision = {generate: bool, notes: str}` used identically in `state.py`, `plan_gate_node`, tests, and `SubmitService`; interrupt payload kinds are `"answers"`/`"plan_gate"` everywhere; tuple positions in `_pack` match `submit_outputs` order in `app.py` and the `POS_*` constants in tests.
