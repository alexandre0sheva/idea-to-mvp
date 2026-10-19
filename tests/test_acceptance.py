"""Held-out acceptance tests: written from the PRD and the public contracts, kept outside the workspace, run in a
throwaway copy, and reported to fix sessions by test name and assertion message only."""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from plan_helpers import PRD, make_plan
from pydantic import ValidationError

from idea_to_mvp import llm
from idea_to_mvp.config import Settings, clear_settings_cache
from idea_to_mvp.demo import fixtures
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation import acceptance, reports
from idea_to_mvp.implementation import verify as lanes
from idea_to_mvp.implementation.acceptance import (
    ACCEPTANCE_REPORT,
    AcceptanceFile,
    AcceptanceSuite,
    acceptance_failures,
    load_suite,
    make_scratch,
    remove_scratch,
    requirement_status,
    save_suite,
    suite_dir,
)
from idea_to_mvp.implementation.ledger import load_ledger
from idea_to_mvp.implementation.reports import REPORTS_DIR, CaseResult, ReportedTests, read_evidence
from idea_to_mvp.implementation.verify import LANES, combine_lanes, run_fix, run_lane
from idea_to_mvp.nodes.acceptance import acceptance_author_node, author_prompt, lanes_for
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.dashboard import render_dashboard

COMMAND = f"python -m pytest acceptance --junitxml={ACCEPTANCE_REPORT}"
SECRET_LINE = "assert response.json()['total'] == 42  # the held-out expectation"


def suite(**fields: Any) -> AcceptanceSuite:
    values: dict[str, Any] = {
        "files": [AcceptanceFile(path="test_r1_journey.py", content=f"def test_R1_journey():\n    {SECRET_LINE}\n")],
        "command": COMMAND,
        "covers": ["R1"],
    }
    values.update(fields)
    return AcceptanceSuite(**values)


def settings(tmp_path: Path, **fields: Any) -> Settings:
    values: dict[str, Any] = {"output_dir": tmp_path / "out", "implementer_sandbox": "off"}
    values.update(fields)
    return Settings(_env_file=None, **values)


def read_cases(root: Path) -> list[CaseResult]:
    cases: list[CaseResult] = []
    read_evidence(root, cases=cases)
    return cases


def report_in(root: Path, document: str) -> Path:
    (root / REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    (root / REPORTS_DIR / "r.xml").write_text(document)
    return root


def junit(cases: list[tuple[str, str, str]], classname: str = "acceptance.test_mod") -> str:
    """(name, status, message) -> a JUnit document whose failure bodies hold test code."""
    rows = []
    for name, status, message in cases:
        body = ""
        if status in ("failure", "error"):
            body = f"<{status} message='{message}'>Traceback: {SECRET_LINE}</{status}>"
        elif status == "skipped":
            body = "<skipped message='later'/>"
        rows.append(f"<testcase classname='{classname}' name='{name}'>{body}</testcase>")
    return f"<testsuites><testsuite name='a' tests='{len(cases)}'>{''.join(rows)}</testsuite></testsuites>"


# ------------------------------------------------------------------ the suite


def test_a_suite_keeps_its_files_inside_its_own_folder() -> None:
    for bad in ("/etc/passwd", "../escape.py", "a/../../b.py", ".hidden/test.py", "", "notes.txt"):
        with pytest.raises(ValidationError):
            suite(files=[AcceptanceFile(path=bad, content="x")])


def test_a_suite_command_must_leave_the_acceptance_report() -> None:
    with pytest.raises(ValidationError, match=r"acceptance\.xml"):
        suite(command="python -m pytest acceptance")
    assert suite(files=[], command="", covers=[]).files == []  # no tests: no command needed


def test_a_suite_has_a_bounded_number_of_distinct_files() -> None:
    many = [AcceptanceFile(path=f"test_{n}.py", content="x") for n in range(acceptance.MAX_FILES + 1)]
    with pytest.raises(ValidationError):
        suite(files=many)
    with pytest.raises(ValidationError):
        suite(files=[AcceptanceFile(path="test_a.py", content="x"), AcceptanceFile(path="test_a.py", content="y")])


def test_the_suite_lives_outside_the_workspace_one_folder_per_version(tmp_path: Path) -> None:
    workspace = tmp_path / "out" / "projects" / "20260101-idea"
    folder = suite_dir(settings(tmp_path), workspace, 2)
    assert folder == tmp_path / "out" / "acceptance" / "20260101-idea" / "v2"
    assert not folder.is_relative_to(workspace)


def test_a_saved_suite_loads_back_and_replaces_the_previous_one(tmp_path: Path) -> None:
    folder = tmp_path / "suite"
    save_suite(folder, suite(files=[AcceptanceFile(path="old/test_old.py", content="x")]))
    paths = save_suite(folder, suite())
    assert paths == ["test_r1_journey.py"] and not (folder / "acceptance" / "old").exists()
    loaded = load_suite(folder)
    assert loaded is not None and loaded.command == COMMAND and [f.path for f in loaded.files] == ["test_r1_journey.py"]
    assert SECRET_LINE in loaded.files[0].content
    assert load_suite(tmp_path / "missing") is None


# ------------------------------------------------------------------ the throwaway copy


def test_the_scratch_copy_has_the_project_and_the_suite_but_no_git_orchestrator_files_or_dependencies(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "ws"
    for relative in ("src/app.py", ".git/config", ".idea-to-mvp/progress.json", "node_modules/x/index.js", "acceptance/mine.py", "src/acceptance/keep.py", "target/debug/big.bin", ".next/cache/x", "vendor/lib.go"):
        (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / relative).write_text("x")
    folder = tmp_path / "suite"
    save_suite(folder, suite())
    scratch = make_scratch(workspace, folder, "v1-1")
    assert scratch.parent.name == "ws" and not scratch.is_relative_to(workspace)
    assert (scratch / "src" / "app.py").exists() and (scratch / "src" / "acceptance" / "keep.py").exists()
    assert not (scratch / ".git").exists() and not (scratch / ".idea-to-mvp" / "progress.json").exists()
    assert not (scratch / "node_modules").exists() and not (scratch / "acceptance" / "mine.py").exists()
    assert not (scratch / "target").exists() and not (scratch / ".next").exists()
    assert (scratch / "vendor" / "lib.go").exists()  # vendored sources are needed to build (Go), unlike build output
    assert SECRET_LINE in (scratch / "acceptance" / "test_r1_journey.py").read_text()
    assert (scratch / ".idea-to-mvp" / "reports").is_dir()
    remove_scratch(workspace, scratch)
    assert not scratch.exists() and not (workspace / "acceptance" / "test_r1_journey.py").exists()


# ------------------------------------------------------------------ reading the result


def test_cases_carry_the_assertion_message_never_the_failure_body(tmp_path: Path) -> None:
    report_in(tmp_path, junit([("test_R1_total", "failure", "assert 41 == 42"), ("test_R2_ok", "passed", "")]))
    cases = read_cases(tmp_path)
    assert [(c.name, c.status, c.message) for c in cases] == [
        ("acceptance.test_mod::test_R1_total", "failed", "assert 41 == 42"),
        ("acceptance.test_mod::test_R2_ok", "passed", ""),
    ]
    assert all(SECRET_LINE not in c.message for c in cases)


def test_each_requirement_gets_the_verdict_of_the_tests_named_after_it(tmp_path: Path) -> None:
    report_in(tmp_path, 
        junit(
            [
                ("test_R1_saves", "passed", ""),
                ("test_r1_lists", "failure", "boom"),
                ("test_R2_exports", "passed", ""),
                ("test_R3_later", "skipped", ""),
                ("test_error_page", "passed", ""),  # names no requirement ("r" inside a word is not one)
                ("test_R12_x", "error", "e"),
            ]
        )
    )
    assert requirement_status(read_cases(tmp_path)) == {"R1": "failed", "R2": "passed", "R3": "skipped", "R12": "failed"}


def test_failures_list_names_and_messages_only(tmp_path: Path) -> None:
    report_in(tmp_path, junit([("test_R1_total", "failure", "assert 41 == 42"), ("test_R2_ok", "passed", "")]))
    failures = acceptance_failures(ReportedTests(tests=2, passed=1, failed=1), read_cases(tmp_path))
    assert failures == ["acceptance.test_mod::test_R1_total: assert 41 == 42"]
    assert acceptance_failures(ReportedTests(problem="no JUnit report"), []) == ["no JUnit report"]
    assert acceptance_failures(ReportedTests(), []) == ["The acceptance report lists no tests."]
    assert "skipped" in acceptance_failures(ReportedTests(tests=2, skipped=2), [])[0]


# ------------------------------------------------------------------ the lane


class AcceptanceQuery:
    """The lane's agent: runs 'the command' by writing a JUnit report into its working directory."""

    def __init__(self, report: str, *, claim: str = "") -> None:
        self.report = report
        self.claim = claim
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        cwd = Path(str(options.cwd))
        (cwd / ACCEPTANCE_REPORT).write_text(self.report)
        yield AssistantMessage(
            content=[TextBlock(text="Running the suite."), ToolUseBlock(id="1", name="Read", input={"file_path": "acceptance/test_r1_journey.py"})],
            model="claude-x",
        )
        report = {"lane": "acceptance", "passed": True, "commands_run": [{"command": COMMAND, "exit_code": 1}], "failures": [self.claim] if self.claim else [], "summary": f"I saw {SECRET_LINE}"}
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=4, session_id="s-acc",
            total_cost_usd=0.4, result=json.dumps(report), structured_output=report,
        )  # fmt: skip


@pytest.fixture()
def project(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "out" / "projects" / "ws"
    workspace.mkdir(parents=True)
    (workspace / "plan.json").write_text(make_plan().model_dump_json())
    (workspace / "PRD.md").write_text(PRD)
    folder = tmp_path / "out" / "acceptance" / "ws" / "v1"
    save_suite(folder, suite())
    return workspace, folder


def test_the_lane_runs_the_suite_in_a_throwaway_copy_and_judges_it_by_the_report(project: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, folder = project
    query = AcceptanceQuery(junit([("test_R1_journey", "failure", "assert 41 == 42")]), claim=f"it failed at {SECRET_LINE}")
    outcome = asyncio.run(
        run_lane(workspace, "acceptance", settings(tmp_path), budget_usd=2.0, emit=lambda e: None, acceptance_dir=folder, query_fn=query)
    )
    prompt, options = query.calls[0]
    assert COMMAND in prompt and SECRET_LINE not in prompt
    assert Path(str(options.cwd)) != workspace and not Path(str(options.cwd)).exists()  # the copy is gone afterwards
    assert {"Edit", "MultiEdit", "Write"} <= set(options.disallowed_tools)
    report = outcome.report
    assert report.lane == "acceptance" and report.passed is False and outcome.cost_usd == 0.4
    assert report.failures == ["acceptance.test_mod::test_R1_journey: assert 41 == 42"]
    assert SECRET_LINE not in json.dumps(report.model_dump())  # neither the agent's words nor the failure body
    assert report.evidence is not None and report.evidence["requirements"] == {"R1": "failed"}


def test_the_lane_keeps_its_trajectory_outside_the_workspace_and_its_cost_in_the_ledger(project: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, folder = project
    query = AcceptanceQuery(junit([("test_R1_journey", "passed", "")]))
    outcome = asyncio.run(
        run_lane(workspace, "acceptance", settings(tmp_path), budget_usd=2.0, emit=lambda e: None, acceptance_dir=folder, query_fn=query)
    )
    assert outcome.report.passed is True
    assert not list(workspace.glob(".idea-to-mvp/trajectories/*acceptance*"))  # the lane read the tests: not for agents' eyes
    [row] = load_ledger(workspace)
    assert row["kind"] == "lane" and row["cost_usd"] == 0.4 and row["label"] == "lane-acceptance-1"
    assert row["trajectory"] == ""  # agents may read the ledger: it must not say where the suite is
    assert str(folder.parent) not in (workspace / ".idea-to-mvp" / "ledger.json").read_text()
    assert [path.name for path in (folder.parent / "trajectories").iterdir()] == ["lane-acceptance-1.md"]


def test_without_a_suite_the_lane_fails_and_says_why(project: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, _ = project
    outcome = asyncio.run(
        run_lane(workspace, "acceptance", settings(tmp_path), budget_usd=2.0, emit=lambda e: None, acceptance_dir=tmp_path / "none")
    )
    assert outcome.report.passed is False and "no acceptance tests" in outcome.report.failures[0].lower()


def test_lanes_fold_into_one_verdict_over_the_expected_lanes() -> None:
    from idea_to_mvp.implementation.verify import LaneReport

    def report(lane: str, passed: bool = True) -> LaneReport:
        return LaneReport(lane=lane, passed=passed, commands_run=[], failures=[] if passed else ["x"], summary="s")  # type: ignore[arg-type]

    three = [report(lane) for lane in LANES]
    assert combine_lanes(three)["passed"] is True
    with_acceptance = combine_lanes(three, (*LANES, "acceptance"))
    assert with_acceptance["passed"] is False and [lane["lane"] for lane in with_acceptance["lanes"]][-1] == "acceptance"
    assert combine_lanes([*three, report("acceptance")], (*LANES, "acceptance"))["passed"] is True
    assert len(combine_lanes([*three, report("acceptance", False)])["lanes"]) == 3  # a stale acceptance report is ignored


def test_a_fix_for_acceptance_failures_is_told_to_fix_behaviour_not_to_look_for_the_tests(tmp_path: Path) -> None:
    class Query:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
            self.prompts.append(prompt)
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s", total_cost_usd=0.1, result="ok")

    query = Query()
    asyncio.run(run_fix(tmp_path, ["[acceptance] a::test_R1: assert 1 == 2"], settings(tmp_path), budget_usd=1.0, emit=lambda e: None, query_fn=query))
    asyncio.run(run_fix(tmp_path, ["[tests] t failed"], settings(tmp_path), budget_usd=1.0, emit=lambda e: None, query_fn=query))
    assert acceptance.FIX_NOTE in query.prompts[0] and acceptance.FIX_NOTE not in query.prompts[1]


# ------------------------------------------------------------------ in the graph (demo mode, acceptance on)



@pytest.fixture()
def acceptance_on(demo_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("ACCEPTANCE_TESTS", "on")
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()
    return demo_env


async def run_demo(thread: str) -> tuple[dict[str, Any], list[str]]:
    graph = build_graph(MemorySaver())
    config = run_config(thread)
    await graph.ainvoke(make_initial_state("A habit tracker for climbing gyms", 1), config)
    for decision in ("1. Solo climbers.", {"option": "A", "notes": ""}, {"generate": True, "notes": ""}):
        await graph.ainvoke(Command(resume=decision), config)
    nodes: list[str] = []
    async for chunk in graph.astream(Command(resume={"implement": True, "notes": ""}), config, stream_mode="updates"):
        nodes += list(chunk)
    return (await graph.aget_state(config)).values, nodes


async def test_a_demo_run_writes_a_held_out_suite_and_verifies_with_four_lanes(acceptance_on: Path) -> None:
    values, nodes = await run_demo("acceptance-demo")
    assert nodes.index("acceptance_author") < nodes.index("implementer")
    assert nodes.count("verify_lane") == 4
    suite = values["acceptance"]
    workspace = Path(values["workspace_dir"])
    folder = Path(suite["dir"])
    assert folder == acceptance_on / "acceptance" / workspace.name / "v1" and suite["covers"] == ["R1", "R2"]
    assert not (workspace / "acceptance").exists() and not list(workspace.rglob("test_acceptance.py"))
    verification = values["verification"]
    assert verification["passed"] is True
    [lane] = [lane for lane in verification["lanes"] if lane["lane"] == "acceptance"]
    assert lane["passed"] is True and lane["evidence"]["requirements"] == {"R1": "passed", "R2": "passed"}
    assert not list(workspace.glob(".idea-to-mvp/trajectories/*acceptance*"))
    assert any(row["label"] == "lane-acceptance-1" for row in load_ledger(workspace))
    html = render_dashboard(values)
    assert "data-lane='acceptance'" in html and "Acceptance" in html


async def test_a_failing_held_out_test_reaches_the_fix_session_by_name_and_message_only(
    acceptance_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = (
        "import unittest\n\nfrom demo_app.core import add_entry\n\n\nclass AcceptanceTests(unittest.TestCase):\n"
        "    def test_R2_the_newest_entry_comes_first(self):\n"
        f"        # {SECRET_LINE}\n"
        "        self.assertEqual(add_entry(['a'], 'b'), ['b', 'a'], 'R2: the newest entry must come first')\n"
    )
    monkeypatch.setitem(
        fixtures.STRUCTURED,
        AcceptanceSuite,
        lambda messages: fixtures.acceptance_suite(messages).model_copy(
            update={"files": [AcceptanceFile(path="__init__.py", content=""), AcceptanceFile(path="test_acceptance.py", content=failing)]}
        ),
    )
    seen: list[list[str]] = []
    real_fix = lanes.run_fix

    async def fix(workspace: Path, failures: list[str], settings: Any, **kwargs: Any) -> Any:
        seen.append(failures)
        return await real_fix(workspace, failures, settings, **kwargs)

    monkeypatch.setattr(lanes, "run_fix", fix)
    values, _ = await run_demo("acceptance-failing")
    assert values["verification"]["passed"] is False and seen
    [line] = [failure for failure in seen[0] if failure.startswith("[acceptance]")]
    assert "test_R2_the_newest_entry_comes_first" in line and "R2: the newest entry must come first" in line
    assert SECRET_LINE not in json.dumps(seen) and "add_entry" not in line
    html = render_dashboard(values)
    assert "req-acceptance failed" in html


async def test_acceptance_is_off_by_default_in_demo_mode(demo_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()
    values, nodes = await run_demo("acceptance-off")
    assert values["acceptance"] == {} and nodes.count("verify_lane") == 3


def test_the_setting_is_on_by_default_off_in_demo_and_explicit_values_win(tmp_path: Path) -> None:
    assert settings(tmp_path).acceptance_enabled is True
    assert settings(tmp_path, demo_mode=True).acceptance_enabled is False
    assert settings(tmp_path, demo_mode=True, acceptance_tests="on").acceptance_enabled is True
    assert settings(tmp_path, acceptance_tests="off").acceptance_enabled is False


def test_the_author_sees_the_prd_contracts_commands_and_later_changes_but_no_code() -> None:
    plan = make_plan()
    prompt = author_prompt(PRD, plan, ["Add CSV export."])
    assert "## PRD.md" in prompt and plan.contracts[0].name in prompt and plan.commands.test in prompt
    assert "2. Add CSV export." in prompt and "wins where it conflicts" in prompt
    assert plan.tasks[0].scope not in prompt  # tasks (what will be built, and how) are not the author's business


def test_a_later_version_gets_its_own_suite_and_lanes_follow_the_current_version(acceptance_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = acceptance_on / "projects" / "ws"
    workspace.mkdir(parents=True)
    (workspace / "plan.json").write_text(make_plan().model_dump_json())
    (workspace / "PRD.md").write_text(PRD)
    prompts: list[str] = []
    real = llm.invoke_structured

    def spy(runtime: Any, messages: Any, schema: Any, **kwargs: Any) -> Any:
        prompts.append(str(messages[-1].content))
        return real(runtime, messages, schema, **kwargs)

    monkeypatch.setattr(llm, "invoke_structured", spy)
    state = {**make_initial_state("idea", 1), "workspace_dir": str(workspace), "iteration": 2, "change_requests": ["Add CSV export."]}
    update = acceptance_author_node(state)  # type: ignore[arg-type]
    assert update["acceptance"]["iteration"] == 2 and Path(update["acceptance"]["dir"]).name == "v2"
    assert "Add CSV export." in prompts[0]
    assert lanes_for({**state, **update}) == (*LANES, "acceptance")  # type: ignore[arg-type]
    assert lanes_for({**state, **update, "iteration": 3}) == LANES  # type: ignore[arg-type]  # a stale suite is not run


def test_no_plan_or_an_empty_answer_means_no_suite_and_no_acceptance_lane(acceptance_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = acceptance_on / "projects" / "ws"
    workspace.mkdir(parents=True)
    state = {**make_initial_state("idea", 1), "workspace_dir": str(workspace)}
    no_plan = acceptance_author_node(state)  # type: ignore[arg-type]
    assert no_plan["acceptance"]["files"] == [] and "No plan" in no_plan["acceptance"]["note"]
    (workspace / "plan.json").write_text(make_plan().model_dump_json())
    monkeypatch.setitem(fixtures.STRUCTURED, AcceptanceSuite, lambda messages: AcceptanceSuite(files=[], command="", covers=[]))
    empty = acceptance_author_node(state)  # type: ignore[arg-type]
    assert empty["acceptance"]["dir"] == "" and "wrote no tests" in empty["acceptance"]["note"]
    assert lanes_for({**state, **empty}) == LANES  # type: ignore[arg-type]
    assert not (acceptance_on / "acceptance").exists()



# ------------------------------------------------------------------ review fixes


def test_a_project_copy_that_fails_is_a_failing_lane_not_a_crash(project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, folder = project

    def broken(*args: Any) -> Path:
        raise shutil.Error([("src/x", "dst/x", "file vanished during copy")])

    monkeypatch.setattr(acceptance, "make_scratch", broken)
    outcome = asyncio.run(
        run_lane(workspace, "acceptance", settings(tmp_path), budget_usd=2.0, emit=lambda e: None, acceptance_dir=folder, query_fn=AcceptanceQuery(""))
    )
    assert outcome.report.passed is False and "could not copy the project" in outcome.report.failures[0]


def test_the_copy_skips_sockets_and_pipes(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "ws"
    (workspace / "run").mkdir(parents=True)
    os.mkfifo(workspace / "run" / "events.fifo")
    (workspace / "app.py").write_text("x")
    folder = tmp_path / "suite"
    save_suite(folder, suite())
    scratch = make_scratch(workspace, folder, "v1-1")
    assert (scratch / "app.py").exists() and not (scratch / "run" / "events.fifo").exists()
    remove_scratch(workspace, scratch)


@pytest.mark.parametrize(
    "command",
    [
        f"python -m pytest --junitxml={ACCEPTANCE_REPORT}",  # the project's own tests
        f"python -m pytest tests/acceptance_helpers --junitxml={ACCEPTANCE_REPORT}",
    ],
)
def test_a_command_that_does_not_run_the_acceptance_folder_is_rejected(command: str) -> None:
    with pytest.raises(ValidationError, match="acceptance/"):
        suite(command=command)


@pytest.mark.parametrize(
    "command",
    [
        f"python -m pytest ./acceptance/ --junitxml={ACCEPTANCE_REPORT}",
        f"npx vitest run acceptance --reporter=junit --outputFile={ACCEPTANCE_REPORT}",
        f"python -m pytest acceptance/test_r1.py --junitxml={ACCEPTANCE_REPORT}",
    ],
)
def test_commands_that_run_the_acceptance_folder_are_accepted(command: str) -> None:
    assert suite(command=command).command == command


def test_a_suite_that_is_gone_from_disk_adds_no_lane(tmp_path: Path) -> None:
    folder = tmp_path / "suite"
    save_suite(folder, suite())
    state = {**make_initial_state("idea", 1), "acceptance": {"iteration": 1, "dir": str(folder), "files": ["test_r1_journey.py"]}}
    assert lanes_for(state) == (*LANES, "acceptance")  # type: ignore[arg-type]
    shutil.rmtree(folder)
    assert lanes_for(state) == LANES  # type: ignore[arg-type]


def test_no_lane_prompt_miscounts_the_lanes_running_beside_it(tmp_path: Path) -> None:
    from idea_to_mvp.implementation.verify import build_acceptance_prompt, build_lane_prompt

    prompts = [build_lane_prompt(lane, make_plan(), PRD) for lane in LANES] + [build_acceptance_prompt(make_plan(), COMMAND)]
    assert all("Two other lanes" not in prompt and "(tests, quality, requirements)" not in prompt for prompt in prompts)


def test_requirement_ids_come_from_the_test_name_not_its_file_or_parameters(tmp_path: Path) -> None:
    report_in(
        tmp_path,
        junit([("test_R2_history", "failure", "boom"), ("test_R1_journey[r2-case]", "passed", "")], classname="acceptance.test_r1_journey"),
    )
    assert requirement_status(read_cases(tmp_path)) == {"R1": "passed", "R2": "failed"}


def test_the_lane_parses_the_report_once(project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, folder = project
    calls: list[str] = []
    real = reports._root_of

    def counting(path: Path) -> Any:
        calls.append(path.name)
        return real(path)

    monkeypatch.setattr(reports, "_root_of", counting)
    asyncio.run(
        run_lane(workspace, "acceptance", settings(tmp_path), budget_usd=2.0, emit=lambda e: None, acceptance_dir=folder,
                 query_fn=AcceptanceQuery(junit([("test_R1_journey", "passed", "")])))
    )  # fmt: skip
    assert calls == ["acceptance.xml"]


def test_every_acceptance_record_has_the_same_shape(acceptance_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = acceptance_on / "projects" / "ws"
    workspace.mkdir(parents=True)
    state = {**make_initial_state("idea", 1), "workspace_dir": str(workspace)}
    records = [acceptance_author_node(state)["acceptance"]]  # type: ignore[arg-type]
    (workspace / "plan.json").write_text(make_plan().model_dump_json())
    (workspace / "PRD.md").write_text(PRD)
    records.append(acceptance_author_node(state)["acceptance"])  # type: ignore[arg-type]
    monkeypatch.setitem(fixtures.STRUCTURED, AcceptanceSuite, lambda messages: AcceptanceSuite(files=[], command="", covers=[]))
    records.append(acceptance_author_node(state)["acceptance"])  # type: ignore[arg-type]
    assert [set(record) for record in records] == [{"iteration", "dir", "files", "covers", "note"}] * 3
