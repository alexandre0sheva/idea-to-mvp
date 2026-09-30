"""Verification in the real graph: parallel lanes, a checkpointed verify -> fix -> verify loop, budgets."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, Send

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation import verify as lanes
from idea_to_mvp.implementation.verify import LANES, FixOutcome, LaneOutcome, LaneReport
from idea_to_mvp.nodes.report import delivery_report_node
from idea_to_mvp.nodes.verify import fix_node, route_after_verdict, route_after_verify, verify_node
from idea_to_mvp.state import make_initial_state

IDEA = "A habit tracker for climbing gyms"


def lane_report(lane: str, passed: bool = True, failures: list[str] | None = None) -> LaneReport:
    return LaneReport(
        lane=lane,  # type: ignore[arg-type]
        passed=passed,
        commands_run=[{"command": f"check {lane}", "exit_code": 0 if passed else 1}],
        failures=failures if failures is not None else ([] if passed else [f"{lane} check failed"]),
        summary=f"{lane} lane summary",
    )


class FakeLanes:
    """Replaces `run_lane` / `run_fix`: outcomes come from a script, calls and concurrency are recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.verify_rounds: list[dict[str, bool]] = []  # per verification: lane -> passed (default: all pass)
        self.lane_calls: list[tuple[str, float]] = []
        self.fix_calls: list[tuple[list[str], float]] = []
        self.active = 0
        self.max_active = 0
        self.rounds_started = 0
        self.cost = 0.5
        monkeypatch.setattr(lanes, "run_lane", self.run_lane)
        monkeypatch.setattr(lanes, "run_fix", self.run_fix)

    async def run_lane(self, workspace: Path, lane: str, settings: Any, *, budget_usd: float, emit: Any, **_: Any) -> LaneOutcome:
        self.lane_calls.append((lane, budget_usd))
        round_index = (len(self.lane_calls) - 1) // len(LANES)
        script = self.verify_rounds[round_index] if round_index < len(self.verify_rounds) else {}
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return LaneOutcome(lane_report(lane, script.get(lane, True)), cost_usd=self.cost, turns=3)

    async def run_fix(self, workspace: Path, failures: list[str], settings: Any, *, budget_usd: float, emit: Any, **_: Any) -> FixOutcome:
        self.fix_calls.append((failures, budget_usd))
        return FixOutcome(True, "Fixed it.", cost_usd=self.cost, turns=2)


@pytest.fixture(autouse=True)
def env(demo_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    monkeypatch.setenv("IMPLEMENTER_MAX_TOTAL_USD", "25")
    monkeypatch.setenv("IMPLEMENTER_MAX_TASK_USD", "5")
    monkeypatch.setenv("MAX_FIX_ATTEMPTS", "2")
    clear_settings_cache()


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLanes:
    return FakeLanes(monkeypatch)


async def run_to_the_end(thread: str) -> tuple[Any, Any, list[tuple[str, dict]]]:
    """Drive the demo pipeline through every gate; returns (graph, config, node updates of the last step)."""
    graph = build_graph(MemorySaver())
    config = run_config(thread)
    await graph.ainvoke(make_initial_state(IDEA, 1), config)
    await graph.ainvoke(Command(resume="1. Solo climbers."), config)
    await graph.ainvoke(Command(resume={"option": "A", "notes": ""}), config)
    await graph.ainvoke(Command(resume={"generate": True, "notes": ""}), config)
    updates: list[tuple[str, dict]] = []
    async for chunk in graph.astream(Command(resume={"implement": True, "notes": ""}), config, stream_mode="updates"):
        updates += [(node, data) for node, data in chunk.items()]
    return graph, config, updates


# ----------------------------------------------------------------- the graph


async def test_a_clean_verification_runs_three_lanes_in_parallel_and_needs_no_fix(fake: FakeLanes) -> None:
    graph, config, updates = await run_to_the_end("verify-clean")
    values = (await graph.aget_state(config)).values
    assert sorted(lane for lane, _ in fake.lane_calls) == sorted(LANES)
    assert fake.max_active == 3
    assert not fake.fix_calls and "fix" not in [node for node, _ in updates]
    verification = values["verification"]
    assert verification["passed"] is True and verification["attempts"] == 0
    assert [lane["lane"] for lane in verification["lanes"]] == list(LANES)
    assert values["stage"] == "done" and "Tests lane: PASSED" in verification["report"]


async def test_every_fix_attempt_is_its_own_checkpointed_graph_step_until_the_attempts_run_out(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {"tests": False}]
    graph, config, updates = await run_to_the_end("verify-loop")
    steps = [node for node, _ in updates if node in ("verify", "verdict", "fix", "delivery_report")]
    assert steps == ["verify", "verdict", "fix", "verify", "verdict", "fix", "verify", "verdict", "delivery_report"]
    assert [node for node, _ in updates].count("verify_lane") == 9
    verification = (await graph.aget_state(config)).values["verification"]
    assert verification["passed"] is False and verification["attempts"] == 2
    checkpoints = [s async for s in graph.aget_state_history(config)]
    assert sum(1 for s in checkpoints if s.next == ("fix",)) == 2  # each attempt has its own checkpoint


async def test_a_fix_that_works_ends_the_loop_early(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"quality": False}, {}]
    graph, config, updates = await run_to_the_end("verify-fixed")
    values = (await graph.aget_state(config)).values
    assert len(fake.fix_calls) == 1 and len(fake.lane_calls) == 6
    assert values["verification"]["passed"] is True and values["verification"]["attempts"] == 1


async def test_the_fix_session_receives_the_merged_failure_list(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False, "requirements": False}, {}]
    await run_to_the_end("verify-failures")
    failures, _ = fake.fix_calls[0]
    assert failures == ["[tests] tests check failed", "[requirements] requirements check failed"]


async def test_lane_and_fix_spend_lands_in_the_usage_records(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {}]
    graph, config, _ = await run_to_the_end("verify-usage")
    usage = (await graph.aget_state(config)).values["usage"]
    verifier = [record for record in usage if record["role"] == "verifier"]
    assert len(verifier) == 6 and sum(record["cost_usd"] for record in verifier) == pytest.approx(3.0)
    assert any(record["role"] == "fixer" and record["cost_usd"] == 0.5 for record in usage)


# ------------------------------------------------------- budget (node level)


def state_with_spend(tmp_path: Path, spent: float, **fields: Any) -> dict[str, Any]:
    state: dict[str, Any] = dict(make_initial_state("idea", 1))
    state["workspace_dir"] = str(tmp_path)
    state["usage"] = [
        {"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": spent}
    ]
    state.update(fields)
    return state


def test_each_lane_gets_the_task_cap_or_its_share_of_what_is_left(tmp_path: Path) -> None:
    sends = route_after_verify(state_with_spend(tmp_path, 1.0))  # type: ignore[arg-type]
    assert isinstance(sends, list) and all(isinstance(send, Send) for send in sends)
    assert sorted(send.arg["lane"] for send in sends) == sorted(LANES)
    assert {send.arg["budget_usd"] for send in sends} == {5.0}
    sends = route_after_verify(state_with_spend(tmp_path, 22.0))  # type: ignore[arg-type]
    assert {send.arg["budget_usd"] for send in sends} == {1.0}  # (25 - 22) / 3 lanes


def test_verification_is_not_started_when_the_budget_is_used_up(tmp_path: Path) -> None:
    state = state_with_spend(tmp_path, 25.0)
    assert route_after_verify(state) == "delivery_report"  # type: ignore[arg-type]
    update = verify_node(state)  # type: ignore[arg-type]
    assert update["verification"]["passed"] is False and update["verification"]["attempts"] == 0
    assert "budget" in update["verification"]["report"].lower() and update["stage"] == "report"


def test_the_fix_session_gets_the_smaller_of_the_task_cap_and_the_remaining_budget(
    tmp_path: Path, fake: FakeLanes
) -> None:
    verification = {"passed": False, "attempts": 0, "report": "r", "lanes": [lane_report("tests", False).model_dump()]}
    asyncio.run(fix_node(state_with_spend(tmp_path, 22.0, verification=verification)))  # type: ignore[arg-type]
    asyncio.run(fix_node(state_with_spend(tmp_path, 1.0, verification=verification)))  # type: ignore[arg-type]
    assert [budget for _, budget in fake.fix_calls] == [3.0, 5.0]


def test_the_verdict_routes_to_fix_only_while_attempts_and_budget_remain(tmp_path: Path) -> None:
    def route(passed: bool, attempts: int, spent: float = 1.0) -> str:
        verification = {"passed": passed, "attempts": attempts, "report": "r", "lanes": []}
        return route_after_verdict(state_with_spend(tmp_path, spent, verification=verification))  # type: ignore[arg-type]

    assert route(True, 0) == "pass"
    assert route(False, 0) == "retry" and route(False, 1) == "retry"
    assert route(False, 2) == "exhausted"  # MAX_FIX_ATTEMPTS=2
    assert route(False, 0, spent=25.0) == "exhausted"  # no budget left for a fix


# ------------------------------------------------------------ delivery report


def test_the_delivery_report_shows_a_verdict_per_lane() -> None:
    lanes_result = lanes.combine_lanes(
        [lane_report("tests"), lane_report("quality", False, ["ruff: 2 errors"]), lane_report("requirements", False, ["R4 (P0): no test"])]
    )
    state = dict(make_initial_state("idea", 1))
    state["verification"] = {**lanes_result, "attempts": 2}
    report = delivery_report_node(state)["delivery_report"]  # type: ignore[arg-type]
    assert "tests ✅" in report and "quality ❌" in report and "requirements ❌" in report
    assert "R4 (P0): no test" in report and "2 fix attempt(s)" in report
