"""`idea-to-mvp bench`: the golden ideas through the headless pipeline, scored and comparable (demo mode, offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from idea_to_mvp import bench, cli, llm
from idea_to_mvp.config import get_settings
from idea_to_mvp.headless import RunSummary

GOLDEN = json.loads(bench.DEFAULT_IDEAS.read_text(encoding="utf-8"))
IDEAS = [bench.BenchIdea(id=g["id"], idea=g["idea"], preferences=g["preferences"]) for g in GOLDEN]


@pytest.fixture()
def cwd(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(demo_env)  # never pick up the developer's real .env
    return demo_env


def run_bench(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    """(exit code, the JSON on stdout, stderr) of one `bench` call."""
    code = cli.main(["bench", *argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else {}, captured.err


def result(idea_id: str = "a", **fields: Any) -> bench.IdeaResult:
    base: dict[str, Any] = {
        "id": idea_id,
        "status": "scored",
        "verified": True,
        "lanes_passed": 3,
        "lanes_total": 3,
        "agent_cost_usd": 2.0,
        "planning_cost_usd": 0.5,
    }
    return bench.IdeaResult(**{**base, **fields})


# ------------------------------------------------------------------ the ideas


def test_there_are_about_eight_golden_ideas_across_stacks() -> None:
    assert 7 <= len(GOLDEN) <= 9
    stacks = {g["preferences"].get("stack_hints", "") for g in GOLDEN}
    assert len(stacks) >= 6 and {g["preferences"].get("platform", "any") for g in GOLDEN} >= {
        "web",
        "cli",
        "api",
        "mobile",
    }


def test_the_golden_ideas_ship_inside_the_package_so_an_installed_bench_finds_them() -> None:
    package = Path(bench.__file__).resolve().parent
    assert bench.DEFAULT_IDEAS.is_file() and bench.DEFAULT_IDEAS.parent == package


def test_ideas_are_selected_by_id_and_an_unknown_id_is_refused(tmp_path: Path) -> None:
    path = bench.DEFAULT_IDEAS
    assert [i.id for i in bench.load_ideas(path)] == [g["id"] for g in GOLDEN]
    assert [i.id for i in bench.load_ideas(path, ["recipe-planner", "climbing-log"])] == [
        "recipe-planner",
        "climbing-log",
    ]
    with pytest.raises(bench.BenchError, match="no-such"):
        bench.load_ideas(path, ["climbing-log", "no-such"])
    with pytest.raises(bench.BenchError, match="cannot read"):
        bench.load_ideas(tmp_path / "missing.json")


# ------------------------------------------------------------------ settings overrides


def test_overrides_are_env_style_settings_the_bench_does_not_own() -> None:
    assert bench.parse_overrides(["MODEL_PROFILE=fast", "harness_tuning=pilot"]) == {
        "MODEL_PROFILE": "fast",
        "HARNESS_TUNING": "pilot",
    }
    for bad in (
        "MODEL_PROFILE",
        "NO_SUCH_SETTING=1",
        "OUTPUT_DIR=/tmp/x",
        "DEMO_MODE=false",
        "IMPLEMENTER_MAX_TOTAL_USD=3",
    ):
        with pytest.raises(bench.BenchError):
            bench.parse_overrides([bad])


def test_overrides_are_applied_for_the_run_only(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    with bench.applied_env({"MODEL_PROFILE": "fast"}) as settings:
        assert settings.model_profile == "fast" and get_settings().model_profile == "fast"
    assert get_settings().model_profile == "balanced"
    with (
        pytest.raises(bench.BenchError, match="MODEL_PROFILE"),
        bench.applied_env({"MODEL_PROFILE": "nonsense"}),
    ):
        pass


# ------------------------------------------------------------------ the snapshot


def snapshot(**changes: Any) -> dict[str, Any]:
    base = {
        "version": 1,
        "mode": "demo",
        "settings": {"model_profile": "balanced", "harness_tuning": "off"},
        "overrides": {},
        "ideas": [{"id": "a", "sha": "1"}],
    }
    return {**base, **changes}


def test_a_snapshot_that_differs_names_every_difference() -> None:
    assert bench.snapshot_differences(snapshot(), snapshot()) == []
    changed = snapshot(settings={"model_profile": "fast", "harness_tuning": "off"}, mode="live")
    differences = bench.snapshot_differences(snapshot(), changed)
    assert any("model_profile" in d and "balanced" in d and "fast" in d for d in differences)
    assert any("mode" in d for d in differences)
    other_ideas = snapshot(ideas=[{"id": "a", "sha": "2"}])
    assert any("a" in d for d in bench.snapshot_differences(snapshot(), other_ideas))


# ------------------------------------------------------------------ the budgets


def test_each_idea_gets_the_per_idea_cap_until_the_global_cap_runs_low() -> None:
    assert bench.idea_budget(per_idea=5.0, total=None, spent=100.0) == 5.0
    assert bench.idea_budget(per_idea=5.0, total=20.0, spent=0.0) == 5.0
    assert bench.idea_budget(per_idea=5.0, total=20.0, spent=17.0) == pytest.approx(3.0)
    assert bench.idea_budget(per_idea=5.0, total=20.0, spent=20.0) is None
    assert (
        bench.idea_budget(per_idea=5.0, total=20.0, spent=19.999) is None
    )  # below a cent is no budget


# ------------------------------------------------------------------ scoring


def summary(outcome: str, code: int, **fields: Any) -> RunSummary:
    return RunSummary(
        thread_id="t",
        title="x",
        outcome=outcome,
        exit_code=code,
        message=fields.pop("message", ""),
        **fields,
    )  # type: ignore[arg-type]


LANES = [
    {
        "lane": "tests",
        "passed": True,
        "evidence": {"passed": 9, "failed": 0, "errors": 0, "skipped": 1},
    },
    {"lane": "quality", "passed": True},
    {"lane": "requirements", "passed": False},
    {
        "lane": "acceptance",
        "passed": False,
        "evidence": {"passed": 6, "failed": 1, "errors": 1, "skipped": 2},
    },
]


def test_a_delivered_run_is_scored_from_the_state() -> None:
    values = {
        "verification": {"passed": False, "attempts": 2, "lanes": LANES},
        "integrity_findings": {"a": {"status": "open"}, "b": {"status": "justified"}},
    }
    scored = bench.score_run(
        "x",
        summary(
            "failed", 2, planning_cost_usd=0.4, agent_cost_usd=3.0, unpriced_calls=1, workspace="/w"
        ),
        values,
        12.5,
    )
    assert (scored.status, scored.verified, scored.fix_attempts) == ("scored", False, 2)
    assert (scored.lanes_passed, scored.lanes_total) == (2, 4)
    assert (scored.acceptance_passed, scored.acceptance_total) == (6, 8)
    assert (scored.integrity_flags, scored.integrity_open) == (2, 1)
    assert (scored.planning_cost_usd, scored.agent_cost_usd, scored.wall_seconds) == (
        0.4,
        3.0,
        12.5,
    )
    assert scored.total_cost_usd == pytest.approx(3.4) and scored.workspace == "/w"


@pytest.mark.parametrize(
    ("outcome", "code", "reason"),
    [
        ("error", 1, "provider exploded"),
        ("gate", 3, "Waiting at the answers gate"),
        ("interrupted", 1, "stopped before"),
        ("ended", 3, "ended"),
    ],
)
def test_a_run_that_did_not_deliver_is_unscored_not_zero(
    outcome: str, code: int, reason: str
) -> None:
    unscored = bench.score_run(
        "x", summary(outcome, code, message=reason, agent_cost_usd=1.5), {}, 3.0
    )
    assert unscored.status == "unscored" and unscored.verified is None and reason in unscored.reason
    assert unscored.agent_cost_usd == 1.5  # the money was spent all the same


def test_a_verified_run_without_an_acceptance_lane_has_no_acceptance_rate() -> None:
    values = {"verification": {"passed": True, "attempts": 0, "lanes": LANES[:2]}}
    scored = bench.score_run("x", summary("verified", 0), values, 1.0)
    assert (
        scored.verified is True
        and scored.acceptance_total is None
        and scored.acceptance_rate is None
    )


def test_the_totals_leave_unscored_runs_out_of_every_rate() -> None:
    results = [
        result("a"),
        result(
            "b",
            verified=False,
            lanes_passed=1,
            fix_attempts=2,
            integrity_flags=1,
            agent_cost_usd=4.0,
        ),
        result(
            "c",
            status="unscored",
            verified=None,
            lanes_passed=None,
            lanes_total=None,
            agent_cost_usd=9.0,
            planning_cost_usd=0.0,
            reason="boom",
        ),
    ]
    totals = bench.totals(results)
    assert (totals.ideas, totals.scored, totals.unscored, totals.verified) == (3, 2, 1, 1)
    assert totals.verified_rate == 0.5 and totals.lane_pass_rate == pytest.approx(4 / 6)
    assert totals.spend_usd == pytest.approx(2.5 + 4.5) and totals.unscored_spend_usd == 9.0
    assert totals.cost_per_idea == pytest.approx(3.5) and totals.cost_per_verified == pytest.approx(
        7.0
    )
    assert totals.verified_per_usd == pytest.approx(1 / 7)
    assert (totals.fix_attempts, totals.integrity_flags) == (2, 1)


def test_the_totals_of_nothing_scored_are_unknown_not_zero() -> None:
    totals = bench.totals([result("c", status="unscored", verified=None, reason="boom")])
    assert totals.scored == 0 and totals.verified_rate is None and totals.cost_per_idea is None
    assert totals.verified_per_usd is None and totals.acceptance_rate is None


def test_a_free_run_has_no_price_per_verified_delivery() -> None:
    totals = bench.totals([result("a", agent_cost_usd=0.0, planning_cost_usd=0.0)])
    assert (
        totals.verified_rate == 1.0
        and totals.verified_per_usd is None
        and totals.cost_per_verified == 0.0
    )


# ------------------------------------------------------------------ the whole bench, offline


def test_dry_run_prices_the_bench_without_a_model_call_or_a_run_folder(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a dry run makes no model call")

    monkeypatch.setattr(llm, "invoke_text", forbidden)
    monkeypatch.setattr(llm, "invoke_structured", forbidden)
    code, report, _err = run_bench(
        capsys, "--name", "dry", "--ideas", "climbing-log,invoice-tool", "--dry-run"
    )
    assert code == 0 and report["dry_run"] is True
    assert [i["id"] for i in report["ideas"]] == ["climbing-log", "invoice-tool"]
    assert report["estimate"]["total_usd"] >= 0 and report["estimate"]["per_idea_cap_usd"] > 0
    assert not (cwd / "bench").exists()


def test_the_bench_runs_the_ideas_headless_and_scores_them(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, report, err = run_bench(capsys, "--name", "first", "--ideas", "climbing-log,invoice-tool")
    assert code == 0 and report["name"] == "first" and report["mode"] == "demo"
    assert [r["id"] for r in report["results"]] == ["climbing-log", "invoice-tool"]
    assert all(r["status"] == "scored" and r["verified"] for r in report["results"])
    assert report["totals"]["verified"] == 2 and report["totals"]["verified_rate"] == 1.0
    assert "[climbing-log]" in err and "[invoice-tool]" in err

    run_dir = cwd / "bench" / "first"
    assert json.loads((run_dir / "config.json").read_text(encoding="utf-8"))["mode"] == "demo"
    stored = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in stored["results"]] == ["climbing-log", "invoice-tool"]
    # the run has its own output root: sessions, workspaces, and library never mix with another configuration's
    assert (run_dir / "output" / "sessions.db").is_file()
    assert Path(report["results"][0]["workspace"]).is_relative_to(run_dir / "output" / "projects")
    assert not (cwd / "projects").exists() and not (cwd / "sessions.db").exists()
    assert get_settings().output_dir == cwd  # the overrides ended with the run


def test_the_project_preferences_of_an_idea_reach_its_session(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from idea_to_mvp.graph import open_graph, read_thread

    run_bench(capsys, "--name", "prefs", "--ideas", "invoice-tool")

    async def read() -> dict[str, Any]:
        settings = get_settings().model_copy(
            update={"output_dir": cwd / "bench" / "prefs" / "output", "checkpoint_db": None}
        )
        async with open_graph(settings) as graph:
            return (await read_thread(graph, "bench-invoice-tool")).values

    values = asyncio.run(read())
    assert (
        values["preferences"]["platform"] == "web"
        and values["preferences"]["stack_hints"] == "TypeScript + Postgres"
    )


def test_a_second_call_keeps_the_scored_ideas_and_runs_only_the_rest(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_bench(capsys, "--name", "again", "--ideas", "climbing-log")
    calls: list[str] = []
    real = bench._execute

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[2].id)
        return await real(*args, **kwargs)

    monkeypatch.setattr(bench, "_execute", counting)
    # a different idea list is a different experiment, so the same name refuses it
    code, _report, err = run_bench(
        capsys, "--name", "again", "--ideas", "climbing-log,invoice-tool"
    )
    assert code == 1 and "ideas" in err and calls == []

    code, report, err = run_bench(capsys, "--name", "again", "--ideas", "climbing-log")
    assert code == 0 and calls == [] and "already scored" in err
    assert [r["id"] for r in report["results"]] == ["climbing-log"]


def test_a_run_folder_refuses_other_settings(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_bench(capsys, "--name", "fixed", "--ideas", "climbing-log")
    code, _report, err = run_bench(
        capsys, "--name", "fixed", "--ideas", "climbing-log", "--profile", "fast"
    )
    assert code == 1 and "model_profile" in err and "different settings" in err
    code, _report, err = run_bench(
        capsys, "--name", "fixed", "--ideas", "climbing-log", "--set", "MAX_FIX_ATTEMPTS=0"
    )
    assert code == 1 and "max_fix_attempts" in err


def test_flags_choose_the_profile_the_tuning_and_the_library_per_run(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, report, _err = run_bench(
        capsys,
        "--name",
        "arms",
        "--ideas",
        "climbing-log",
        "--profile",
        "fast",
        "--harness-tuning",
        "pilot",
        "--library",
        "on",
    )
    assert code == 0
    settings = json.loads((cwd / "bench" / "arms" / "config.json").read_text(encoding="utf-8"))[
        "settings"
    ]
    assert (
        settings["model_profile"] == "fast"
        and settings["harness_tuning"] == "pilot"
        and settings["library"] == "on"
    )
    assert report["totals"]["scored"] == 1


def test_a_run_that_stops_is_unscored_and_a_later_call_retries_only_it(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bench._execute

    async def flaky(
        graph: Any, registry: Any, idea: bench.BenchIdea, *args: Any, **kwargs: Any
    ) -> Any:
        if idea.id == "invoice-tool":
            raise RuntimeError("provider exploded")
        return await real(graph, registry, idea, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(bench, "_execute", flaky)
        code, report, _err = run_bench(
            capsys, "--name", "flaky", "--ideas", "climbing-log,invoice-tool"
        )
    by_id = {r["id"]: r for r in report["results"]}
    assert code == 3  # incomplete: an unscored idea is neither a pass nor a failure
    assert by_id["climbing-log"]["status"] == "scored"
    assert (
        by_id["invoice-tool"]["status"] == "unscored"
        and "provider exploded" in by_id["invoice-tool"]["reason"]
    )
    assert (
        report["totals"]["scored"] == 1
        and report["totals"]["unscored"] == 1
        and report["totals"]["verified_rate"] == 1.0
    )

    code, report, err = run_bench(capsys, "--name", "flaky", "--ideas", "climbing-log,invoice-tool")
    assert code == 0 and report["totals"]["scored"] == 2
    assert "already scored" in err  # climbing-log kept, invoice-tool run again


def test_the_global_cap_leaves_the_last_ideas_unrun(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []

    async def pricey(
        graph: Any,
        registry: Any,
        idea: bench.BenchIdea,
        thread_id: str,
        settings: Any,
        progress: Any,
    ) -> Any:
        seen.append(settings.implementer_max_total_usd)
        done = summary(
            "verified", 0, agent_cost_usd=4.0, planning_cost_usd=1.0, gate="iterate_gate"
        )
        return done, {"verification": {"passed": True, "attempts": 0, "lanes": LANES[:2]}}

    monkeypatch.setattr(bench, "_execute", pricey)
    code, report, err = run_bench(
        capsys,
        "--name",
        "capped",
        "--ideas",
        "climbing-log,invoice-tool,recipe-planner",
        "--max-usd-per-idea",
        "5",
        "--set",
        "IMPLEMENTER_MAX_TASK_USD=2",  # a per-idea budget must fit two task reservations
        "--max-total-usd",
        "7",
    )
    assert seen == [5.0, 2.0]  # the second idea got what was left
    by_id = {r["id"]: r for r in report["results"]}
    assert (
        by_id["recipe-planner"]["status"] == "unscored"
        and "budget" in by_id["recipe-planner"]["reason"]
    )
    assert code == 3 and "budget" in err


def test_live_needs_a_global_cap_and_demo_mode_refuses_live(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _report, err = run_bench(capsys, "--name", "x", "--ideas", "climbing-log", "--live")
    assert code == 1 and "DEMO_MODE" in err
    monkeypatch.setenv("DEMO_MODE", "false")
    from idea_to_mvp.config import clear_settings_cache

    clear_settings_cache()
    code, _report, err = run_bench(capsys, "--name", "x", "--ideas", "climbing-log", "--live")
    assert code == 1 and "--max-total-usd" in err
    code, _report, err = run_bench(capsys, "--name", "x", "--ideas", "climbing-log")
    assert code == 1 and "--live" in err
    assert not (cwd / "bench").exists()


# ------------------------------------------------------------------ comparing runs


def test_compare_scores_only_the_ideas_every_run_scored(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_bench(capsys, "--name", "one", "--ideas", "climbing-log,invoice-tool")
    run_bench(capsys, "--name", "two", "--ideas", "climbing-log,invoice-tool", "--profile", "fast")
    code = cli.main(["bench", "--compare", "one", "two", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and [r["name"] for r in report["runs"]] == ["one", "two"]
    assert report["common_ideas"] == ["climbing-log", "invoice-tool"]
    names = {d["setting"] for d in report["differences"]}
    assert "model_profile" in names and all(
        n == "model_profile" or n.startswith("models.") for n in names
    )
    assert all(run["totals"]["scored"] == 2 for run in report["runs"])

    code = cli.main(["bench", "--compare", "one", "two"])
    text = capsys.readouterr().out
    assert (
        code == 0
        and "model_profile" in text
        and "verified" in text.lower()
        and "climbing-log" in text
    )


def test_compare_drops_an_idea_one_run_left_unscored_or_changed() -> None:
    def run(name: str, *results: bench.IdeaResult, sha: str = "1") -> bench.RunRecord:
        snap = snapshot(ideas=[{"id": r.id, "sha": sha} for r in results])
        return bench.RunRecord(name=name, snapshot=snap, results=list(results))

    unscored = result("b", status="unscored", verified=None, reason="boom")
    first = run("one", result("a"), result("b"), result("c"))
    second = run("two", result("a"), unscored, result("c", verified=False), sha="1")
    third = run("three", result("a"), result("b"), result("c"), sha="2")
    compared = bench.compare_runs([first, second])
    assert compared.common_ideas == ["a", "c"] and "b" in " ".join(compared.notes)
    assert bench.compare_runs([first, third]).common_ideas == []  # same ids, different idea text


def test_compare_refuses_a_run_that_does_not_exist(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["bench", "--compare", "nope", "nada"]) == 1
    assert "no bench run" in capsys.readouterr().err
    assert cli.main(["bench", "--compare", "only-one"]) == 1


def test_bench_takes_a_name_unless_it_compares(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["bench", "--ideas", "climbing-log"])
    assert raised.value.code == 1


# ------------------------------------------------------------------ a budget that fits one task is refused


def test_a_per_idea_budget_that_fits_one_task_session_is_refused(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each running task reserves IMPLEMENTER_MAX_TASK_USD, so with a whole-run cap of the same size only the first
    task of a plan ever starts (a live run delivered T01 of five tasks and failed verification for the missing rest)."""
    monkeypatch.setenv("IMPLEMENTER_MAX_TASK_USD", "5")
    code, _report, err = run_bench(
        capsys, "--name", "tight", "--ideas", "climbing-log", "--max-usd-per-idea", "6"
    )
    assert code == 1 and "IMPLEMENTER_MAX_TASK_USD" in err and "at least 10" in err
    assert not (cwd / "bench" / "tight").exists()  # refused before anything was written

    code, _report, _err = run_bench(
        capsys,
        "--name",
        "tight",
        "--ideas",
        "climbing-log",
        "--max-usd-per-idea",
        "6",
        "--set",
        "IMPLEMENTER_MAX_TASK_USD=2",
    )
    assert code == 0  # lowering the per-task cap for the run is the other way out

    code, _report, err = run_bench(
        capsys, "--dry-run", "--ideas", "climbing-log", "--max-usd-per-idea", "6"
    )
    assert code == 1 and "at least 10" in err
