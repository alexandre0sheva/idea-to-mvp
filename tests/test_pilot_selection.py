"""Blind selection of the pilot build (`pilot_selection.py`) and the Behavioral contrast card (`ui/render.py`)."""

from __future__ import annotations

from typing import Any

from plan_helpers import make_task

from idea_to_mvp.harness_tuning import ProbeResult
from idea_to_mvp.pilot_selection import (
    BuildScore,
    Selection,
    anonymize,
    build_section,
    candidates,
    overrule,
    selection_problems,
    selection_prompt,
)
from idea_to_mvp.ui.render import tuning_card
from idea_to_mvp.ui.view import tuning_markdown

WORKTREE = "/projects/.worktrees/climb-log/pilot-dom"


def probe(
    arm: str, delivered: bool = True, passed: bool | None = True, **extra: Any
) -> ProbeResult:
    result: ProbeResult = {
        "arm": arm,  # type: ignore[typeddict-item]
        "outcome": "success" if delivered else "failed",
        "delivered": delivered,
        "summary": f"I followed the {arm} harness's new skill",
        "cost_usd": 0.5,
        "turns": 10,
        "commit": "abc1234" if delivered else None,
        "changed": " app.py | 3 +++" if delivered else "",
        "tests": {
            "ran": passed is not None,
            "passed": passed,
            "failures": [],
            "summary": "",
            "evidence": {"tests": 3},
        },
        "trajectory": f".idea-to-mvp/trajectories/pilot-{arm}.md",
    }
    result.update(extra)  # type: ignore[typeddict-item]
    return result


def score(build: str, delivery: int = 4, finish: int = 3, **marks: int) -> dict[str, Any]:
    fields = {"conformance": 4, "fidelity": 4, "consistency": 4, "operational_fitness": 4, **marks}
    return BuildScore(
        build=build, delivery=delivery, finish=finish, notes="n", **fields
    ).model_dump()


def test_only_builds_that_delivered_are_candidates_and_a_seed_fixes_their_names() -> None:
    probes = {
        "base": probe("base"),
        "inv": probe("inv", delivered=False, passed=None),
        "dom": probe("dom"),
    }
    assert candidates(probes) == ["base", "dom"]
    arms: list[Any] = ["base", "inv", "dom"]
    assert anonymize(arms, "seed") == anonymize(arms, "seed")  # a retried step shows the same names
    assert sorted(anonymize(arms, "seed")) == ["A", "B", "C"] and sorted(
        anonymize(arms, "seed").values()
    ) == sorted(arms)
    orders = {tuple(anonymize(arms, f"seed-{n}").values()) for n in range(20)}
    assert len(orders) > 1  # shuffled, not always base first


def test_the_prompt_shows_diffs_and_tests_but_never_which_harness_made_a_build() -> None:
    failing = probe(
        "dom",
        passed=False,
        tests={
            "ran": True,
            "passed": False,
            "failures": [f"{WORKTREE}/tests/test_app.py::test_log failed"],
            "summary": "",
            "evidence": None,
        },
    )
    sections = {
        "A": build_section("A", failing, f"diff --git a/app.py b/app.py\n+ROOT = '{WORKTREE}'\n"),
        "B": build_section(
            "B",
            probe("base", integrity=[{"kind": "deleted", "path": "tests/test_old.py"}]),
            "+print('pilot-base')\n",
        ),
    }
    prompt = selection_prompt(
        make_task("T01", title="Log a climb"),
        "# PRD\n\n- R1 (P0): log a climb\n",
        sections,
        [WORKTREE],
    )
    for leak in (
        WORKTREE,
        "pilot-dom",
        "pilot-base",
        "harness",
        "INV",
        "DOM",
        "trajectory",
        "$0.50",
        "10 turns",
    ):
        assert leak not in prompt, leak
    assert "### Build A" in prompt and "### Build B" in prompt and "[hidden]" in prompt
    assert (
        "Tests lane: failed" in prompt
        and "test_log failed" in prompt
        and "R1 (P0): log a climb" in prompt
    )
    assert (
        "deleted: tests/test_old.py" in prompt and "no test was deleted" in prompt
    )  # what each session did to the tests


def test_an_answer_must_choose_one_of_the_builds_and_score_each_exactly_once() -> None:
    good = Selection(
        choice="B",
        scores=[BuildScore.model_validate(score("A")), BuildScore.model_validate(score("B"))],
        rationale="r",
    )
    assert selection_problems(good, ["A", "B"]) == []
    assert selection_problems(good.model_copy(update={"choice": "none"}), ["A", "B"]) == []
    unknown = good.model_copy(update={"choice": "C"})
    assert "not one of the builds" in selection_problems(unknown, ["A", "B"])[0]
    short = good.model_copy(update={"scores": good.scores[:1]})
    assert selection_problems(short, ["A", "B"]) == [
        "build B is scored 0 times (exactly once expected)"
    ]


def test_a_build_whose_tests_failed_gives_way_to_the_best_scored_clean_build() -> None:
    builds: dict[str, Any] = {"A": "base", "B": "inv", "C": "dom"}
    probes = {"base": probe("base", passed=False), "inv": probe("inv"), "dom": probe("dom")}
    # C is better on delivery, the first criterion, although B has the higher total.
    scores = {
        "A": score("A", delivery=5),
        "B": score("B", delivery=3, finish=5, consistency=5),
        "C": score("C", delivery=4, finish=1),
    }
    choice, reason = overrule("A", builds, probes, scores)
    assert choice == "C" and "its tests did not pass" in reason
    assert overrule("B", builds, probes, scores) == ("B", "")  # a clean choice stands
    assert overrule("none", builds, probes, scores) == ("none", "")  # so does choosing none


def test_a_build_whose_session_weakened_the_tests_is_not_chosen_over_a_clean_one() -> None:
    builds: dict[str, Any] = {"A": "base", "B": "dom"}
    probes = {
        "base": probe("base", integrity=[{"kind": "skipped", "path": "tests/test_app.py"}]),
        "dom": probe("dom"),
    }
    choice, reason = overrule("A", builds, probes, {"A": score("A"), "B": score("B")})
    assert choice == "B" and "weakened the tests" in reason
    probes["dom"] = probe(
        "dom", passed=False
    )  # passing tests come first: the weakened but passing build stands
    assert overrule("A", builds, probes, {})[0] == "A"


def tuning_record(**selection: Any) -> dict[str, Any]:
    edit = {
        "summary": "Start with the checks",
        "evidence": ["[ACTION] <Bash> ran the tests late"],
        "files": [{"path": ".claude/skills/start-a-task/SKILL.md", "content": "x"}],
    }
    return {
        "status": "done",
        "pilot": {"id": "T01", "title": "Project setup"},
        "adopted": "adopt_inv",
        "commit": "abc1234",
        "base": probe("base"),
        "inv": {**edit, "probe": probe("inv")},
        "dom": {
            "summary": "",
            "inherited": True,
            "files": [],
            "probe": probe("dom", delivered=False, passed=None),
        },
        "compose": {
            "notes": [
                {
                    "verdict": "kept",
                    "edit": "INV: start-a-task",
                    "evidence": "the INV run tested first",
                }
            ],
            "rationale": "INV avoided a detour.",
            "proposed": "adopt_inv",
            "overruled": "",
        },
        "selection": {
            "status": "chosen",
            "builds": {"A": "inv", "B": "base"},
            "proposed": "A",
            "choice": "A",
            "arm": "inv",
            "overruled": "",
            "scores": [score("A"), score("B", delivery=3)],
            "rationale": "A covers both criteria.",
            "called": True,
            "note": "",
            "landed": True,
            "task_commit": "def5678",
            **selection,
        },
        "ledger": {"ok": True, "problems": [], "rows": 9, "cost_usd": 2.0},
    }


def test_the_card_shows_the_edits_their_evidence_the_harness_the_build_and_the_spend() -> None:
    card = tuning_card(tuning_record())
    assert card.startswith("<details") and "Behavioral contrast · pilot T01 (Project setup)" in card
    assert "adopted the INV edit" in card and "abc1234" in card
    assert (
        "Build A of 2 (built under the INV edit) was selected as T01&#x27;s result" in card
        and "✓ selected" in card
    )
    assert (
        "&lt;Bash&gt; ran the tests late" in card and "<Bash>" not in card
    )  # agent text is escaped
    assert (
        "no usable edit" in card
        and "the INV run tested first" in card
        and "A = the INV edit" in card
    )
    assert (
        "rubric order: delivery, conformance, fidelity, consistency, operational fitness, finish"
        in card
    )
    assert (
        "<td>4 / 4 / 4 / 4 / 4 / 3</td>" in card
        and "$2.00 (pilot builds and their tests lanes $1.50, model calls $0.50)" in card
    )
    assert tuning_card({"status": "skipped", "note": "Skipped: no budget."}) == ""


def test_the_card_and_the_chat_text_say_when_no_build_is_used() -> None:
    record = tuning_record(
        landed=False,
        note="the selected build could not be applied (conflicts in: AGENTS.md), so T01 is built again",
    )
    assert "could not be applied" in tuning_card(record) and "✓ selected" not in tuning_card(record)
    none = tuning_record(
        status="none", choice="", arm="", rationale="Both builds log to the wrong table."
    )
    text = tuning_markdown(none)
    assert (
        "No pilot build was selected (Both builds log to the wrong table.); T01 is built again."
        in text
    )
    skipped = tuning_record(status="skipped", note="no pilot run delivered a build")
    assert "No pilot build was selected: no pilot run delivered a build." in tuning_markdown(
        skipped
    )
    overruled = tuning_record(
        proposed="B", overruled="build B was chosen but its tests did not pass"
    )
    assert (
        "The selector chose build B, overruled: build B was chosen but its tests did not pass."
        in tuning_markdown(overruled)
    )
