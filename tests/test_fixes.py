"""The contrastive fix loop's pieces: failure classes, the `<notes>` contract, and the attempt folders."""

from pathlib import Path

import pytest

from idea_to_mvp.implementation import fixes
from idea_to_mvp.implementation.fixes import Note

# ------------------------------------------------------------ failure classes


@pytest.mark.parametrize(
    "failure",
    [
        "[tests] ENVIRONMENT: the sandbox has no Postgres server, so the integration tests cannot connect",
        "[tests] pip install failed: Could not resolve host: pypi.org",
        "[quality] the smoke probe cannot reach api.example.com: the domain is blocked by the sandbox",
        "[quality] the app needs STRIPE_API_KEY and the credentials are not set",
        "[tests] Temporary failure in name resolution while fetching the dataset",
        "[tests] playwright cannot be installed in this sandbox",
    ],
)
def test_a_failure_the_project_cannot_fix_is_an_environment_failure(failure: str) -> None:
    assert fixes.classify_failures([failure]).environment == [failure]


@pytest.mark.parametrize(
    "failure",
    [
        "[tests] test_total: AssertionError: expected 3, got 4",
        "[tests] ModuleNotFoundError: No module named 'flask'",
        "[quality] ruff: command not found",  # the project may need to declare the tool: a fix can do that
        "[requirements] R2 (P0): no test covers exporting to CSV",
        "[tests] test_login: password is required",
        "[integrity] tests/test_core.py: a skip marker was added",
        # a failing test's own name and message, in product words, is never the lane's diagnosis
        "[acceptance] test_R2_rejects_anonymous: R2: requests with missing credentials must be rejected",
        "[tests] tests.test_api::test_login_without_token: the access token is not set, so expected a 401",
        "[tests] test_offline_mode: the network is blocked, so the app must show the cached list",
    ],
)
def test_a_failure_a_code_change_can_fix_stays_a_code_failure(failure: str) -> None:
    classified = fixes.classify_failures([failure])
    assert classified.environment == [] and classified.code == [failure]


def test_a_test_failure_counts_as_environmental_only_with_the_explicit_mark() -> None:
    assert fixes.classify_failures(["[acceptance] ENVIRONMENT: the suite needs docker"]).only_environment is True


def test_the_loop_stops_early_only_when_every_failure_is_an_environment_failure() -> None:
    env = "[tests] ENVIRONMENT: no network, pypi.org is blocked"
    code = "[tests] test_total: AssertionError"
    assert fixes.classify_failures([env]).only_environment is True
    mixed = fixes.classify_failures([env, code])
    assert mixed.only_environment is False and mixed.environment == [env] and mixed.code == [code]
    assert fixes.classify_failures([]).only_environment is False  # nothing listed is not an environment problem


def test_the_reason_names_the_environment_failures_without_their_lane_tags() -> None:
    reason = fixes.classify_failures(["[tests] ENVIRONMENT: pypi.org is blocked", "[quality] ENVIRONMENT: no docker"]).reason
    assert "pypi.org is blocked" in reason and "no docker" in reason and "[tests]" not in reason


# ------------------------------------------------------------------ the notes


def test_notes_are_read_from_the_notes_block_one_line_per_hypothesis() -> None:
    text = (
        "Changed the parser.\n\n<notes>\n"
        "kept -> the date parser drops the year -> trajectory: the second pytest run still fails on 2024-02-29\n"
        "- dropped -> the fixture is stale -> fix-1 trajectory, ACTION 4: the fixture was regenerated\n"
        "new -> leap days are rejected by the validator -> report: test_leap fails in validate()\n"
        "</notes>"
    )
    assert fixes.parse_notes(text) == [
        Note("kept", "the date parser drops the year", "trajectory: the second pytest run still fails on 2024-02-29"),
        Note("dropped", "the fixture is stale", "fix-1 trajectory, ACTION 4: the fixture was regenerated"),
        Note("new", "leap days are rejected by the validator", "report: test_leap fails in validate()"),
    ]


def test_lines_that_are_not_in_the_shape_are_ignored_and_no_block_means_no_notes() -> None:
    assert fixes.parse_notes("<notes>\nI think it is the parser\nmaybe -> x -> y\nrevised -> only a hypothesis\n</notes>") == []
    assert fixes.parse_notes("no block here") == []
    assert fixes.parse_notes("") == []


def test_an_environment_answer_is_read_from_its_tag() -> None:
    assert fixes.parse_environment("Nothing to change.\n<environment>\nThe sandbox has no docker.\n</environment>") == "The sandbox has no docker."
    assert fixes.parse_environment("I fixed it.") == ""


def test_a_later_attempt_must_judge_what_came_before_and_bring_a_hypothesis() -> None:
    earlier = [Note("new", "the parser drops the year", "report")]
    good = [Note("dropped", "the parser drops the year", "fix-1 run"), Note("new", "the validator rejects leap days", "report")]
    assert fixes.notes_problem(good, attempt=2, earlier=earlier) == ""
    assert "no <notes>" in fixes.notes_problem([], attempt=2, earlier=earlier)
    assert "earlier hypothesis" in fixes.notes_problem([Note("new", "x", "y")], attempt=2, earlier=earlier)
    assert "new hypothesis" in fixes.notes_problem([Note("kept", "the parser drops the year", "r")], attempt=2, earlier=earlier)
    assert fixes.notes_problem([Note("new", "x", "y")], attempt=1, earlier=[]) == ""
    assert "no <notes>" in fixes.notes_problem([], attempt=1, earlier=[])
    # nothing earlier to judge when the previous attempt left no notes
    assert fixes.notes_problem([Note("new", "x", "y")], attempt=2, earlier=[]) == ""


# ------------------------------------------------------------ attempt folders


def test_an_attempt_folder_holds_the_failures_the_diff_and_the_trajectory(tmp_path: Path) -> None:
    trajectory = tmp_path / "t.md"
    trajectory.write_text("[ACTION] Bash {}\n")
    folder = fixes.write_attempt(
        tmp_path, iteration=1, attempt=1, failures=["[tests] a failed"], diff="diff --git a/x b/x\n", trajectory=trajectory
    )
    assert folder == tmp_path / ".idea-to-mvp" / "fix" / "attempt-1"
    assert "- [tests] a failed" in (folder / "failures.md").read_text()
    assert (folder / "diff.patch").read_text() == "diff --git a/x b/x\n"
    assert (folder / "trajectory.md").read_text() == "[ACTION] Bash {}\n"


def test_a_later_version_keeps_its_attempts_apart() -> None:
    assert fixes.attempt_name(1, 2) == "attempt-2" and fixes.attempt_name(3, 1) == "v3-attempt-1"


def test_an_attempt_without_changes_or_a_trajectory_still_gets_a_folder(tmp_path: Path) -> None:
    folder = fixes.write_attempt(tmp_path, iteration=1, attempt=2, failures=["x"], diff="", trajectory=None)
    assert "no changes" in (folder / "diff.patch").read_text().lower()
    assert not (folder / "trajectory.md").exists()


def test_the_lane_reports_that_followed_an_attempt_are_added_when_the_next_round_is_judged(tmp_path: Path) -> None:
    fixes.write_attempt(tmp_path, iteration=1, attempt=1, failures=["x"], diff="", trajectory=None)
    fixes.write_after(tmp_path, iteration=1, attempt=1, passed=False, report="### Tests lane: FAILED")
    after = (tmp_path / ".idea-to-mvp" / "fix" / "attempt-1" / "after.md").read_text()
    assert "still failing" in after.lower() and "### Tests lane: FAILED" in after


def test_the_evidence_block_names_every_earlier_attempt_folder_and_asks_for_notes(tmp_path: Path) -> None:
    for attempt in (1, 2):
        fixes.write_attempt(tmp_path, iteration=1, attempt=attempt, failures=["x"], diff="", trajectory=None)
    block = fixes.evidence_block(tmp_path, iteration=1, attempt=3)
    assert ".idea-to-mvp/fix/attempt-1/" in block and ".idea-to-mvp/fix/attempt-2/" in block
    assert "attempt-3" not in block
    assert "kept | revised | dropped" in block and "<notes>" in block
    assert fixes.evidence_block(tmp_path, iteration=1, attempt=1) == ""  # nothing earlier to read
    assert fixes.evidence_block(tmp_path, iteration=2, attempt=2) == ""  # another version's attempts are not this one's


# --------------------------------------------------------------------- history


def test_the_history_entry_of_an_attempt_is_closed_by_the_next_verdict() -> None:
    entry = fixes.history_entry(2, failures=["a", "b"], summary="Changed x.", files=["x.py"], notes=[Note("new", "h", "e")], model="m")
    assert entry["result"] == "" and entry["files"] == ["x.py"] and entry["notes"] == [{"verdict": "new", "hypothesis": "h", "evidence": "e"}]
    closed = fixes.close_entry(entry, passed=False, remaining=1)
    assert closed["result"] == "failed" and closed["remaining"] == 1 and entry["result"] == ""
    assert fixes.close_entry(entry, passed=True, remaining=0)["result"] == "passed"


def test_files_changed_are_read_from_the_diff_headers() -> None:
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n+x\ndiff --git a/new.md b/new.md\n"
    assert fixes.files_in(diff) == ["src/a.py", "new.md"]
    assert fixes.files_in("") == []
