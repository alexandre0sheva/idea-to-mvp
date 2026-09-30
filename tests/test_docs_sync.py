import importlib.util
import re
import tomllib
from pathlib import Path

from idea_to_mvp.config import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_graph_diagram.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_graph_diagram", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_architecture_doc_graph_diagram_matches_real_graph() -> None:
    script = _load_script()
    current = script.DOC_PATH.read_text(encoding="utf-8")
    assert script.sync_document(current) == current, (
        "docs/architecture.md graph diagram is stale; run "
        "`uv run python scripts/gen_graph_diagram.py`"
    )


def test_diagram_mentions_every_pipeline_node() -> None:
    script = _load_script()
    diagram = script.render_diagram()
    for node in ("panel", "summarizer", "architect", "strategy", "prepare_workspace", "implementer", "delivery_report"):
        assert node in diagram


def test_diagram_expands_the_panel_subgraph() -> None:
    diagram = _load_script().render_diagram()
    for node in ("opening_turn", "merge_openings", "moderator", "speaker_turn"):
        assert node in diagram


def test_diagram_expands_the_parallel_implementation_subgraph() -> None:
    diagram = _load_script().render_diagram()
    for node in ("implement_plan", "pick_wave", "run_task_node", "merge_wave", "finish"):
        assert node in diagram


def test_diagram_expands_the_blueprint_subgraph() -> None:
    diagram = _load_script().render_diagram()
    for node in ("wave_1", "doc_1", "plan_writer", "wave_3", "doc_3", "review", "revise", "rewrite_plan", "write"):
        assert node in diagram


# ------------------------------------------------ what the docs say matches the code

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "idea_to_mvp"
DOCS = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "CLAUDE.md", "SECURITY.md")] + [
    ROOT / "docs" / "architecture.md"
]
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def code_spans(path: Path) -> list[str]:
    return _CODE_SPAN.findall(path.read_text(encoding="utf-8"))


def defines(file: Path, name: str) -> bool:
    return re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\b", file.read_text(encoding="utf-8"), re.MULTILINE) is not None


def test_every_repository_path_and_test_the_docs_name_exists() -> None:
    """`src/...`, `tests/...::test_name`, `docs/...`, `scripts/...` in backticks must point at something real."""
    missing: list[str] = []
    for doc in DOCS:
        for span in code_spans(doc):
            match = re.fullmatch(r"((?:src|tests|docs|scripts|\.github)/[\w./-]+?)(?:::(\w+))?", span)
            if not match or "*" in span:
                continue
            path, test = ROOT / match.group(1), match.group(2)
            if not path.exists():
                missing.append(f"{doc.name}: {span}")
            elif test and not defines(path, test):
                missing.append(f"{doc.name}: {span} (no such test)")
    assert not missing, "the docs name things that do not exist:\n" + "\n".join(missing)


def test_every_module_file_the_architecture_doc_names_exists() -> None:
    """`nodes/panel.py`, `exporter.py`, `guard.py`: a path relative to the package, or a bare file name that some
    module in the package has (the tables describe a sub-package and then list its files)."""
    missing = []
    for span in code_spans(ROOT / "docs" / "architecture.md"):
        if not re.fullmatch(r"[\w/]+\.py", span) or span.startswith(("src/", "tests/", "scripts/")):
            continue
        if not any(str(path).endswith("/" + span) for path in SRC.rglob(span.rsplit("/", 1)[-1])):
            missing.append(span)
    assert not missing, f"architecture.md names modules that do not exist: {missing}"


def _module_level_names() -> set[str]:
    """UPPER_CASE names the package defines (constants such as `GATES`, `LLM_RETRY`, `PROFILES`)."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        names |= set(re.findall(r"^([A-Z][A-Z0-9_]{3,})\b\s*[:=]", path.read_text(encoding="utf-8"), re.MULTILINE))
    return names


def test_every_setting_the_docs_name_is_a_real_one() -> None:
    """`MAX_ITERATIONS`-style names in the docs must be `Settings` fields, documented in `.env.example`, or a
    constant the package defines; nothing else in capitals may be in backticks."""
    known = {name.upper() for name in Settings.model_fields} | _module_level_names()
    known |= set(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", (ROOT / ".env.example").read_text(encoding="utf-8")))
    known |= {"START", "END", "LICENSE", "MAX_TOKENS"}  # LangGraph's constants, a file, a provider's finish reason
    unknown = {
        f"{doc.name}: {span}"
        for doc in DOCS
        for span in code_spans(doc)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", span) and span not in known
    }
    assert not unknown, "the docs name settings that do not exist:\n" + "\n".join(sorted(unknown))


def test_the_readme_embeds_three_or_four_screenshots_that_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme)
    assert 3 <= len(images) <= 4
    assert all((ROOT / image).is_file() for image in images), images


def test_the_version_is_released_and_matches_the_changelog() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version == "0.3.0"
    headings = re.findall(r"^## \[([^\]]+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE)
    assert headings[0] == version  # the newest release is the one in pyproject


def test_the_changelog_keeps_no_unreleased_leftovers_after_a_release() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## Unreleased", 1)[1].split("\n## [", 1)[0] if "## Unreleased" in text else ""
    assert not unreleased.strip(), "entries under Unreleased belong to the 0.3.0 release"


def test_the_architecture_doc_maps_every_langgraph_pattern_to_code_and_a_test() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "## LangGraph patterns used" in text
    section = text.split("## LangGraph patterns used", 1)[1].split("\n## ", 1)[0].lower()
    for pattern in (
        "interrupt",
        "checkpointer",
        "send",
        "subgraph",
        "structured output",
        "evaluator-optimizer",
        "retry polic",
        "custom stream",
        "cancellation",
    ):
        assert pattern in section, f"the patterns section does not cover {pattern!r}"
    rows = [line for line in section.splitlines() if line.startswith("|") and "---" not in line][1:]
    assert len(rows) >= 9
    assert all("src/idea_to_mvp/" in row and "tests/" in row and "::" in row for row in rows), (
        "every pattern names a source file and a test (`tests/test_x.py::test_name`)"
    )


def test_the_architecture_doc_has_an_extension_guide_for_agents_and_gates() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "## Extending the pipeline" in text
    guide = text.split("## Extending the pipeline", 1)[1].split("\n## ", 1)[0]
    assert "### Add an agent" in guide and "### Add a gate" in guide
