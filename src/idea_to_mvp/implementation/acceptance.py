"""Held-out acceptance tests: a check of the product that the agents who build it never see.

The implementing agents write the code *and* its tests, so a green suite says the code does what its authors
thought, not what the PRD asked (Harness²'s answer-free principle: the agent being judged must not see the
grader). Before the first task of every version, the `acceptance_author` role writes a black-box suite from the
PRD's requirements and the plan's public contracts and commands only (`nodes/acceptance.py`). It is stored
**outside the workspace**, in `OUTPUT_DIR/acceptance/<workspace>/v<n>/`, so no task, resolver, or fix session
finds it in the files it works on.

The fourth verification lane (`verify.run_lane("acceptance")`) copies the project into a throwaway folder beside
the workspace (`make_scratch`), adds the suite, and has a read-mostly session install the dependencies and run the
suite's command, which leaves a JUnit report. The orchestrator judges the run by that report alone; the session's
own words are dropped. What reaches a fix session is only each failing test's name and its assertion message
(`acceptance_failures`, from the cases `reports.read_evidence` lists, which never carry a failure's body: a traceback quotes the
test's code), so the fix changes behaviour instead of fitting a test it has read. The lane's trajectory, which
does show the tests, is kept next to the suite, not in the workspace's `.idea-to-mvp/`.

This hides the tests from an agent that follows its brief; it does not make them secret from one that goes
looking (a shell can read outside the workspace; see `SECURITY.md`).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from idea_to_mvp.implementation.merge import worktree_root
from idea_to_mvp.implementation.reports import CaseResult, ReportedTests
from idea_to_mvp.implementation.workspace import GENERATED_DIRS, IGNORED_DIRS
from idea_to_mvp.plan import REPORTS_DIR

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings

ACCEPTANCE_DIR = "acceptance"  # the suite's folder, in its stored copy and in the scratch copy of the project
ACCEPTANCE_REPORT = f"{REPORTS_DIR}/acceptance.xml"  # what the suite's command must write
SUITE_FILE = "suite.json"
TRAJECTORY_DIR = "trajectories"
MAX_FILES = 12
MAX_FILE_CHARS = 60_000
_MAX_COMMAND = 500
_LISTED_FAILURES = 10
_SUFFIXES = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".go", ".rb", ".rs", ".java", ".kt", ".json", ".toml", ".ini", ".cfg"}
)
_REQUIREMENT = re.compile(r"(?<![A-Za-z0-9])[Rr](\d+)(?![0-9])")
_PARAMETERS = re.compile(r"\[.*\]$")  # pytest's `test_x[case-id]`: the ids name cases, not requirements
_UNCOPIED_DIRS = frozenset({*IGNORED_DIRS, *GENERATED_DIRS})

FIX_NOTE = """\
A failure tagged `[acceptance]` comes from black-box acceptance tests of the PRD that are kept out of this
workspace on purpose; you get each failing test's name and assertion message. Fix the behaviour the PRD and the
contracts in `plan.md` ask for. Do not look for the tests, and do not write code that only satisfies a test's name.
"""

LANE_STEPS = """\
This folder is a throwaway copy of the project with a held-out acceptance suite in `acceptance/`. Nothing you do
here reaches the project.
1. Install the project's dependencies{install}.
2. From the project root, run exactly this command once: `{command}`. It writes the JUnit report
   `{report}`, and the orchestrator judges the run by that report alone.
3. Do not change, add, or delete any file (the dependency install excepted). Do not edit the tests, and never
   create or edit the report by hand.
Report the commands you ran with their exit codes. `failures` may stay empty: the orchestrator reads them from
the report.
"""


class AcceptanceFile(BaseModel):
    path: str  # relative to the suite's `acceptance/` folder
    content: str

    @field_validator("path")
    @classmethod
    def _inside_the_suite(cls, value: str) -> str:
        path = PurePosixPath(value.strip())
        if not value.strip() or path.is_absolute() or "\\" in value:
            raise ValueError("a relative path inside the acceptance folder is required")
        if any(part in ("..", "") or part.startswith(".") for part in path.parts):
            raise ValueError("the path may not leave the acceptance folder or name a hidden file")
        if path.suffix not in _SUFFIXES:
            raise ValueError(f"a test or config file is required (one of {', '.join(sorted(_SUFFIXES))})")
        return path.as_posix()

    @field_validator("content")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if len(value) > MAX_FILE_CHARS:
            raise ValueError(f"a file may hold at most {MAX_FILE_CHARS} characters")
        return value


class AcceptanceSuite(BaseModel):
    """What the acceptance author returns: the test files, the command that runs them, and the requirements
    they check. An empty suite (no files) is allowed and means "no acceptance tests for this version"."""

    files: list[AcceptanceFile]
    command: str  # run from the project root; must write ACCEPTANCE_REPORT
    covers: list[str]  # requirement ids ("R1", ...) the suite checks

    @model_validator(mode="after")
    def _runnable(self) -> AcceptanceSuite:
        if len(self.files) > MAX_FILES:
            raise ValueError(f"at most {MAX_FILES} files")
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("every file needs its own path")
        command = self.command.strip()
        if self.files:
            if not command or "\n" in command or len(command) > _MAX_COMMAND:
                raise ValueError(f"the command must be one line of at most {_MAX_COMMAND} characters")
            if ACCEPTANCE_REPORT not in command or not _runs_the_suite(command):
                raise ValueError(f"the command must run the `{ACCEPTANCE_DIR}/` folder and write JUnit XML to {ACCEPTANCE_REPORT}")
        self.command = command
        return self


def _runs_the_suite(command: str) -> bool:
    """Whether a word of the command (the report path aside) names the suite's folder or a file in it."""
    for word in re.split(r"[\s=]+", command.replace(ACCEPTANCE_REPORT, " ")):
        path = word.strip("'\"").removeprefix("./")
        if path == ACCEPTANCE_DIR or path.startswith(f"{ACCEPTANCE_DIR}/"):
            return True
    return False


# ------------------------------------------------------------------ storage


def suite_root(settings: Settings, workspace: Path) -> Path:
    """Where a project's acceptance suites live: beside the projects folder, never inside a workspace."""
    return settings.output_dir / ACCEPTANCE_DIR / Path(workspace).name


def suite_dir(settings: Settings, workspace: Path, iteration: int) -> Path:
    return suite_root(settings, workspace) / f"v{iteration}"


def save_suite(folder: Path, suite: AcceptanceSuite) -> list[str]:
    """Write a suite (replacing whatever that folder held): its files under `acceptance/`, and `suite.json`."""
    folder = Path(folder)
    shutil.rmtree(folder, ignore_errors=True)
    tests = folder / ACCEPTANCE_DIR
    tests.mkdir(parents=True)
    for file in suite.files:
        target = tests / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
    (folder / SUITE_FILE).write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    return [file.path for file in suite.files]


def load_suite(folder: Path | None) -> AcceptanceSuite | None:
    """The suite saved in `folder`, or None when there is none (or it no longer validates)."""
    if folder is None:
        return None
    try:
        return AcceptanceSuite.model_validate_json((Path(folder) / SUITE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


# ------------------------------------------------------------------ the throwaway copy


def make_scratch(workspace: Path, folder: Path, label: str) -> Path:
    """A throwaway copy of the project beside it (like a task worktree) with the suite in `acceptance/`.

    Dependency and build folders, `.git`, and the orchestrator's `.idea-to-mvp/` are not copied (so nothing the
    lane does can reach the workspace's git or records), and a top-level `acceptance/` of the project's own is
    replaced by the suite.
    """
    workspace = Path(workspace).resolve()
    target = worktree_root(workspace) / f"{ACCEPTANCE_DIR}-{label}"
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    def skip(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in _UNCOPIED_DIRS}
        if Path(directory).resolve() == workspace and ACCEPTANCE_DIR in names:
            skipped.add(ACCEPTANCE_DIR)
        return skipped

    shutil.copytree(workspace, target, symlinks=True, ignore=skip, copy_function=_copy_regular_file)
    shutil.copytree(Path(folder) / ACCEPTANCE_DIR, target / ACCEPTANCE_DIR)
    (target / REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    return target


def _copy_regular_file(source: str, target: str) -> str:
    """Copy one file; sockets and pipes (a dev server's) are skipped, and a file that vanished while the copy ran
    (the tests lane installs dependencies at the same time) is left out instead of failing the copy."""
    try:
        if not stat.S_ISREG(os.stat(source).st_mode):
            return target
        return str(shutil.copy2(source, target))
    except FileNotFoundError:
        return target


def remove_scratch(workspace: Path, scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)
    root = worktree_root(Path(workspace).resolve())
    for folder in (root, root.parent):
        try:
            folder.rmdir()  # only when empty: task worktrees may be beside it
        except OSError:
            break


# ------------------------------------------------------------------ the verdict


def requirement_status(cases: Sequence[CaseResult]) -> dict[str, str]:
    """Requirement id -> `passed` / `failed` / `skipped`, from the tests whose names carry the id
    (`test_R1_...`, `it("R2: ...")`): failed if any of them failed, passed if any passed, else skipped. Only the
    test's own name counts, not its file or class (`test_r1_journey.py` may hold an R2 test) or its parameters."""
    seen: dict[str, set[str]] = {}
    for case in cases:
        name = _PARAMETERS.sub("", case.name.rsplit("::", 1)[-1])
        for number in _REQUIREMENT.findall(name):
            seen.setdefault(f"R{int(number)}", set()).add(case.status)
    order = sorted(seen, key=lambda rid: int(rid[1:]))
    return {
        rid: "failed" if "failed" in seen[rid] else "passed" if "passed" in seen[rid] else "skipped" for rid in order
    }


def acceptance_failures(evidence: ReportedTests, cases: Sequence[CaseResult]) -> list[str]:
    """Why the acceptance run did not pass, as failing test names and assertion messages only ([] when it did)."""
    if evidence.problem:
        return [evidence.problem]
    failing = [case for case in cases if case.status == "failed"]
    if failing:
        lines = [f"{case.name}: {case.message}" for case in failing[:_LISTED_FAILURES]]
        if len(failing) > _LISTED_FAILURES:
            lines.append(f"... and {len(failing) - _LISTED_FAILURES} more failing acceptance test(s).")
        return lines
    if evidence.failed + evidence.errors:
        return [f"{evidence.failed + evidence.errors} acceptance test(s) failed."]
    if evidence.tests == 0:
        return ["The acceptance report lists no tests."]
    if evidence.passed == 0:
        return [f"No acceptance test ran ({evidence.skipped} skipped)."]
    return []
