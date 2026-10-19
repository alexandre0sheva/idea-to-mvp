"""Test-integrity guard: agents may not make verification pass by weakening the tests.

The agents that write the code also write and run its tests, so the easiest way to a green run is to delete
the failing test, skip it, or drop the assertion that fails. The orchestrator watches for that itself. It
snapshots the project's test files before every task, merge-resolver, and fix session and compares them
afterwards (`SessionWatch`); reading files never runs agent code. A session is flagged for:

- a test file it **deleted**, or **renamed** (a rename can move a test out of what the runner collects);
- **new skip markers**: `skip` / `skipif` / `xfail`, `.only` / `.skip` / `xit`, `t.Skip`, `#[ignore]`, ...;
- **removed assertions** in a test whose other lines it left unchanged (a rewritten test is not flagged);

and, after each tests lane with a test report (Task 8), a **drop in the collected test count** against the
previous round (`count_finding`). A finding stays open until the test is back (`review`: the file exists
again with as many assertions, the markers are gone, the count is back) or the change is justified with a
list item `- <path>: <why>` in `quality/TEST_CHANGES.md` that was not already there before the change (an old
line about the same file does not justify a new change), which the dashboard shows the user. With
`TEST_INTEGRITY=block` (default) an open finding fails the verdict as an `[integrity]` failure; `warn` only
reports it; `off` reads nothing.

Findings live in the checkpointed state, out of the agents' reach. What the guard does not catch (an
assertion made trivially true, a test rewritten to assert less, test code that lies) is in `SECURITY.md`.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, TypedDict

from idea_to_mvp.implementation.workspace import GENERATED_DIRS, IGNORED_DIRS
from idea_to_mvp.text_utils import clip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings

IntegrityMode = Literal["block", "warn", "off"]
FindingKind = Literal["deleted", "renamed", "skip_added", "assertions_removed", "count_dropped"]
FindingStatus = Literal["open", "restored", "justified"]

CHANGES_FILE = "quality/TEST_CHANGES.md"
COUNT_KEY = "test-count"  # what a justification of a test-count drop names instead of a path

MAX_FILE_BYTES = 1_000_000  # a larger file is judged by its first megabyte
_MAX_CHANGES_BYTES = 200_000
_REASON_LIMIT = 300
_PATH_LIMIT = 200

_SKIPPED_DIRS = frozenset({*IGNORED_DIRS, *GENERATED_DIRS, "vendor"})  # vendored code is not the project's tests
_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "e2e", "integration_tests"})
_CODE_SUFFIXES = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".rb", ".rs", ".java",
     ".kt", ".kts", ".scala", ".cs", ".php", ".swift", ".dart", ".ex", ".exs", ".c", ".cc", ".cpp"}
)  # fmt: skip
_JS_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"})

_ASSERTION = re.compile(
    r"^\s*assert\b"  # Python's assert statement
    r"|(?<!\w)assert\w*!?\s*\("  # assert(, assertEqual(, self.assertTrue(, assert_eq!(, assertThat(
    r"|\b(?:assert|require|expect)\.\w+"  # node's assert.equal, testify's require.NoError, chai's expect.fail
    r"|\bexpect\s*\("  # jest, vitest, rspec
    r"|\.should\b"
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\("  # Go
    r"|\bpytest\.raises\s*\("
)
_SKIP = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail|importorskip)\s*\("
    r"|@(?:unittest\.)?(?:skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\bskipTest\s*\("
    r"|\braise\s+(?:unittest\.)?SkipTest\b"
    r"|(?<![\w.])(?:it|test|describe|context|suite|specify)\.(?:skip|only|todo|fixme)\b"
    r"|(?<![\w.])(?:xit|xtest|xdescribe|xcontext|xspecify|fit|fdescribe|ftest)\s*\("
    r"|\bt\.Skip(?:f|Now)?\s*\("  # Go
    r"|#\[ignore\b"  # Rust
    r"|@(?:Disabled|Ignore)\b"  # JUnit
    r"|\bSkip\s*=\s*\""  # xUnit
    r"|^\s*(?:skip|pending)(?:\s+[\"']|\s*\(|\s*$)"  # RSpec / minitest
)
_PY_TEST = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
_JS_TEST = re.compile(r"^\s*(?:it|test|specify)(?:\.\w+)*\s*\(\s*(['\"`])(.+?)\1")
_GO_TEST = re.compile(r"^func\s+(Test\w*)\s*\(")
_JUSTIFICATION = re.compile(r"^\s*[-*]\s+(`?)(?P<key>[^`\s:]+)\1\s*(?::|\s[—–-])\s*(?P<reason>\S.*?)\s*$")


class IntegrityFinding(TypedDict):
    """One way a session weakened the tests, and what has happened to it since."""

    id: str  # stable: the same change seen twice is one finding
    session: str  # the trajectory label of the session that made the change (T02, T03-resolver, fix-1, lane-tests-2)
    iteration: int
    kind: FindingKind
    path: str  # the test file (or COUNT_KEY): what a justification names
    block: str  # the test that lost assertions ('' when the file has no named tests, or for other kinds)
    before: int  # what restores it: the assertions it had, the skip markers it had, the tests that were collected
    detail: str  # one line for the user and the fix session
    status: FindingStatus
    reason: str  # the justification from TEST_CHANGES.md, once justified
    stale: str  # what TEST_CHANGES.md already said about `path` before the change: not a justification of it


_FIELD_TYPES: dict[str, type] = {
    "id": str, "session": str, "iteration": int, "kind": str, "path": str, "block": str, "before": int,
    "detail": str, "status": str, "reason": str, "stale": str,
}  # fmt: skip
_KINDS = ("deleted", "renamed", "skip_added", "assertions_removed", "count_dropped")
_STATUSES = ("open", "restored", "justified")


def valid_finding(entry: object) -> bool:
    """Whether `entry` (read back from a file an agent could have edited) is a well-formed finding."""
    return (
        isinstance(entry, dict)
        and all(type(entry.get(key)) is kind for key, kind in _FIELD_TYPES.items())
        and entry["kind"] in _KINDS
        and entry["status"] in _STATUSES
    )


@dataclass(frozen=True)
class BlockFacts:
    assertions: int
    others: str  # digest of the test's other lines, stripped (blank lines and comments left out)


@dataclass(frozen=True)
class FileFacts:
    digest: str
    assertions: int
    skips: int
    blocks: dict[str, BlockFacts] = field(default_factory=dict)  # test name -> facts; '' = the whole file

    @property
    def holds_tests(self) -> bool:
        """Whether the file has a named test, an assertion, or a skip marker (an `__init__.py`, a fixtures-only
        `conftest.py`, or a helper without assertions holds none: deleting one is not deleting a test)."""
        return bool(self.assertions or self.skips or any(self.blocks.keys() - {""}))


Snapshot = dict[str, FileFacts]  # posix path relative to the root -> facts


# ------------------------------------------------------------------ reading test files


def is_test_file(relative: str | PurePosixPath) -> bool:
    """A source file named like a test (`test_x.py`, `x_test.go`, `x.test.ts`, `XTest.java`, ...) or kept in a
    test folder (`tests/`, `__tests__/`, `spec/`, ...). Data files in those folders are not tests."""
    path = PurePosixPath(relative)
    if path.suffix not in _CODE_SUFFIXES:
        return False
    stem = path.stem
    named = (
        stem.startswith("test_")
        or stem in ("test", "tests")
        or stem.endswith(("_test", "_spec", ".test", ".spec", "Test", "Tests"))
        or (stem.startswith("Test") and len(stem) > 4 and stem[4].isupper())
    )
    return named or any(part in _TEST_DIRS for part in path.parts[:-1])


def _is_comment(stripped: str) -> bool:
    if stripped.startswith(("#[", "#![")):  # a Rust attribute, not a comment
        return False
    return stripped.startswith(("#", "//", "/*", "*", "<!--"))


def _block_ranges(lines: list[str], suffix: str) -> list[tuple[str, int, int]]:
    """(test name, first line, end line) of every named test in the file; [] when none can be found."""
    ranges: list[tuple[str, int, int]] = []
    if suffix == ".py":
        index = 0
        while index < len(lines):
            match = _PY_TEST.match(lines[index])
            if not match:
                index += 1
                continue
            indent, end = len(match.group(1).expandtabs()), index + 1
            while end < len(lines) and (
                not lines[end].strip()
                or lines[end].lstrip().startswith((")", "]"))  # the end of a signature split over lines
                or len(lines[end].expandtabs()) - len(lines[end].expandtabs().lstrip()) > indent
            ):
                end += 1
            ranges.append((match.group(2), index, end))
            index = end
        return ranges
    pattern = _GO_TEST if suffix == ".go" else _JS_TEST if suffix in _JS_SUFFIXES else None
    if pattern is None:
        return []
    starts = [(index, match.group(match.lastindex or 1)) for index, line in enumerate(lines) if (match := pattern.match(line))]
    return [(name, start, starts[n + 1][0] if n + 1 < len(starts) else len(lines)) for n, (start, name) in enumerate(starts)]


def file_facts(text: str, relative: str) -> FileFacts:
    """What a test file holds: its assertions, its skip markers, and per named test its assertions and other lines."""
    lines = text.splitlines()
    kinds: list[str] = []  # per line: "assert", "other", or "" (blank or comment)
    assertions = skips = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or _is_comment(stripped):
            kinds.append("")
            continue
        skips += bool(_SKIP.search(line))
        if _ASSERTION.search(line):
            assertions += 1
            kinds.append("assert")
        else:
            kinds.append("other")
    ranges = _block_ranges(lines, PurePosixPath(relative).suffix) or [("", 0, len(lines))]
    blocks: dict[str, BlockFacts] = {}
    for name, start, end in ranges:
        key = name
        while key in blocks:  # two tests with the same name (in different describe blocks)
            key += "#"
        blocks[key] = BlockFacts(
            assertions=sum(1 for kind in kinds[start:end] if kind == "assert"),
            others=_digest("\n".join(lines[i].strip() for i in range(start, end) if kinds[i] == "other")),
        )
    return FileFacts(digest=_digest(text), assertions=assertions, skips=skips, blocks=blocks)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(root: Path) -> Snapshot:
    """The facts of every test file under `root` (dependency, build, and orchestrator folders and symbolic
    links are left out). A file read only: nothing of the project runs. There is no cap on the number of files:
    a capped walk would let new files push old ones out (and look deleted), and leave the rest unwatched."""
    root = Path(root)
    facts: Snapshot = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in _SKIPPED_DIRS)
        for name in sorted(files):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not is_test_file(relative):
                continue
            try:
                with path.open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES)
            except OSError:
                continue
            facts[relative] = file_facts(raw.decode("utf-8", errors="replace"), relative)
    return facts


# ------------------------------------------------------------------ comparing


def _finding(
    *, session: str, iteration: int, kind: FindingKind, path: str, before: int, detail: str, block: str = ""
) -> IntegrityFinding:
    return {
        "id": f"v{iteration}:{session}:{kind}:{path}:{block}",
        "session": session,
        "iteration": iteration,
        "kind": kind,
        "path": path,
        "block": block,
        "before": before,
        "detail": detail,
        "status": "open",
        "reason": "",
        "stale": "",
    }


def compare(
    before: Mapping[str, FileFacts],
    after: Mapping[str, FileFacts],
    *,
    session: str,
    iteration: int,
    exempt: Collection[str] = (),
) -> list[IntegrityFinding]:
    """What one session did to the tests, from its before and after snapshots. Files in `exempt` (the
    conflicted files a merge resolver had to pick a side in) may lose assertions, but not disappear or gain
    skip markers."""
    findings: list[IntegrityFinding] = []
    added = {path: facts.digest for path, facts in after.items() if path not in before}
    for path, old in before.items():
        shown = clip(path, _PATH_LIMIT)
        new = after.get(path)
        if new is None:
            if not old.holds_tests:
                continue
            moved = next((other for other, digest in added.items() if digest == old.digest), None)
            what = f"renamed the test file {shown} to {clip(moved, _PATH_LIMIT)}" if moved else f"deleted the test file {shown}"
            findings.append(
                _finding(
                    session=session,
                    iteration=iteration,
                    kind="renamed" if moved else "deleted",
                    path=path,
                    before=old.assertions,
                    detail=f"{session} {what} ({old.assertions} assertion(s)).",
                )
            )
            continue
        if new.skips > old.skips:
            findings.append(_skip_finding(session, iteration, path, old.skips, new.skips))
        if path in exempt or new.digest == old.digest:
            continue
        for name, block in old.blocks.items():
            now = new.blocks.get(name)
            if now is not None and now.assertions < block.assertions and now.others == block.others:
                where = f"test {clip(name, 80)} in {shown}" if name else shown
                findings.append(
                    _finding(
                        session=session,
                        iteration=iteration,
                        kind="assertions_removed",
                        path=path,
                        block=name,
                        before=block.assertions,
                        detail=(
                            f"{session} removed {block.assertions - now.assertions} assertion(s) from {where} "
                            "and left the rest of it unchanged."
                        ),
                    )
                )
    for path in added:
        if after[path].skips:
            findings.append(_skip_finding(session, iteration, path, 0, after[path].skips))
    return findings


def _skip_finding(session: str, iteration: int, path: str, old: int, new: int) -> IntegrityFinding:
    return _finding(
        session=session,
        iteration=iteration,
        kind="skip_added",
        path=path,
        before=old,
        detail=f"{session} added {new - old} skip / xfail / only marker(s) to {clip(path, _PATH_LIMIT)}.",
    )


def count_finding(
    previous: int | None, current: int, *, session: str, iteration: int, stale: str = ""
) -> IntegrityFinding | None:
    """A drop in the number of tests the test report lists, against the previous round (None when it did not drop).
    `stale` is what TEST_CHANGES.md already says about the count, which cannot justify this drop."""
    if previous is None or current >= previous:
        return None
    finding = _finding(
        session=session,
        iteration=iteration,
        kind="count_dropped",
        path=COUNT_KEY,
        before=previous,
        detail=f"The test report lists {current} test(s), {previous - current} fewer than the {previous} of the previous round.",
    )
    finding["stale"] = stale
    return finding


class SessionWatch:
    """Snapshot the test files under `root` when a session starts; `findings()` once it has ended."""

    def __init__(self, root: Path, settings: Settings, *, session: str, iteration: int) -> None:
        self._root = Path(root)
        self._session = session
        self._iteration = iteration
        enabled = settings.test_integrity != "off"
        self._before: Snapshot | None = snapshot(self._root) if enabled else None
        self._known = read_justifications(self._root) if enabled else {}  # cannot justify what this session does

    def findings(self, *, session: str | None = None, exempt: Collection[str] = ()) -> list[IntegrityFinding]:
        """`session` names the session if its label was only known once it ran (a trajectory label like `T01-2`)."""
        if self._before is None:
            return []
        found = compare(
            self._before, snapshot(self._root), session=session or self._session, iteration=self._iteration, exempt=exempt
        )
        for finding in found:
            finding["stale"] = self._known.get(finding["path"], "")
        return found


# ------------------------------------------------------------------ restore or justify


def read_justifications(workspace: Path) -> dict[str, str]:
    """`quality/TEST_CHANGES.md` as {path or COUNT_KEY: reason}: one list item `- <path>: <why>` per change."""
    try:
        with (Path(workspace) / CHANGES_FILE).open("rb") as stream:
            text = stream.read(_MAX_CHANGES_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}
    reasons: dict[str, str] = {}
    for line in text.splitlines():
        if match := _JUSTIFICATION.match(line):
            reasons[match.group("key")] = clip(match.group("reason"), _REASON_LIMIT)
    return reasons


def _restored(finding: IntegrityFinding, current: Mapping[str, FileFacts], test_count: int | None) -> bool:
    kind, before = finding["kind"], finding["before"]
    if kind == "count_dropped":
        return test_count is not None and test_count >= before
    facts = current.get(finding["path"])
    if kind == "skip_added":
        return facts is None or facts.skips <= before  # a file deleted since is a finding of its own
    if facts is None:
        return False
    if kind == "assertions_removed":
        block = facts.blocks.get(finding["block"])
        return block is not None and block.assertions >= before
    return facts.assertions >= before  # deleted or renamed: the file is back with what it held


def review(
    findings: Mapping[str, IntegrityFinding], workspace: Path, *, test_count: int | None
) -> dict[str, IntegrityFinding]:
    """Open findings that are now restored or justified, with their new status (closed findings stay closed).
    `test_count` is this round's collected test count, None when there is no test report."""
    still_open = [finding for finding in findings.values() if finding["status"] == "open"]
    if not still_open:
        return {}
    needs_files = any(finding["kind"] != "count_dropped" for finding in still_open)
    current = snapshot(Path(workspace)) if needs_files else {}
    reasons = read_justifications(workspace)
    updates: dict[str, IntegrityFinding] = {}
    for finding in still_open:
        if _restored(finding, current, test_count):
            updates[finding["id"]] = {**finding, "status": "restored"}
        elif (reason := reasons.get(finding["path"])) and reason != finding.get("stale", ""):
            updates[finding["id"]] = {**finding, "status": "justified", "reason": reason}
    return updates


# ------------------------------------------------------------------ the verdict


def blocking(findings: Mapping[str, IntegrityFinding], mode: str) -> list[IntegrityFinding]:
    """The findings that fail the verdict: the open ones, and only with TEST_INTEGRITY=block."""
    if mode != "block":
        return []
    return [finding for finding in findings.values() if finding["status"] == "open"]


def failure_lines(findings: Iterable[IntegrityFinding]) -> list[str]:
    """The `[integrity]` failures a fix session receives, each saying how it can be cleared."""
    return [
        f"[integrity] {finding['detail']} Restore it, or, if the change is right, justify it in {CHANGES_FILE} "
        f"with a list item `- {finding['path']}: <why>`."
        for finding in findings
    ]


def report_block(findings: Mapping[str, IntegrityFinding], mode: str) -> str:
    """The verification report's section on test integrity ('' when the guard is off or found nothing)."""
    if mode == "off" or not findings:
        return ""
    open_count = sum(1 for finding in findings.values() if finding["status"] == "open")
    if not open_count:
        verdict = "CLEAR"
    elif mode == "block":
        verdict = f"BLOCKED ({open_count} open)"
    else:
        verdict = f"WARNING ({open_count} open, not blocking: TEST_INTEGRITY=warn)"
    lines = [f"### Test integrity: {verdict}", ""]
    for finding in findings.values():
        why = f" Justification: {finding['reason']}" if finding["status"] == "justified" else ""
        lines.append(f"- [{finding['status']}] {finding['detail']}{why}")
    return "\n".join(lines)
