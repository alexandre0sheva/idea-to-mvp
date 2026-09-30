from idea_to_mvp.blueprint_review import (
    CritiqueReport,
    Issue,
    finalize_report,
    format_issues,
    normalize_file,
    plan_issues_as_issues,
    render_review_markdown,
    revision_targets,
)

PATHS = ["README.md", "PRD.md", "ARCHITECTURE.md", "plan.md", ".claude/agents/web-ui.md"]


def blocker(file: str, text: str = "broken") -> Issue:
    return Issue(severity="blocker", file=file, description=text)


def warning(file: str, text: str = "meh") -> Issue:
    return Issue(severity="warning", file=file, description=text)


def test_the_critic_schema_is_provider_safe_every_field_is_required() -> None:
    schema = CritiqueReport.model_json_schema()
    for name, definition in schema["$defs"].items():
        assert set(definition["properties"]) == set(definition["required"]), name
    assert set(schema["properties"]) == set(schema["required"])


def test_a_report_with_a_blocker_is_never_approved_whatever_the_model_said() -> None:
    report = CritiqueReport(approved=True, issues=[warning("PRD.md"), blocker("plan.md")])
    assert finalize_report(report).approved is False


def test_a_report_with_only_warnings_is_approved_even_if_the_model_hesitated() -> None:
    report = CritiqueReport(approved=False, issues=[warning("PRD.md")])
    assert finalize_report(report).approved is True


def test_file_names_are_normalised_to_bundle_paths() -> None:
    assert normalize_file(" ./PRD.md ", PATHS) == "PRD.md"
    assert normalize_file("`plan.md`", PATHS) == "plan.md"
    assert normalize_file("plan.json", PATHS) == "plan.md"  # the plan is one document
    assert normalize_file("agents/web-ui.md", PATHS) == ".claude/agents/web-ui.md"
    assert normalize_file("the whole pack", PATHS) is None


def test_only_blocker_files_that_can_be_regenerated_are_revision_targets() -> None:
    issues = [
        blocker("./PRD.md"),
        warning("ARCHITECTURE.md"),  # warnings never trigger a revision
        blocker("plan.json"),
        blocker("PRD.md", "again"),  # no duplicates
        blocker("the whole pack"),  # cannot be regenerated
        blocker("STRATEGY.json"),  # not a generated document
    ]
    assert revision_targets(issues, PATHS) == ["PRD.md", "plan.md"]
    assert revision_targets([warning("PRD.md")], PATHS) == []


def test_issues_are_formatted_for_a_revision_prompt() -> None:
    text = format_issues([blocker("PRD.md", "R4 has no owner"), warning("PRD.md", "vague metric")])
    assert "- [blocker] R4 has no owner" in text and "- [warning] vague metric" in text


def test_deterministic_plan_problems_become_blockers_on_the_plan() -> None:
    issues = plan_issues_as_issues(["cycle: T01 -> T02 -> T01", "task T03 uses unknown workstream 'x'"])
    assert [(i.severity, i.file) for i in issues] == [("blocker", "plan.md")] * 2
    assert issues[0].description.startswith("cycle")


def test_the_review_document_lists_blockers_before_warnings() -> None:
    text = render_review_markdown([warning("PRD.md", "vague metric"), blocker("plan.md", "T02 cycle")])
    assert text.startswith("# Blueprint review")
    assert text.index("T02 cycle") < text.index("vague metric")
    assert "`plan.md`" in text and "`PRD.md`" in text
    assert render_review_markdown([]) == ""
