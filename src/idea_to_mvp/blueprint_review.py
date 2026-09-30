"""Cross-document review of a blueprint pack: the critic's report and what to do with it.

Pure helpers (LLM-free). The critic node in `nodes/blueprint.py` produces a `CritiqueReport`; this
module decides which documents a revision must regenerate, formats the issues for a revision prompt,
and renders the `REVIEW.md` that ships in the bundle when problems remain.

Schema rules as in `schemas.py`: every field is required, no defaults.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

PLAN_PATH = "plan.md"
_PLAN_ALIASES = frozenset({"plan.md", "plan.json"})


class Issue(BaseModel):
    severity: Literal["blocker", "warning"]
    file: str  # a bundle path such as "PRD.md"; free text is normalised by `normalize_file`
    description: str


class CritiqueReport(BaseModel):
    approved: bool
    issues: list[Issue]


def finalize_report(report: CritiqueReport) -> CritiqueReport:
    """A pack is approved exactly when nothing blocks it, whatever the model claimed."""
    blocked = any(issue.severity == "blocker" for issue in report.issues)
    return CritiqueReport(approved=not blocked, issues=report.issues)


def normalize_file(file: str, known_paths: Sequence[str]) -> str | None:
    """Map whatever the critic wrote ('./PRD.md', '`plan.json`', 'agents/web-ui.md') to a generated
    document path, or None when it names nothing that can be regenerated."""
    candidate = file.strip().strip("`'\"").strip()
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate in _PLAN_ALIASES and PLAN_PATH in known_paths:
        return PLAN_PATH
    if candidate in known_paths:
        return candidate
    for path in known_paths:
        if candidate and (path.endswith(f"/{candidate}") or candidate.endswith(f"/{path}")):
            return path
    return None


def revision_targets(issues: Sequence[Issue], known_paths: Sequence[str]) -> list[str]:
    """Documents a revision regenerates: those named by blocker issues, in first-named order."""
    targets: list[str] = []
    for issue in issues:
        if issue.severity != "blocker":
            continue
        path = normalize_file(issue.file, known_paths)
        if path is not None and path not in targets:
            targets.append(path)
    return targets


def issues_for(path: str, issues: Sequence[Issue], known_paths: Sequence[str]) -> list[Issue]:
    return [issue for issue in issues if normalize_file(issue.file, known_paths) == path]


def format_issues(issues: Sequence[Issue]) -> str:
    return "\n".join(f"- [{issue.severity}] {issue.description.strip()}" for issue in issues)


def plan_issues_as_issues(plan_issues: Sequence[str]) -> list[Issue]:
    """Validator findings are hard facts, so they block like a critic blocker on the plan."""
    return [Issue(severity="blocker", file=PLAN_PATH, description=text) for text in plan_issues]


def render_review_markdown(issues: Sequence[Issue]) -> str:
    """REVIEW.md: the problems still open in the pack, blockers first ('' when there are none)."""
    if not issues:
        return ""
    ordered = sorted(issues, key=lambda issue: issue.severity != "blocker")
    blockers = sum(1 for issue in ordered if issue.severity == "blocker")
    lines = [
        "# Blueprint review",
        "",
        f"{blockers} blocker(s) and {len(ordered) - blockers} warning(s) were still open when this pack was "
        "written. Decide whether to accept them before starting implementation.",
        "",
    ]
    lines += [f"- **{issue.severity}** `{issue.file.strip() or 'pack'}`: {issue.description.strip()}" for issue in ordered]
    return "\n".join(lines) + "\n"
