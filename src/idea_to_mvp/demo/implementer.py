"""Stand-in for the Claude Agent SDK implementation stage in demo mode.

Writes a tiny but real, dependency-free Python project into the workspace and verifies it by actually
running its unittest suite, so the delivery report and verdict come from a real process.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

VERIFY_TIMEOUT_SECONDS = 60


def _idea_from_workspace(workspace: Path) -> str:
    slug = re.sub(r"^\d{8}-\d{6}-\d+-", "", workspace.name)
    return slug.replace("-", " ").strip() or "your product idea"


def demo_implement(workspace: Path, strategy: dict[str, Any]) -> str:
    """Write the demo project and return an implementation summary."""
    workspace = Path(workspace)
    idea = _idea_from_workspace(workspace)
    package = workspace / "demo_app"
    tests = workspace / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)

    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        f'''IDEA = {json.dumps(idea)}


def describe() -> str:
    return f"Demo MVP for: {{IDEA}}"


def add_entry(entries: list[str], entry: str) -> list[str]:
    cleaned = entry.strip()
    if not cleaned:
        raise ValueError("entry must not be empty")
    return [*entries, cleaned]
''',
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        "from demo_app.core import describe\n\nprint(describe())\n", encoding="utf-8"
    )
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_core.py").write_text(
        '''import unittest

from demo_app.core import IDEA, add_entry, describe


class CoreTests(unittest.TestCase):
    def test_describe_mentions_the_idea(self):
        self.assertIn(IDEA, describe())

    def test_add_entry_appends_cleaned_text(self):
        self.assertEqual(add_entry(["a"], "  b "), ["a", "b"])

    def test_add_entry_rejects_blank_text(self):
        with self.assertRaises(ValueError):
            add_entry([], "   ")


if __name__ == "__main__":
    unittest.main()
''',
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        f"# Demo MVP: {idea}\n\n"
        "Generated in **demo mode** (no models were called): a tiny stand-in project so the whole "
        "pipeline can be tried offline.\n\n"
        "## Run\n\n```bash\npython -m demo_app\n```\n\n"
        "## Test\n\n```bash\npython -m unittest discover -s tests -t .\n```\n",
        encoding="utf-8",
    )

    workstreams = [str(w.get("name") or "workstream") for w in (strategy or {}).get("workstreams") or []]
    lines = [f"Demo mode: built a stand-in project for “{idea}” in `{workspace.name}`."]
    lines += [f"- Workstream `{name}`: completed (simulated)." for name in workstreams]
    lines.append("- Files: `demo_app/`, `tests/test_core.py`, `README.md`.")
    return "\n".join(lines)


def demo_verify(workspace: Path) -> dict[str, Any]:
    """Run the demo project's unittest suite and report in the real verifier's format."""
    workspace = Path(workspace)
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    try:
        run = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "report": "Demo test run timed out.\n\nVERDICT: FAIL"}
    output = (run.stderr or run.stdout).strip()
    ran = re.search(r"Ran (\d+) tests?", output)
    passed = run.returncode == 0
    report = (
        f"Ran {ran.group(1) if ran else '0'} tests with `python -m unittest`.\n\n"
        f"```\n{output[-1500:]}\n```\n\n"
        f"VERDICT: {'PASS' if passed else 'FAIL'}"
    )
    return {"passed": passed, "report": report}


def demo_fix(report: str) -> str:
    return "Demo mode: nothing to fix."
