"""The moderated panel subgraph: parallel openings, moderator-driven turns, early stop, round-robin."""

import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver

from idea_to_mvp import llm
from idea_to_mvp.demo.fixtures import respond_structured
from idea_to_mvp.graph import build_graph, merged_values, run_config
from idea_to_mvp.nodes.panel import build_panel_subgraph, route_after_moderator, route_after_turn
from idea_to_mvp.roles import SPEAKER_ORDER, TOKEN_TO_SPEAKER
from idea_to_mvp.schemas import ModeratorDecision
from idea_to_mvp.state import make_initial_state

PANELISTS = {"pm", "tech_lead", "skeptic"}


class FakeRuntime:
    def __init__(self, role: str) -> None:
        self.role = role
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = f"system::{role}"


class Recorder:
    """Fake LLM layer: records what ran, in which order, and how many calls overlapped."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.delays: dict[str, float] = {}
        self.started: list[str] = []
        self.finished: list[str] = []
        self.prompts: list[str] = []
        self.active = 0
        self.max_active = 0
        self.moderator_calls = 0
        self.fail_on_speaker_call: int | None = None
        # None = simulate a model that cannot produce a valid decision (the fallback is used)
        self.decide: Callable[[list[BaseMessage]], ModeratorDecision] | None = lambda _m: ModeratorDecision(
            converged=True, next_speaker=None, reason="aligned"
        )

    @property
    def speaker_calls(self) -> int:
        return len([role for role in self.started if role in PANELISTS])

    def invoke_text(self, runtime: FakeRuntime, messages: list[BaseMessage], *, max_tokens: int | None = None) -> str:
        role = runtime.role
        with self.lock:
            self.started.append(role)
            self.prompts.append("\n".join(str(m.content) for m in messages))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            call_number = self.speaker_calls
        try:
            time.sleep(self.delays.get(role, 0.0))
            if self.fail_on_speaker_call == call_number and role in PANELISTS:
                raise RuntimeError("model exploded")  # not transient: LLM_RETRY must not retry it
            return f"{role} says hi"
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append(role)

    def invoke_structured(self, runtime: Any, messages: list[BaseMessage], schema: Any, *, fallback: Any = None) -> Any:
        if schema is ModeratorDecision:
            with self.lock:
                self.moderator_calls += 1
            if self.decide is None:
                assert fallback is not None, "the moderator call must provide a deterministic fallback"
                return fallback()
            return self.decide(messages)
        return respond_structured(schema, messages)


@pytest.fixture()
def rec(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(llm, "get_runtime", FakeRuntime)
    monkeypatch.setattr(llm, "invoke_text", recorder.invoke_text)
    monkeypatch.setattr(llm, "invoke_structured", recorder.invoke_structured)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    return recorder


def run_panel(rounds: int, mode: str = "moderated", config: Any = None) -> dict[str, Any]:
    state = make_initial_state("idea", rounds, panel_mode=mode)  # type: ignore[arg-type]
    return build_panel_subgraph().invoke(state, config=config)


def speakers(result: dict[str, Any]) -> list[str]:
    return [TOKEN_TO_SPEAKER[str(m.name)] for m in result["discussion_history"] if isinstance(m, AIMessage)]


# ------------------------------------------------------------------ openings


def test_openings_run_in_parallel_and_land_in_canonical_order(rec: Recorder) -> None:
    rec.delays = {"pm": 0.30, "tech_lead": 0.15, "skeptic": 0.0}
    result = run_panel(rounds=1)
    assert rec.finished == ["skeptic", "tech_lead", "pm"]  # they overlapped: completion order is reversed
    assert speakers(result) == SPEAKER_ORDER  # ...yet the history is PM, Tech Lead, Skeptic
    assert result["turn_count"] == 3
    assert rec.moderator_calls == 0  # one round is the openings only


def test_openings_are_written_without_seeing_each_other(rec: Recorder) -> None:
    run_panel(rounds=1)
    assert len(rec.prompts) == 3
    assert all("says hi" not in prompt for prompt in rec.prompts)


# ----------------------------------------------------------------- moderator


def test_moderator_cannot_end_the_debate_before_everyone_spoke_twice(rec: Recorder) -> None:
    result = run_panel(rounds=3)  # turn cap: 9
    assert result["turn_count"] == 6
    assert Counter(speakers(result)) == {name: 2 for name in SPEAKER_ORDER}
    assert result["convergence"] == {"converged": True, "reason": "aligned"}
    assert rec.moderator_calls == 4  # after turns 3, 4 and 5 it was overruled; after turn 6 it stopped the panel


def test_moderator_picks_the_next_speaker(rec: Recorder) -> None:
    picks = iter(["Tech Lead", "PM", "Skeptic"])
    rec.decide = lambda _m: ModeratorDecision(converged=False, next_speaker=next(picks), reason="go on")  # type: ignore[arg-type]
    result = run_panel(rounds=2)
    assert speakers(result) == ["PM", "Tech Lead", "Skeptic", "Tech Lead", "PM", "Skeptic"]


def test_the_same_speaker_never_speaks_twice_in_a_row(rec: Recorder) -> None:
    rec.decide = lambda _m: ModeratorDecision(converged=False, next_speaker="Skeptic", reason="again")
    result = run_panel(rounds=3)
    names = speakers(result)
    assert len(names) == 9
    assert all(a != b for a, b in zip(names, names[1:], strict=False)), names


def test_the_turn_cap_ends_a_panel_that_never_converges(rec: Recorder) -> None:
    rec.decide = lambda _m: ModeratorDecision(converged=False, next_speaker=None, reason="keep going")
    result = run_panel(rounds=2)
    assert result["turn_count"] == len(speakers(result)) == 6
    assert rec.moderator_calls == 3  # after turns 3, 4 and 5; the capped turn 6 needs no decision
    assert result["convergence"]["converged"] is False


def test_an_unusable_moderator_falls_back_to_a_plain_rotation(rec: Recorder) -> None:
    rec.decide = None
    result = run_panel(rounds=2)
    names = speakers(result)
    assert len(names) == 6 and all(a != b for a, b in zip(names, names[1:], strict=False))
    assert result["convergence"]["converged"] is False


# --------------------------------------------------------------- round robin


def test_round_robin_reproduces_the_sequential_turn_order(rec: Recorder) -> None:
    result = run_panel(rounds=2, mode="round_robin")
    assert rec.started == ["pm", "tech_lead", "skeptic", "pm", "tech_lead", "skeptic"]
    assert speakers(result) == SPEAKER_ORDER * 2
    assert result["turn_count"] == 6
    assert rec.moderator_calls == 0
    assert rec.max_active == 1
    assert "pm says hi" in rec.prompts[1]  # each turn sees the transcript so far


# ------------------------------------------------------------------- routers


def _state(mode: str, turns: int, cap: int = 6, converged: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = dict(make_initial_state("idea", 2, panel_mode=mode))  # type: ignore[arg-type]
    state.update(turn_count=turns, max_rounds=cap, convergence={"converged": converged, "reason": "r"})
    return state


def test_route_after_turn_loops_until_the_cap_in_round_robin() -> None:
    assert route_after_turn(_state("round_robin", 2, cap=3)) == "speaker_turn"
    assert route_after_turn(_state("round_robin", 3, cap=3)) == "__end__"


def test_route_after_turn_asks_the_moderator_until_the_cap_when_moderated() -> None:
    assert route_after_turn(_state("moderated", 3)) == "moderator"
    assert route_after_turn(_state("moderated", 6)) == "__end__"


def test_route_after_moderator() -> None:
    assert route_after_moderator(_state("moderated", 4)) == "speaker_turn"
    assert route_after_moderator(_state("moderated", 4, converged=True)) == "__end__"


# ------------------------------------------------------ the whole pipeline


def test_the_panel_is_one_node_whose_llm_nodes_carry_the_retry_policy() -> None:
    parent = build_graph().builder.nodes
    assert "panel" in parent and "discussion" not in parent
    assert not parent["panel"].retry_policy  # a retry would redo every turn; the inner nodes retry instead
    inner = build_panel_subgraph().builder.nodes
    for name in ("opening_turn", "speaker_turn", "moderator"):
        assert inner[name].retry_policy, name


@pytest.mark.parametrize(("limit", "expected_peak"), [(1, 1), (4, 3)])
def test_run_config_caps_parallel_model_calls(rec: Recorder, limit: int, expected_peak: int) -> None:
    rec.delays = {role: 0.05 for role in PANELISTS}
    run_panel(rounds=1, config=run_config("cap", max_concurrency=limit))
    assert rec.max_active == expected_peak


def test_a_crash_mid_panel_resumes_without_redoing_the_openings(rec: Recorder) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("crash")
    state = make_initial_state("idea", 3)

    rec.fail_on_speaker_call = 4  # the first turn after the openings
    with pytest.raises(RuntimeError, match="exploded"):
        graph.invoke(state, config)
    snapshot = graph.get_state(config, subgraphs=True)
    assert snapshot.next == ("panel",)
    assert merged_values(snapshot)["turn_count"] == 3  # the openings survived in the subgraph checkpoint

    rec.fail_on_speaker_call = None
    graph.invoke(None, config)
    assert rec.speaker_calls == 3 + 1 + 3  # openings, the failed turn, then turns 4-6 (never the openings again)
    values = graph.get_state(config).values
    assert values["turn_count"] == 6 and values["convergence"]["converged"] is True
    assert len([m for m in values["discussion_history"] if isinstance(m, AIMessage)]) == 6


def test_panel_usage_is_recorded_once_in_the_parent_state(demo_env) -> None:
    sentinel = {"role": "earlier", "provider": "", "model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
    state = make_initial_state("A climbing app", 1)
    state["usage"] = [sentinel]
    graph = build_graph(MemorySaver())
    config = run_config("usage")
    graph.invoke(state, config)
    usage = graph.get_state(config).values["usage"]
    assert usage.count(sentinel) == 1  # a shared reducer channel would have doubled what was already there
    assert sorted(r["role"] for r in usage if r["role"] in PANELISTS) == sorted(PANELISTS)


async def test_demo_panel_stops_early_and_its_progress_is_visible_while_it_runs(demo_env) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("progress")
    seen_turns: list[int] = []
    usage_sizes: list[int] = []
    async for _event in graph.astream(
        make_initial_state("A climbing app", 3), config, stream_mode="updates", subgraphs=True
    ):
        values = merged_values(await graph.aget_state(config, subgraphs=True))
        seen_turns.append(int(values["turn_count"]))
        usage_sizes.append(len(values["usage"]))
    assert seen_turns == sorted(seen_turns)
    assert {0, 3} <= set(seen_turns)  # seen before the openings merged and right after them, not only at the end
    assert usage_sizes == sorted(usage_sizes)
    final = (await graph.aget_state(config)).values
    assert final["turn_count"] == 6 < final["max_rounds"] == 9  # the demo moderator converges early
    assert final["convergence"]["converged"] is True and final["convergence"]["reason"]
