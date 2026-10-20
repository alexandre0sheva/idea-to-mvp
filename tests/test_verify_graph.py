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
from idea_to_mvp.implementation.fixes import Note
from idea_to_mvp.implementation.integrity import SessionWatch
from idea_to_mvp.implementation.verify import LANES, FixOutcome, LaneOutcome, LaneReport
from idea_to_mvp.nodes.report import delivery_report_node
from idea_to_mvp.nodes.verify import (
    fix_node,
    route_after_verdict,
    route_after_verify,
    verdict_node,
    verify_node,
)
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
        self.fix_attempts: list[int] = []
        self.active = 0
        self.max_active = 0
        self.rounds_started = 0
        self.cost = 0.5
        self.fix_effects: list[Any] = []  # per fix attempt: what the session does to the workspace
        self.lane_failures: dict[str, list[str]] = {}  # lane -> failures it reports when it fails (default: one generic line)
        self.fix_outcomes: list[FixOutcome] = []  # per fix attempt: the outcome to return (default: fixed, with notes)
        self.folders_seen: list[list[str]] = []  # per fix attempt: the attempt folders that existed when it started
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
        passed = script.get(lane, True)
        return LaneOutcome(lane_report(lane, passed, None if passed else self.lane_failures.get(lane)), cost_usd=self.cost, turns=3)

    async def run_fix(self, workspace: Path, failures: list[str], settings: Any, *, budget_usd: float, emit: Any, attempt: int = 1, **_: Any) -> FixOutcome:
        self.fix_calls.append((failures, budget_usd))
        self.fix_attempts.append(attempt)
        root = Path(workspace) / ".idea-to-mvp" / "fix"
        self.folders_seen.append(sorted(p.name for p in root.iterdir()) if root.is_dir() else [])
        watch = SessionWatch(Path(workspace), settings, session=f"fix-{attempt}", iteration=1)  # as the real run_fix does
        if attempt <= len(self.fix_effects) and self.fix_effects[attempt - 1] is not None:
            self.fix_effects[attempt - 1](Path(workspace))
        if attempt <= len(self.fix_outcomes):
            outcome = self.fix_outcomes[attempt - 1]
            outcome.integrity = watch.findings()
            return outcome
        notes = [Note("new", f"hypothesis {attempt}", "report line")]
        if attempt > 1:
            notes.insert(0, Note("dropped", f"hypothesis {attempt - 1}", "the second run"))
        return FixOutcome(True, "Fixed it.", cost_usd=self.cost, turns=2, integrity=watch.findings(), notes=notes)


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


async def test_each_fix_session_is_told_its_attempt_so_later_ones_escalate(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {"tests": False}]
    await run_to_the_end("verify-attempts")
    assert fake.fix_attempts == [1, 2]


async def test_usage_names_the_model_each_session_kind_ran_on(fake: FakeLanes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_MODEL", "m-task")
    monkeypatch.setenv("VERIFIER_MODEL", "m-verify")
    monkeypatch.setenv("ESCALATION_MODEL", "m-hard")
    clear_settings_cache()
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {"tests": False}]
    graph, config, _ = await run_to_the_end("verify-models")
    usage = (await graph.aget_state(config)).values["usage"]
    assert {r["model"] for r in usage if r["role"] == "verifier"} == {"m-verify"}
    assert [r["model"] for r in usage if r["role"] == "fixer"] == ["m-task", "m-hard"]


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


# ------------------------------------------------------------ test integrity


def skip_a_test(workspace: Path) -> None:
    path = workspace / "tests" / "test_core.py"
    text = path.read_text()
    path.write_text(text.replace("    def test_describe_mentions_the_idea", "    @unittest.skip('flaky')\n    def test_describe_mentions_the_idea"))


def justify_the_skip(workspace: Path) -> None:
    (workspace / "quality").mkdir(exist_ok=True)
    (workspace / "quality" / "TEST_CHANGES.md").write_text("- tests/test_core.py: the idea text is not shown yet, see R3\n")


async def test_a_fix_that_skips_a_test_blocks_the_verdict_until_the_change_is_justified(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {}, {}]  # the lanes pass from the second round on
    fake.fix_effects = [skip_a_test, justify_the_skip]
    graph, config, _ = await run_to_the_end("integrity-block")
    values = (await graph.aget_state(config)).values
    assert len(fake.fix_calls) == 2
    second_failures, _ = fake.fix_calls[1]
    assert [f for f in second_failures if f.startswith("[integrity]")] and all(f.startswith("[integrity]") for f in second_failures)
    assert "tests/test_core.py" in second_failures[0] and "fix-1" in second_failures[0]
    [finding] = values["integrity_findings"].values()
    assert (finding["session"], finding["kind"], finding["status"]) == ("fix-1", "skip_added", "justified")
    assert finding["reason"] == "the idea text is not shown yet, see R3"
    verification = values["verification"]
    assert verification["passed"] is True and verification["integrity"] == "block"
    assert "Test integrity: CLEAR" in verification["report"]


async def test_with_warn_a_skipped_test_is_reported_but_does_not_block(fake: FakeLanes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INTEGRITY", "warn")
    clear_settings_cache()
    fake.verify_rounds = [{"tests": False}, {}]
    fake.fix_effects = [skip_a_test]
    graph, config, _ = await run_to_the_end("integrity-warn")
    values = (await graph.aget_state(config)).values
    assert len(fake.fix_calls) == 1 and values["verification"]["passed"] is True
    assert "WARNING (1 open" in values["verification"]["report"]


async def test_with_the_guard_off_nothing_is_checked(fake: FakeLanes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INTEGRITY", "off")
    clear_settings_cache()
    fake.verify_rounds = [{"tests": False}, {}]
    fake.fix_effects = [skip_a_test]
    graph, config, _ = await run_to_the_end("integrity-off")
    values = (await graph.aget_state(config)).values
    assert values["verification"]["passed"] is True and values["integrity_findings"] == {}
    assert "Test integrity" not in values["verification"]["report"]


def report_listing(count: int) -> dict[str, Any]:
    report = lane_report("tests").model_copy(update={"evidence": {"tests": count, "passed": count, "problem": ""}})
    return report.model_dump()


def test_a_drop_in_the_reported_test_count_blocks_the_verdict_and_becomes_the_new_baseline(tmp_path: Path) -> None:
    reports = {"tests": report_listing(9), "quality": lane_report("quality").model_dump(), "requirements": lane_report("requirements").model_dump()}
    state = state_with_spend(tmp_path, 1.0, lane_reports=reports, integrity_test_count=12, verification={"passed": False, "attempts": 1, "report": "", "lanes": []})
    update = verdict_node(state)  # type: ignore[arg-type]
    [finding] = update["integrity_findings"].values()
    assert (finding["kind"], finding["session"], finding["before"]) == ("count_dropped", "lane-tests-2", 12)
    assert update["verification"]["passed"] is False and update["integrity_test_count"] == 9
    assert "BLOCKED (1 open)" in update["verification"]["report"]
    again = verdict_node({**state, "integrity_test_count": 9, "integrity_findings": update["integrity_findings"]})  # type: ignore[arg-type]
    assert "integrity_findings" not in again and again["verification"]["passed"] is False  # still open, no new drop
    restored = verdict_node({**state, "lane_reports": {**reports, "tests": report_listing(12)}, "integrity_findings": update["integrity_findings"]})  # type: ignore[arg-type]
    assert restored["integrity_findings"][finding["id"]]["status"] == "restored" and restored["verification"]["passed"] is True


def test_a_round_without_a_test_report_keeps_the_baseline(tmp_path: Path) -> None:
    reports = {lane: lane_report(lane).model_dump() for lane in LANES}
    update = verdict_node(state_with_spend(tmp_path, 1.0, lane_reports=reports, integrity_test_count=12))  # type: ignore[arg-type]
    assert "integrity_test_count" not in update and "integrity_findings" not in update
    assert update["verification"]["passed"] is True


def test_a_round_skipped_for_budget_still_reports_what_sessions_did_to_the_tests(tmp_path: Path) -> None:
    finding = {
        "id": "v1:T02:deleted:tests/test_a.py:", "session": "T02", "iteration": 1, "kind": "deleted", "path": "tests/test_a.py",
        "block": "", "before": 2, "detail": "T02 deleted the test file tests/test_a.py (2 assertion(s)).", "status": "open",
        "reason": "", "stale": "",
    }  # fmt: skip
    update = verify_node(state_with_spend(tmp_path, 25.0, integrity_findings={finding["id"]: finding}))  # type: ignore[arg-type]
    assert update["verification"]["integrity"] == "block"
    assert "Test integrity: BLOCKED" in update["verification"]["report"] and "tests/test_a.py" in update["verification"]["report"]


def test_an_old_test_count_justification_in_the_workspace_does_not_clear_a_new_drop(tmp_path: Path) -> None:
    (tmp_path / "quality").mkdir()
    (tmp_path / "quality" / "TEST_CHANGES.md").write_text("- test-count: duplicates were merged in v0.1\n")
    reports = {"tests": report_listing(9), "quality": lane_report("quality").model_dump(), "requirements": lane_report("requirements").model_dump()}
    update = verdict_node(state_with_spend(tmp_path, 1.0, lane_reports=reports, integrity_test_count=12))  # type: ignore[arg-type]
    [finding] = update["integrity_findings"].values()
    assert finding["status"] == "open" and update["verification"]["passed"] is False


# ------------------------------------------------------------ contrastive fix loop


def touch(name: str):  # type: ignore[no-untyped-def]
    def effect(workspace: Path) -> None:
        (workspace / name).write_text(f"changed by {name}\n")

    return effect


async def test_each_fix_attempt_leaves_a_folder_that_the_next_one_can_read(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {"tests": False}]
    fake.fix_effects = [touch("one.txt"), touch("two.txt")]
    graph, config, _ = await run_to_the_end("fix-folders")
    workspace = Path((await graph.aget_state(config)).values["workspace_dir"])
    assert fake.folders_seen == [[], ["attempt-1"]]  # attempt 2 sees attempt 1's folder, attempt 1 sees none
    first = workspace / ".idea-to-mvp" / "fix" / "attempt-1"
    assert "- [tests] tests check failed" in (first / "failures.md").read_text()
    diff = (first / "diff.patch").read_text()
    assert "one.txt" in diff and "two.txt" not in diff
    assert "still failing" in (first / "after.md").read_text().lower() and "Quality lane: FAILED" in (first / "after.md").read_text()
    assert "[quality] quality check failed" in (workspace / ".idea-to-mvp" / "fix" / "attempt-2" / "failures.md").read_text()


async def test_the_attempt_history_is_kept_in_the_verification_result(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {"tests": False}]
    fake.fix_effects = [touch("one.txt"), touch("two.txt")]
    graph, config, _ = await run_to_the_end("fix-history")
    history = (await graph.aget_state(config)).values["verification"]["history"]
    assert [entry["attempt"] for entry in history] == [1, 2]
    assert [entry["result"] for entry in history] == ["failed", "failed"]
    assert history[0]["files"] == ["one.txt"] and history[1]["files"] == ["two.txt"]
    assert history[0]["failures"] == ["[tests] tests check failed"] and history[1]["failures"] == ["[quality] quality check failed"]
    assert history[1]["notes"][0]["verdict"] == "dropped" and history[1]["notes_problem"] == ""
    assert history[0]["remaining"] == 1 and history[0]["model"]


async def test_a_fix_that_works_closes_its_history_entry_as_passed(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"quality": False}, {}]
    graph, config, _ = await run_to_the_end("fix-history-passed")
    verification = (await graph.aget_state(config)).values["verification"]
    assert verification["passed"] is True and [(e["attempt"], e["result"]) for e in verification["history"]] == [(1, "passed")]


async def test_notes_that_break_the_contract_are_recorded_not_punished(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {}]
    fake.fix_outcomes = [FixOutcome(True, "Fixed it.", notes=[]), FixOutcome(True, "Fixed it.", notes=[Note("new", "h", "e")])]
    graph, config, _ = await run_to_the_end("fix-notes")
    history = (await graph.aget_state(config)).values["verification"]["history"]
    assert "no <notes>" in history[0]["notes_problem"] and history[1]["notes_problem"] == ""
    assert (await graph.aget_state(config)).values["verification"]["passed"] is True


async def test_a_loop_whose_failures_are_all_environmental_ends_without_a_fix_attempt(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}]
    fake.lane_failures = {"tests": ["ENVIRONMENT: pypi.org is blocked by the sandbox, so nothing can be installed"]}
    graph, config, updates = await run_to_the_end("fix-environment-lane")
    values = (await graph.aget_state(config)).values
    verification = values["verification"]
    assert not fake.fix_calls and "fix" not in [node for node, _ in updates] and len(fake.lane_calls) == 3
    assert verification["passed"] is False and verification["attempts"] == 0
    assert "pypi.org is blocked" in verification["environment"]
    assert "environment" in verification["report"].lower() and "another fix attempt cannot help" in verification["report"]
    assert values["stage"] == "done"


async def test_a_mixed_failure_list_is_still_fixed_and_the_fix_is_told_which_part_is_not_its_to_fix(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False, "quality": False}, {}]
    fake.lane_failures = {"tests": ["test_a failed"], "quality": ["ENVIRONMENT: the sandbox blocks the domain api.example.com"]}
    await run_to_the_end("fix-environment-mixed")
    [(failures, _)] = fake.fix_calls
    assert failures == ["[tests] test_a failed", "[quality] ENVIRONMENT: the sandbox blocks the domain api.example.com"]


async def test_a_fix_session_that_finds_an_environment_problem_ends_the_loop_without_another_round(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}]
    fake.fix_outcomes = [FixOutcome(True, "Nothing changed.", kind="environment", environment="The sandbox has no docker.")]
    graph, config, updates = await run_to_the_end("fix-environment-session")
    values = (await graph.aget_state(config)).values
    verification = values["verification"]
    assert len(fake.fix_calls) == 1 and len(fake.lane_calls) == 3  # no verification round after the fix
    assert [node for node, _ in updates if node in ("verify", "verdict", "fix", "delivery_report")] == [
        "verify", "verdict", "fix", "verify", "delivery_report",
    ]  # fmt: skip
    assert verification["passed"] is False and verification["attempts"] == 1
    assert verification["environment"] == "The sandbox has no docker." and "no docker" in verification["report"]
    assert [(e["kind"], e["result"]) for e in verification["history"]] == [("environment", "stopped")]
    assert verification["history"][0]["summary"] == "The sandbox has no docker."  # the stated reason, not the prose
    assert values["stage"] == "done"


def test_the_verdict_stops_for_an_environment_marker_whatever_the_attempts_left(tmp_path: Path) -> None:
    verification = {"passed": False, "attempts": 0, "report": "r", "lanes": [], "environment": "no docker"}
    assert route_after_verdict(state_with_spend(tmp_path, 1.0, verification=verification)) == "exhausted"  # type: ignore[arg-type]
    assert route_after_verify(state_with_spend(tmp_path, 1.0, verification=verification)) == "delivery_report"  # type: ignore[arg-type]


async def test_the_delivery_report_lists_the_fix_attempts(fake: FakeLanes) -> None:
    fake.verify_rounds = [{"tests": False}, {"quality": False}, {}]
    fake.fix_effects = [touch("one.txt"), touch("two.txt")]
    graph, config, _ = await run_to_the_end("fix-report")
    report = (await graph.aget_state(config)).values["delivery_report"]
    assert "### Fix attempts" in report and "Attempt 1" in report and "Attempt 2" in report
    assert "one.txt" in report and "hypothesis 2" in report
