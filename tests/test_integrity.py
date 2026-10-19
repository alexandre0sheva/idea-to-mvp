"""The test-integrity guard: deleted, renamed, skipped, and weakened tests are flagged until restored or justified."""

from pathlib import Path

import pytest

from idea_to_mvp.config import Settings
from idea_to_mvp.implementation import integrity
from idea_to_mvp.implementation.integrity import (
    CHANGES_FILE,
    COUNT_KEY,
    SessionWatch,
    blocking,
    compare,
    count_finding,
    failure_lines,
    file_facts,
    is_test_file,
    read_justifications,
    report_block,
    review,
    snapshot,
)

PY_TEST = """\
import pytest

from app import add


def test_add():
    total = add(2, 2)
    assert total == 4
    assert isinstance(total, int)


def test_add_negative():
    assert add(-1, 1) == 0
"""

JS_TEST = """\
import { sum } from "../src/sum";

describe("sum", () => {
  it("adds two numbers", () => {
    const total = sum(1, 2);
    expect(total).toBe(3);
    expect(typeof total).toBe("number");
  });

  it("adds zero", () => {
    expect(sum(0, 0)).toBe(0);
  });
});
"""


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def changed(tmp_path: Path, relative: str, before: str, after: str | None, **kwargs: object) -> list[dict]:
    """Findings of one session that turned `before` into `after` (None = deleted) at `relative`."""
    write(tmp_path, relative, before)
    first = snapshot(tmp_path)
    if after is None:
        (tmp_path / relative).unlink()
    else:
        write(tmp_path, relative, after)
    return [dict(f) for f in compare(first, snapshot(tmp_path), session="T02", iteration=1, **kwargs)]  # type: ignore[arg-type]


# ------------------------------------------------------------------ test files


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_api.py",
        "test_models.py",
        "app/models_test.py",
        "pkg/server_test.go",
        "web/src/app.test.ts",
        "web/src/app.spec.jsx",
        "web/__tests__/menu.js",
        "spec/models/user_spec.rb",
        "tests/conftest.py",
        "tests/helpers.ts",
    ],
)
def test_test_files_are_recognised_by_name_or_folder(path: str) -> None:
    assert is_test_file(path)


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "README.md", "tests/fixtures/data.json", "quality/TEST_CHANGES.md", "src/testing.py", "contest.py"],
)
def test_other_files_are_not_test_files(path: str) -> None:
    assert not is_test_file(path)


def test_the_snapshot_skips_dependency_folders_orchestrator_files_and_symlinks(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_a.py", PY_TEST)
    write(tmp_path, "node_modules/lib/test_x.py", PY_TEST)
    write(tmp_path, ".venv/lib/test_y.py", PY_TEST)
    write(tmp_path, ".idea-to-mvp/reports/test_z.py", PY_TEST)
    (tmp_path / "tests" / "test_link.py").symlink_to(tmp_path / "tests" / "test_a.py")
    assert sorted(snapshot(tmp_path)) == ["tests/test_a.py"]


# ------------------------------------------------------------------ what a session did


def test_deleting_a_test_file_is_flagged_with_what_it_held(tmp_path: Path) -> None:
    [finding] = changed(tmp_path, "tests/test_math.py", PY_TEST, None)
    assert (finding["kind"], finding["path"], finding["session"], finding["status"]) == ("deleted", "tests/test_math.py", "T02", "open")
    assert finding["before"] == 3 and "3 assertion" in finding["detail"]


def test_renaming_a_test_file_is_flagged_as_a_rename(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    before = snapshot(tmp_path)
    (tmp_path / "tests" / "test_math.py").rename(tmp_path / "tests" / "test_arith.py")
    [finding] = compare(before, snapshot(tmp_path), session="fix-1", iteration=1)
    assert finding["kind"] == "renamed" and "tests/test_arith.py" in finding["detail"]


def test_renaming_a_test_out_of_the_test_pattern_is_a_deletion(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    before = snapshot(tmp_path)
    (tmp_path / "tests" / "test_math.py").rename(tmp_path / "tests" / "math_disabled.txt")
    [finding] = compare(before, snapshot(tmp_path), session="T02", iteration=1)
    assert finding["kind"] == "deleted"


@pytest.mark.parametrize(
    ("relative", "before", "after"),
    [
        ("tests/test_math.py", PY_TEST, PY_TEST.replace("def test_add():", "@pytest.mark.skip\ndef test_add():")),
        ("tests/test_math.py", PY_TEST, PY_TEST.replace("def test_add():", "@pytest.mark.xfail(reason='flaky')\ndef test_add():")),
        ("tests/test_math.py", PY_TEST, PY_TEST.replace("    total = add(2, 2)", "    pytest.skip('later')\n    total = add(2, 2)")),
        ("tests/test_math.py", PY_TEST, PY_TEST.replace("def test_add():", "@unittest.skip('x')\ndef test_add():")),
        ("tests/test_math.py", PY_TEST, PY_TEST.replace("import pytest", "import pytest\npytestmark = pytest.mark.skip")),
        ("web/sum.test.ts", JS_TEST, JS_TEST.replace('it("adds zero"', 'it.skip("adds zero"')),
        ("web/sum.test.ts", JS_TEST, JS_TEST.replace('it("adds zero"', 'it.only("adds zero"')),
        ("web/sum.test.ts", JS_TEST, JS_TEST.replace('describe("sum"', 'describe.only("sum"')),
        ("web/sum.test.ts", JS_TEST, JS_TEST.replace('it("adds zero"', 'xit("adds zero"')),
        ("pkg/sum_test.go", "func TestSum(t *testing.T) {\n\tif Sum(1, 2) != 3 {\n\t\tt.Fatal(\"bad\")\n\t}\n}\n",
         "func TestSum(t *testing.T) {\n\tt.Skip(\"later\")\n\tif Sum(1, 2) != 3 {\n\t\tt.Fatal(\"bad\")\n\t}\n}\n"),
    ],
)  # fmt: skip
def test_new_skip_xfail_and_only_markers_are_flagged(tmp_path: Path, relative: str, before: str, after: str) -> None:
    findings = changed(tmp_path, relative, before, after)
    assert [f["kind"] for f in findings] == ["skip_added"]
    assert findings[0]["before"] == 0 and relative in findings[0]["detail"]


def test_a_skip_marker_in_a_comment_is_not_a_skip(tmp_path: Path) -> None:
    assert changed(tmp_path, "tests/test_math.py", PY_TEST, PY_TEST + "\n# @pytest.mark.skip would hide it\n") == []


def test_a_new_test_file_that_starts_skipped_is_flagged(tmp_path: Path) -> None:
    before = snapshot(tmp_path)
    write(tmp_path, "tests/test_new.py", "import pytest\n\n@pytest.mark.skip\ndef test_later():\n    assert False\n")
    assert [f["kind"] for f in compare(before, snapshot(tmp_path), session="T03", iteration=1)] == ["skip_added"]


def test_removing_an_assertion_from_an_otherwise_unchanged_test_is_flagged(tmp_path: Path) -> None:
    [finding] = changed(tmp_path, "tests/test_math.py", PY_TEST, PY_TEST.replace("    assert isinstance(total, int)\n", ""))
    assert (finding["kind"], finding["block"], finding["before"]) == ("assertions_removed", "test_add", 2)
    assert "test_add" in finding["detail"]


def test_commenting_an_assertion_out_removes_it(tmp_path: Path) -> None:
    after = PY_TEST.replace("    assert total == 4", "    # assert total == 4")
    assert [f["kind"] for f in changed(tmp_path, "tests/test_math.py", PY_TEST, after)] == ["assertions_removed"]


def test_removing_an_expectation_from_a_js_test_is_flagged(tmp_path: Path) -> None:
    after = JS_TEST.replace('    expect(typeof total).toBe("number");\n', "")
    [finding] = changed(tmp_path, "web/sum.test.ts", JS_TEST, after)
    assert (finding["kind"], finding["block"]) == ("assertions_removed", "adds two numbers")


def test_a_rewritten_test_with_fewer_assertions_is_not_flagged(tmp_path: Path) -> None:
    after = PY_TEST.replace("    total = add(2, 2)\n    assert total == 4\n    assert isinstance(total, int)\n", "    assert add(2, 2) == 4\n")
    assert changed(tmp_path, "tests/test_math.py", PY_TEST, after) == []


def test_adding_tests_and_assertions_is_never_flagged(tmp_path: Path) -> None:
    after = PY_TEST + "\n\ndef test_add_big():\n    assert add(10**9, 1) == 10**9 + 1\n"
    assert changed(tmp_path, "tests/test_math.py", PY_TEST, after) == []
    assert changed(tmp_path, "src/app.py", "x = 1\n", "x = 2\n") == []  # not a test file


def test_a_file_without_named_tests_is_judged_as_a_whole(tmp_path: Path) -> None:
    before = "#[test]\nfn sums() {\n    assert_eq!(sum(1, 2), 3);\n    assert!(sum(0, 0) == 0);\n}\n"
    after = before.replace("    assert!(sum(0, 0) == 0);\n", "")
    [finding] = changed(tmp_path, "tests/sum_test.rs", before, after)
    assert finding["kind"] == "assertions_removed" and finding["block"] == ""


def test_exempt_files_may_lose_assertions_but_not_disappear(tmp_path: Path) -> None:
    after = PY_TEST.replace("    assert isinstance(total, int)\n", "")
    assert changed(tmp_path, "tests/test_math.py", PY_TEST, after, exempt={"tests/test_math.py"}) == []
    assert [f["kind"] for f in changed(tmp_path, "tests/test_b.py", PY_TEST, None, exempt={"tests/test_b.py"})] == ["deleted"]


def test_finding_ids_are_stable_and_distinct_per_session(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    before = snapshot(tmp_path)
    (tmp_path / "tests" / "test_math.py").unlink()
    after = snapshot(tmp_path)
    first = compare(before, after, session="T02", iteration=1)[0]["id"]
    assert compare(before, after, session="T02", iteration=1)[0]["id"] == first
    assert compare(before, after, session="fix-1", iteration=1)[0]["id"] != first


# ------------------------------------------------------------------ the session watch


def settings(mode: str = "block") -> Settings:
    return Settings(_env_file=None, test_integrity=mode)  # type: ignore[call-arg]


def test_a_session_watch_reports_what_happened_between_start_and_end(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    watch = SessionWatch(tmp_path, settings(), session="T02", iteration=2)
    (tmp_path / "tests" / "test_math.py").unlink()
    [finding] = watch.findings()
    assert (finding["session"], finding["iteration"], finding["kind"]) == ("T02", 2, "deleted")


def test_with_the_guard_off_a_session_watch_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    monkeypatch.setattr(integrity, "snapshot", lambda root: pytest.fail("no snapshot when off"))
    watch = SessionWatch(tmp_path, settings("off"), session="T02", iteration=1)
    (tmp_path / "tests" / "test_math.py").unlink()
    assert watch.findings() == []


# ------------------------------------------------------------------ restore or justify


def test_justifications_are_list_items_naming_the_path(tmp_path: Path) -> None:
    write(
        tmp_path,
        CHANGES_FILE,
        "# Test changes\n\n- `tests/test_math.py`: the add() API moved to a calculator module.\n"
        "* test-count — two duplicated tests were merged into one parametrized test\n"
        "- tests/test_empty.py:\n"
        "Some prose naming tests/test_other.py: not a list item.\n",
    )
    assert read_justifications(tmp_path) == {
        "tests/test_math.py": "the add() API moved to a calculator module.",
        COUNT_KEY: "two duplicated tests were merged into one parametrized test",
    }
    assert read_justifications(tmp_path / "missing") == {}


def test_review_marks_open_findings_restored_justified_or_still_open(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_a.py", PY_TEST)
    write(tmp_path, "tests/test_b.py", PY_TEST)
    write(tmp_path, "tests/test_c.py", PY_TEST)
    before = snapshot(tmp_path)
    for name in ("a", "b", "c"):
        (tmp_path / "tests" / f"test_{name}.py").unlink()
    findings = {f["id"]: f for f in compare(before, snapshot(tmp_path), session="T02", iteration=1)}
    write(tmp_path, "tests/test_a.py", PY_TEST)  # restored
    write(tmp_path, "tests/test_c.py", "def test_c():\n    pass\n")  # back, but without its assertions
    write(tmp_path, CHANGES_FILE, "- tests/test_b.py: replaced by tests/test_api.py, which covers the same cases\n")
    reviewed = review(findings, tmp_path, test_count=None)
    status = {f["path"]: (f["status"], f["reason"]) for f in reviewed.values()}
    assert status == {  # test_c.py is back without its assertions: still open, so not in the updates
        "tests/test_a.py": ("restored", ""),
        "tests/test_b.py": ("justified", "replaced by tests/test_api.py, which covers the same cases"),
    }


def test_a_closed_finding_stays_closed(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_a.py", PY_TEST)
    before = snapshot(tmp_path)
    (tmp_path / "tests" / "test_a.py").unlink()
    [finding] = compare(before, snapshot(tmp_path), session="T02", iteration=1)
    closed = {finding["id"]: {**finding, "status": "restored"}}
    assert review(closed, tmp_path, test_count=None) == {}  # nothing to update


def test_skip_and_assertion_findings_are_restored_when_the_test_is_back(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    before = snapshot(tmp_path)
    write(tmp_path, "tests/test_math.py", PY_TEST.replace("def test_add():", "@pytest.mark.skip\ndef test_add():").replace("    assert isinstance(total, int)\n", ""))
    findings = {f["id"]: f for f in compare(before, snapshot(tmp_path), session="fix-1", iteration=1)}
    assert sorted(f["kind"] for f in findings.values()) == ["assertions_removed", "skip_added"]
    assert {f["status"] for f in review(findings, tmp_path, test_count=None).values()} == set()  # still open: unchanged
    write(tmp_path, "tests/test_math.py", PY_TEST)
    assert {f["status"] for f in review(findings, tmp_path, test_count=None).values()} == {"restored"}


def test_a_drop_in_the_collected_test_count_is_flagged_and_restored_by_a_higher_count(tmp_path: Path) -> None:
    assert count_finding(None, 12, session="lane-tests-1", iteration=1) is None
    assert count_finding(12, 14, session="lane-tests-2", iteration=1) is None
    finding = count_finding(12, 9, session="lane-tests-2", iteration=1)
    assert finding is not None
    assert (finding["kind"], finding["path"], finding["before"]) == ("count_dropped", COUNT_KEY, 12)
    assert "12" in finding["detail"] and "9" in finding["detail"]
    findings = {finding["id"]: finding}
    assert review(findings, tmp_path, test_count=None) == {}  # no report this round: no change
    assert review(findings, tmp_path, test_count=11) == {}
    assert review(findings, tmp_path, test_count=12)[finding["id"]]["status"] == "restored"
    write(tmp_path, CHANGES_FILE, f"- {COUNT_KEY}: four slow duplicates were removed on purpose\n")
    assert review(findings, tmp_path, test_count=9)[finding["id"]]["status"] == "justified"


# ------------------------------------------------------------------ the verdict


def test_only_open_findings_in_block_mode_block_the_verdict(tmp_path: Path) -> None:
    open_ = count_finding(5, 3, session="lane-tests-2", iteration=1)
    assert open_ is not None
    closed = {**open_, "id": "other", "status": "justified"}
    findings = {open_["id"]: open_, "other": closed}
    assert [f["id"] for f in blocking(findings, "block")] == [open_["id"]]
    assert blocking(findings, "warn") == [] and blocking(findings, "off") == []


def test_failure_lines_are_tagged_and_say_how_to_clear_them(tmp_path: Path) -> None:
    [finding] = changed(tmp_path, "tests/test_math.py", PY_TEST, None)
    [line] = failure_lines([finding])  # type: ignore[list-item]
    assert line.startswith("[integrity] ") and "tests/test_math.py" in line and CHANGES_FILE in line


def test_the_report_block_says_what_was_flagged_and_what_happened_to_it(tmp_path: Path) -> None:
    [finding] = changed(tmp_path, "tests/test_math.py", PY_TEST, None)
    justified = {**finding, "status": "justified", "reason": "moved to tests/test_api.py"}
    block = report_block({finding["id"]: justified}, "block")  # type: ignore[dict-item]
    assert "Test integrity" in block and "justified" in block and "moved to tests/test_api.py" in block
    assert "BLOCKED" in report_block({finding["id"]: finding}, "block")  # type: ignore[dict-item]
    assert "WARNING" in report_block({finding["id"]: finding}, "warn")  # type: ignore[dict-item]
    assert report_block({}, "block") == "" and report_block({finding["id"]: finding}, "off") == ""  # type: ignore[dict-item]


def test_file_facts_count_assertions_skips_and_tests() -> None:
    facts = file_facts(PY_TEST, "tests/test_math.py")
    assert (facts.assertions, facts.skips, sorted(facts.blocks)) == (3, 0, ["test_add", "test_add_negative"])


def test_a_test_whose_signature_spans_lines_is_still_one_test(tmp_path: Path) -> None:
    before = "def test_client(\n    client,\n):\n    assert client.get('/').ok\n    assert client.get('/x').ok\n"
    after = before.replace("    assert client.get('/x').ok\n", "")
    [finding] = changed(tmp_path, "tests/test_web.py", before, after)
    assert (finding["kind"], finding["block"]) == ("assertions_removed", "test_client")


def test_a_verdict_without_a_workspace_reads_no_files(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from idea_to_mvp.config import clear_settings_cache
    from idea_to_mvp.nodes.verify import verdict_node
    from idea_to_mvp.state import make_initial_state

    monkeypatch.setenv("TEST_INTEGRITY", "block")  # demo_env clears the settings cache again in its teardown
    clear_settings_cache()
    [open_finding] = compare({"tests/test_a.py": file_facts(PY_TEST, "tests/test_a.py")}, {}, session="T01", iteration=1)
    monkeypatch.setattr(integrity, "snapshot", lambda root: pytest.fail(f"read {root}"))
    state = {**make_initial_state("idea", 1), "integrity_findings": {open_finding["id"]: open_finding}}
    update = verdict_node(state)  # type: ignore[arg-type]
    assert update["verification"]["passed"] is False  # still open: nothing could restore it


# ------------------------------------------------------------------ review fixes


def test_a_justification_written_before_the_change_does_not_clear_it(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_math.py", PY_TEST)
    write(tmp_path, CHANGES_FILE, "- tests/test_math.py: an old reason, for an earlier skip\n")
    watch = SessionWatch(tmp_path, settings(), session="I2-01", iteration=2)
    (tmp_path / "tests" / "test_math.py").unlink()
    [finding] = watch.findings()
    assert finding["stale"] == "an old reason, for an earlier skip"
    assert review({finding["id"]: finding}, tmp_path, test_count=None) == {}  # the old line is not a justification
    write(tmp_path, CHANGES_FILE, "- tests/test_math.py: merged into tests/test_calc.py in I2-01\n")
    assert review({finding["id"]: finding}, tmp_path, test_count=None)[finding["id"]]["status"] == "justified"


def test_an_old_test_count_justification_does_not_clear_a_new_drop(tmp_path: Path) -> None:
    finding = count_finding(12, 9, session="v2-lane-tests-1", iteration=2, stale="duplicates were removed in v0.1")
    assert finding is not None and finding["stale"] == "duplicates were removed in v0.1"
    write(tmp_path, CHANGES_FILE, f"- {COUNT_KEY}: duplicates were removed in v0.1\n")
    assert review({finding["id"]: finding}, tmp_path, test_count=9) == {}


def test_deleting_a_file_in_a_test_folder_that_holds_no_tests_is_not_flagged(tmp_path: Path) -> None:
    assert changed(tmp_path, "tests/__init__.py", "", None) == []
    assert changed(tmp_path, "tests/conftest.py", "import pytest\n\n@pytest.fixture\ndef client():\n    return 1\n", None) == []
    assert [f["kind"] for f in changed(tmp_path, "tests/test_named.py", "def test_x():\n    pass\n", None)] == ["deleted"]


def test_the_snapshot_has_no_file_cap_so_new_files_never_push_old_ones_out(tmp_path: Path) -> None:
    write(tmp_path, "z/tests/test_last.py", PY_TEST)
    before = snapshot(tmp_path)
    for index in range(2100):
        write(tmp_path, f"a/tests/test_{index:04}.py", "def test_ok():\n    assert True\n")
    after = snapshot(tmp_path)
    assert "z/tests/test_last.py" in after and compare(before, after, session="T02", iteration=1) == []


def test_a_finding_is_valid_only_with_every_field_of_the_right_type(tmp_path: Path) -> None:
    [finding] = changed(tmp_path, "tests/test_math.py", PY_TEST, None)
    assert integrity.valid_finding(finding)
    assert not integrity.valid_finding({"id": "x"})
    assert not integrity.valid_finding({**finding, "status": "gone"})
    assert not integrity.valid_finding({**finding, "before": "3"})
