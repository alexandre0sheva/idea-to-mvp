"""The experience library: storage and index, the leak check, the reflect node, and learning across two demo projects."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from idea_to_mvp import llm
from idea_to_mvp.config import clear_settings_cache, get_settings
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation.acceptance import (
    AcceptanceFile,
    AcceptanceSuite,
    save_suite,
    suite_dir,
)
from idea_to_mvp.implementation.ledger import append_row
from idea_to_mvp.library import (
    ENTRIES_DIR,
    HARNESS_DIR,
    INDEX_FILE,
    EditRow,
    Entry,
    Forbidden,
    Lesson,
    LibraryLeak,
    Reflection,
    domain_for,
    entry_id,
    forbidden_for,
    forget,
    leaks,
    list_domains,
    load_entries,
    redact,
    reference_block,
    reflection_problems,
    render_index,
    snapshot,
    usable_rows,
    value_leaks,
    write_entry,
)
from idea_to_mvp.nodes import reflect
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.library import entry_choices, library_markdown, read_view
from idea_to_mvp.ui.view import reflection_markdown, transcript_from_state

IDEA = "A habit tracker for climbing gyms"
NOTHING = Forbidden()
SKILL = ".claude/skills/run-the-tests/SKILL.md"
ACCEPTANCE_TEST = '''import unittest


class Acceptance(unittest.TestCase):
    def test_R1_the_tracker_lists_the_climbs_in_order(self):
        self.assertEqual(sorted_climbs(["b", "a"]), ["a", "b"], "climbs come back in the order they were logged")
'''


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.chdir(tmp_path)  # no `.env`
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("EXPERIENCE_LIBRARY", raising=False)
    clear_settings_cache()
    yield
    clear_settings_cache()


def lesson(cue: str = "The tests import the package from src/", confidence: str = "medium") -> dict[str, str]:
    return {"cue": cue, "lesson": "Install the project in editable mode first.", "evidence": "T01 failed twice on an import.", "confidence": confidence}


def entry(entry_id: str = "proj-v1-aaaa", *, created: float = 1_000.0, edits: list[dict[str, str]] | None = None, **fields: Any) -> Entry:
    base: Entry = {
        "id": entry_id,
        "domain": "python-fastapi",
        "project": "proj",
        "iteration": 1,
        "verified": True,
        "created_at": created,
        "edits": edits or [],
        "lesson": lesson(),
        "harness": None,
    }
    return {**base, **fields}  # type: ignore[typeddict-item]


# ------------------------------------------------------------------ domains, storage, the index


def test_the_domain_is_the_template_else_the_chosen_stack_else_general() -> None:
    proposal = {"option_a": {"stack": ["Python 3.12", "FastAPI", "HTMX"]}, "option_b": {"stack": ["Next.js", "Postgres"]}}
    assert domain_for({"template": "react-vite"}, proposal, {"option": "A"}) == "react-vite"
    assert domain_for({"template": "none"}, proposal, {"option": "A"}) == "python-fastapi"
    assert domain_for({}, proposal, {"option": "B"}) == "nextjs-postgres"
    assert domain_for({"template": "../etc"}, {}, {}) == "general"  # a name that is no folder name is not a domain
    assert domain_for(None, {"option_a": {"stack": ["Python", "python"]}}, None) == "python"


def test_an_entry_id_is_stable_per_project_version_and_drops_the_pack_timestamp(tmp_path: Path) -> None:
    workspace = tmp_path / "20261020-153000-123456-climbing-tracker"
    assert entry_id(workspace, 1) == entry_id(workspace, 1) != entry_id(workspace, 2)
    assert entry_id(workspace, 1).startswith("climbing-tracker-v1-")


def test_entries_are_written_listed_indexed_and_forgotten(tmp_path: Path) -> None:
    root = tmp_path / "library"
    harness = {"decision": "adopt_dom", "summary": "DOM: a skill for the tests", "files": [SKILL]}
    write_entry(root, entry(harness=harness), {SKILL: "---\nname: run-the-tests\n---\n1. Run them.\n"}, forbidden=NOTHING)
    write_entry(root, entry("other-v1-bbbb", created=2_000.0, lesson=None, edits=[{"edit": "lesson:proj-v1-aaaa", "effect": "helped", "evidence": "T01 passed at once."}]), forbidden=NOTHING)
    folder = root / "python-fastapi"
    assert list_domains(root) == ["python-fastapi"]
    assert [e["id"] for e in load_entries(root, "python-fastapi")] == ["proj-v1-aaaa", "other-v1-bbbb"]
    assert (folder / HARNESS_DIR / "proj-v1-aaaa" / SKILL).read_text().startswith("---\nname: run-the-tests")
    index = (folder / INDEX_FILE).read_text()
    assert "When: The tests import the package from src/" in index and "later projects: helped 1" in index
    assert "adopt_dom" in index and f"`{SKILL}`" in index

    assert forget(root, "python-fastapi", "proj-v1-aaaa") and not forget(root, "python-fastapi", "proj-v1-aaaa")
    assert not (folder / HARNESS_DIR / "proj-v1-aaaa").exists()
    assert "The tests import" not in (folder / INDEX_FILE).read_text()  # the index was rebuilt
    assert not forget(root, "python-fastapi", "../../etc")


def test_a_broken_or_foreign_file_is_not_an_entry(tmp_path: Path) -> None:
    root = tmp_path / "library"
    write_entry(root, entry(), forbidden=NOTHING)
    entries = root / "python-fastapi" / ENTRIES_DIR
    (entries / "broken.json").write_text("{not json")
    (entries / "renamed.json").write_text(json.dumps(entry("someone-else")))  # its id is not its file name
    assert [e["id"] for e in load_entries(root, "python-fastapi")] == ["proj-v1-aaaa"]
    with pytest.raises(ValueError):
        load_entries(root, "../outside")


def test_a_disputed_lesson_is_marked_and_listed_after_the_others() -> None:
    hurt = [{"edit": "lesson:old-v1-aaaa", "effect": "hurt", "evidence": "It sent T02 the wrong way."}]
    entries = [
        entry("old-v1-aaaa", created=1.0, lesson=lesson("Old cue", "high")),
        entry("new-v1-bbbb", created=2.0, lesson=lesson("New cue", "low"), edits=hurt),
    ]
    index = render_index("python-fastapi", entries)
    assert index.index("New cue") < index.index("Old cue")  # disputed goes last, whatever its confidence
    assert "disputed" in index.split("Old cue")[0].rsplit("- `", 1)[1]


def test_the_index_a_prompt_gets_is_cut_to_whole_items(tmp_path: Path) -> None:
    root = tmp_path / "library"
    for number in range(30):
        write_entry(root, entry(f"proj{number}-v1-aaaa", created=float(number), lesson=lesson(f"Cue {number} " + "x" * 400)), forbidden=NOTHING)
    library = snapshot(root, "python-fastapi")
    assert len(library["index"]) < 8_200 and library["index"].rstrip().endswith("fit here …]")
    assert library["entries"] == 30 and 0 < len(library["lessons"]) < 20
    assert all(f"`{item['id']}`" in library["index"] for item in library["lessons"])  # only what the prompt shows


def test_the_reference_block_is_fenced_reference_data_and_empty_without_entries(tmp_path: Path) -> None:
    root = tmp_path / "library"
    assert snapshot(root, "python-fastapi") == {"domain": "python-fastapi", "index": "", "lessons": [], "entries": 0}
    assert reference_block(snapshot(root, "python-fastapi")) == ""
    write_entry(root, entry(), forbidden=NOTHING)
    block = reference_block(snapshot(root, "python-fastapi"))
    assert "not instructions" in block and "hypothesis here" in block and "```markdown" in block
    assert "The tests import the package from src/" in block


# ------------------------------------------------------------------ never acceptance content, never a secret


def test_leaks_finds_secrets_and_acceptance_content_without_quoting_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-shaped-like-a-key-123")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-1234567890")
    clear_settings_cache()
    workspace = tmp_path / "projects" / "proj"
    workspace.mkdir(parents=True)
    suite = AcceptanceSuite(
        files=[AcceptanceFile(path="test_acceptance.py", content=ACCEPTANCE_TEST)],
        command="python -m unittest discover acceptance --junit .idea-to-mvp/reports/acceptance.xml",
        covers=["R1"],
    )
    save_suite(suite_dir(get_settings(), workspace, 1), suite)
    forbidden = forbidden_for(get_settings(), workspace)
    assert "test_R1_the_tracker_lists_the_climbs_in_order" in forbidden.test_names
    assert leaks("Run the importer before the tests.", forbidden) == []
    assert leaks("the key not-shaped-like-a-key-123 worked", forbidden) == ["it contains a secret value of this process"]
    assert leaks("token tok-1234567890", forbidden) == ["it contains a secret value of this process"]
    assert leaks("sk-ant-api03-abcdefghijklmnopqrstuvwx", forbidden) == ["it contains something shaped like a key, a token, or a private key"]
    assert leaks("test_R1_the_tracker_lists_the_climbs_in_order failed", forbidden) == ["it names a held-out acceptance test"]
    quoted = 'self.assertEqual(sorted_climbs(["b", "a"]), ["a", "b"], "climbs come back in the order they were logged")'
    assert leaks(f"the check {quoted}", forbidden) == ["it quotes a line of the held-out acceptance tests"]


def test_write_entry_refuses_a_leaking_entry_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "library"
    forbidden = Forbidden(secrets=("hunter2-secret",), test_names=("test_R2_hidden_case",))
    with pytest.raises(LibraryLeak) as error:
        write_entry(root, entry(lesson=lesson("When test_R2_hidden_case fails")), forbidden=forbidden)
    assert "test_R2_hidden_case" not in str(error.value)
    with pytest.raises(LibraryLeak):
        write_entry(root, entry(), {SKILL: "export KEY=hunter2-secret"}, forbidden=forbidden)
    with pytest.raises(ValueError, match="not a harness file"):
        write_entry(root, entry(), {"../outside.md": "text"}, forbidden=NOTHING)
    assert not (root / "python-fastapi").exists()


def test_a_quoted_or_respaced_acceptance_line_is_caught_in_every_field_not_only_in_raw_text() -> None:
    line = 'self.assertEqual(sorted_climbs(["b", "a"]), ["a", "b"], "climbs come back in the order they were logged")'
    forbidden = Forbidden(suite_lines=(" ".join(line.split()),))
    assert value_leaks({"lesson": {"cue": f"The check {line}"}}, forbidden) == ["it quotes a line of the held-out acceptance tests"]
    respaced = Forbidden(suite_lines=('expected = {"a": 1, "b": 2} # sorted by the first time logged',))
    assert leaks('Use expected = {"a":  1,   "b": 2}  # sorted   by the first time logged', respaced)  # runs of spaces
    with pytest.raises(LibraryLeak):  # the JSON dump of the entry escapes the quotes; the check must not look there
        write_entry(Path("unused"), entry(lesson=lesson(f"When {line}")), forbidden=forbidden)


def test_redact_hides_acceptance_names_lines_and_secrets_from_a_prompt() -> None:
    forbidden = Forbidden(
        secrets=("hunter2-secret",),
        suite_lines=('self.assertEqual(sorted_climbs(["b", "a"]), ["a", "b"], "climbs in order")',),
        test_names=("test_R1_hidden_case",),
    )
    text = (
        "- note: new -> test_R1_hidden_case fails -> expected 3\n"
        '    self.assertEqual(sorted_climbs(["b", "a"]),  ["a", "b"], "climbs in order")\n'
        "token hunter2-secret and sk-ant-api03-abcdefghijklmnopqrstuvwx; test_R1_hidden_case_two stays"
    )
    hidden = redact(text, forbidden)
    assert "test_R1_hidden_case fails" not in hidden and "[held-out test] fails" in hidden
    assert hidden.splitlines()[1] == "[held-out test line]" and "sorted_climbs" not in hidden
    assert "hunter2" not in hidden and "sk-ant" not in hidden and hidden.count("[secret]") == 2
    assert "test_R1_hidden_case_two stays" in hidden  # a longer name is another name


def test_writes_from_two_processes_at_once_leave_every_entry_in_the_index(tmp_path: Path) -> None:
    root = tmp_path / "library"
    script = (
        "import sys; from pathlib import Path; from idea_to_mvp.library import Forbidden, write_entry\n"
        "for n in range(8):\n"
        "    eid = f'{sys.argv[2]}{n}-v1-aaaa'\n"
        "    write_entry(Path(sys.argv[1]), {'id': eid, 'domain': 'python-fastapi', 'project': 'p', 'iteration': 1,"
        " 'verified': True, 'created_at': float(n), 'edits': [], 'harness': None,"
        " 'lesson': {'cue': eid, 'lesson': 'l', 'evidence': 'e', 'confidence': 'low'}}, forbidden=Forbidden())\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(root), name]) for name in ("a", "b")]
    assert [proc.wait(timeout=60) for proc in procs] == [0, 0]
    index = (root / "python-fastapi" / INDEX_FILE).read_text()
    assert all(f"`{name}{n}-v1-aaaa`" in index for name in "ab" for n in range(8))


# ------------------------------------------------------------------ the reflector's answer


def test_a_reflection_is_judged_against_the_edits_it_was_given() -> None:
    ids = ["harness:inv", "lesson:a"]
    answer = Reflection(
        edits=[
            EditRow(edit="harness:inv", effect="confounded", evidence="One run each."),
            EditRow(edit="harness:inv", effect="helped", evidence="Twice."),
            EditRow(edit="harness:dom", effect="hurt", evidence="Not listed."),
        ],
        lessons=[Lesson(cue="a", lesson="b", evidence="c", confidence="low")] * 2,
        rationale="",
    )
    problems = reflection_problems(answer, ids)
    assert "2 lessons: write at most one (or none)" in problems and "harness:inv is judged 2 times" in problems
    assert "harness:dom: not an edit listed under Edits to judge" in problems and "lesson:a is not judged" in problems
    assert [(row.edit, row.effect) for row in usable_rows(answer, ids)] == [("harness:inv", "confounded")]


# ------------------------------------------------------------------ the reflect node


def fake_reflector(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[list[Any]]:
    calls: list[list[Any]] = []

    class Runtime:
        system_prompt = "system"

    def invoke(runtime: Any, messages: list[Any], schema: type, fallback: Any = None) -> Any:
        calls.append(messages)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(llm, "get_runtime", lambda role: Runtime())
    monkeypatch.setattr(llm, "invoke_structured", invoke)
    return calls


TUNING = {
    "status": "done",
    "pilot": {"id": "T01", "title": "Setup"},
    "adopted": "adopt_dom",
    "inv": {"summary": "Run the checks first.", "files": [{"path": ".claude/skills/a/SKILL.md", "content": "x"}]},
    "dom": {"summary": "A skill for the tests.", "files": [{"path": SKILL, "content": "y"}]},
    "compose": {"decision": "adopt_dom", "rationale": "DOM avoided the import error.", "notes": [], "files": [{"path": SKILL, "content": "---\nname: run-the-tests\n---\n1. Run.\n"}]},
}


def delivered_state(tmp_path: Path, **fields: Any) -> dict[str, Any]:
    workspace = tmp_path / "projects" / "20261020-153000-123456-climbing"
    (workspace / ".idea-to-mvp" / "trajectories").mkdir(parents=True)
    (workspace / ".idea-to-mvp" / "trajectories" / "T02.md").write_text("[ACTION] Bash {\"command\": \"pytest\"}\n[ERROR] ImportError\n")
    append_row(
        workspace,
        {"kind": "task", "label": "T02", "task_id": "T02", "iteration": 1, "attempt": 1, "model": "m", "cost_usd": 0.5, "turns": 9,
         "outcome": "failed", "session_id": None, "trajectory": ".idea-to-mvp/trajectories/T02.md", "started_at": 1.0, "ended_at": 2.0},
    )  # fmt: skip
    state = make_initial_state(IDEA, 1)
    lanes = [
        {"lane": "tests", "passed": False, "summary": "1 failure", "failures": ["tests/test_core.py::test_x: boom"], "commands_run": []},
        {"lane": "acceptance", "passed": False, "summary": "secret summary", "failures": ["acceptance/t.py::test_R1_hidden: no"],
         "commands_run": [], "evidence": {"tests": 2, "failed": 1, "errors": 0, "failures": ["test_R1_hidden: no"]}},
    ]  # fmt: skip
    history = [{"attempt": 1, "model": "m", "kind": "code", "failures": ["[tests] boom", "[acceptance] test_R1_hidden: no"], "files": ["app.py"],
                "notes": [{"verdict": "new", "hypothesis": "the import path", "evidence": "ImportError"}], "result": "failed"}]  # fmt: skip
    state.update(
        workspace_dir=str(workspace),
        execution_strategy={"mode": "agent_team", "template": "python-fastapi"},
        verification={"passed": False, "attempts": 1, "report": "test_R1_hidden failed", "lanes": lanes, "history": history},
        **fields,
    )
    return state  # type: ignore[return-value]


def test_reflect_records_edit_rows_and_one_lesson_from_evidence_without_acceptance_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    library = {"domain": "python-fastapi", "index": "# idx", "lessons": [{"id": "old-v1-aaaa", "cue": "c", "lesson": "l", "confidence": "low"}]}
    state = delivered_state(tmp_path, harness_tuning=TUNING, library=library)
    good = Lesson(cue="pytest cannot import the package", lesson="Install it editable first.", evidence="T02's ImportError.", confidence="medium")
    first = Reflection(edits=[EditRow(edit="harness:inv", effect="helped", evidence="x")], lessons=[good], rationale="r")
    second = Reflection(
        edits=[
            EditRow(edit="harness:inv", effect="confounded", evidence="One pilot run each."),
            EditRow(edit="harness:dom", effect="helped", evidence="The DOM run had no ImportError."),
            EditRow(edit="lesson:old-v1-aaaa", effect="no observed effect", evidence="Nothing touched it."),
        ],
        lessons=[good],
        rationale="The import path was the one recurring problem.",
    )
    calls = fake_reflector(monkeypatch, [first, second])
    update = reflect.reflect_node(state)  # type: ignore[arg-type]

    prompt = calls[0][-1].content
    assert len(calls) == 2 and "harness:dom is not judged" in calls[1][-1].content  # one repair attempt
    assert "`harness:inv`" in prompt and "`harness:dom`" in prompt and "`lesson:old-v1-aaaa`" in prompt
    assert "ImportError" in prompt and "test_core.py::test_x: boom" in prompt and "the import path" in prompt
    assert "test_R1_hidden" not in prompt and "secret summary" not in prompt  # acceptance names and messages stay out
    assert "acceptance: failed (2 tests, 1 failed, 0 errors)" in prompt

    record = update["reflection"]
    assert record["status"] == "recorded" and record["lesson"]["cue"] == "pytest cannot import the package"
    stored = load_entries(get_settings().library_dir, "python-fastapi")
    assert [e["id"] for e in stored] == [record["entry"]]
    assert [row["edit"] for row in stored[0]["edits"]] == ["harness:inv", "harness:dom", "lesson:old-v1-aaaa"]
    assert stored[0]["harness"] == {"decision": "adopt_dom", "summary": "DOM: A skill for the tests.", "files": [SKILL]}
    assert (get_settings().library_dir / "python-fastapi" / HARNESS_DIR / record["entry"] / SKILL).is_file()
    shown = reflection_markdown(record)
    assert "recorded a lesson" in shown and "`harness:dom`: helped" in shown


def test_reflect_drops_a_lesson_that_names_an_acceptance_test_or_holds_a_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret-value")
    clear_settings_cache()
    state = delivered_state(tmp_path)
    suite = AcceptanceSuite(
        files=[AcceptanceFile(path="test_acceptance.py", content=ACCEPTANCE_TEST)],
        command="python -m unittest discover acceptance --junit .idea-to-mvp/reports/acceptance.xml",
        covers=["R1"],
    )
    save_suite(suite_dir(get_settings(), Path(state["workspace_dir"]), 1), suite)
    state["verification"]["history"][0]["notes"][0]["hypothesis"] = "test_R1_the_tracker_lists_the_climbs_in_order fails"
    leaky = Lesson(cue="test_R1_the_tracker_lists_the_climbs_in_order fails", lesson="Sort the climbs.", evidence="the lane", confidence="high")
    calls = fake_reflector(monkeypatch, [Reflection(edits=[], lessons=[leaky], rationale="")])
    record = reflect.reflect_node(state)["reflection"]  # type: ignore[arg-type]
    prompt = calls[0][-1].content  # a fix session's notes named the hidden test: the reflector never sees the name
    assert "test_R1_the_tracker" not in prompt and "note: new -> [held-out test] fails" in prompt
    assert record["status"] == "no_entry" and record["lesson"] is None
    assert record["dropped"] == ["the lesson (it names a held-out acceptance test)"]
    assert list_domains(get_settings().library_dir) == []

    quoted = Lesson(
        cue="The order check fails",
        lesson='Make self.assertEqual(sorted_climbs(["b", "a"]), ["a", "b"], "climbs come back in the order they were logged") pass.',
        evidence="the lane",
        confidence="low",
    )
    fake_reflector(monkeypatch, [Reflection(edits=[], lessons=[quoted], rationale="Because test_R1_the_tracker_lists_the_climbs_in_order failed.")])
    record = reflect.reflect_node(state)["reflection"]  # type: ignore[arg-type]
    assert record["dropped"] == [
        "the lesson (it quotes a line of the held-out acceptance tests)",
        "the rationale (it names a held-out acceptance test)",
    ] and record["rationale"] == ""

    secret = Lesson(cue="Pushing needs a token", lesson="Use github-secret-value.", evidence="the run", confidence="low")
    fake_reflector(monkeypatch, [Reflection(edits=[], lessons=[secret], rationale="")])
    record = reflect.reflect_node(state)["reflection"]  # type: ignore[arg-type]
    assert record["dropped"] == ["the lesson (it contains a secret value of this process)"]
    assert "github-secret-value" not in json.dumps(record)


def test_a_later_version_judges_no_edits_and_a_failed_reflection_does_not_stop_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = delivered_state(tmp_path, harness_tuning=TUNING, iteration=2, library={"lessons": [{"id": "x"}]})
    calls = fake_reflector(monkeypatch, [Reflection(edits=[], lessons=[], rationale="nothing new")])
    record = reflect.reflect_node(state)["reflection"]  # type: ignore[arg-type]
    assert record["status"] == "no_entry" and "(none: return an empty `edits` list)" in calls[0][-1].content
    assert "harness tuning did not run" in calls[0][-1].content  # tuning belongs to v0.1
    fake_reflector(monkeypatch, [ValueError("schema says no")])
    record = reflect.reflect_node(state)["reflection"]  # type: ignore[arg-type]
    assert record["status"] == "failed" and "schema says no" in record["note"]
    assert reflect.reflect_node({"workspace_dir": ""})["reflection"]["status"] == "skipped"  # type: ignore[typeddict-item]


def test_reflect_runs_after_a_delivery_only_while_the_library_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"iteration": 1}
    assert get_settings().library_enabled and reflect.route_to_reflect(state) == "reflect"  # type: ignore[arg-type]
    monkeypatch.setenv("EXPERIENCE_LIBRARY", "off")
    clear_settings_cache()
    assert reflect.route_to_reflect(state) == "iterate_gate"  # type: ignore[arg-type]
    monkeypatch.delenv("EXPERIENCE_LIBRARY")
    monkeypatch.setenv("DEMO_MODE", "true")
    clear_settings_cache()
    assert not get_settings().library_enabled  # canned demo lessons never reach a real library unless asked for


# ------------------------------------------------------------------ two demo projects: the second learns from the first


async def build_to_iterate_gate(graph: Any, thread: str, *, tune: bool) -> dict[str, Any]:
    config = run_config(thread)
    await graph.ainvoke(make_initial_state(IDEA, 1), config)
    await graph.ainvoke(Command(resume="1. Solo climbers."), config)
    await graph.ainvoke(Command(resume={"option": "A", "notes": ""}), config)
    await graph.ainvoke(Command(resume={"generate": True, "notes": ""}), config)
    await graph.ainvoke(Command(resume={"implement": True, "notes": "", "harness_tuning": tune}), config)
    return dict((await graph.aget_state(config)).values)


async def test_a_second_demo_project_of_the_stack_is_planned_and_tuned_with_the_first_ones_lesson(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERIENCE_LIBRARY", "on")
    clear_settings_cache()
    prompts: dict[str, list[str]] = {}
    real = llm.invoke_structured

    def spy(runtime: Any, messages: list[Any], schema: type, fallback: Any = None) -> Any:
        prompts.setdefault(schema.__name__, []).append(str(messages[-1].content))
        return real(runtime, messages, schema, fallback=fallback)

    monkeypatch.setattr(llm, "invoke_structured", spy)
    graph = build_graph(MemorySaver())
    first = await build_to_iterate_gate(graph, "first", tune=False)
    root = demo_env / "library"
    assert first["library"] == {"domain": "python-fastapi", "index": "", "lessons": [], "entries": 0}
    assert first["reflection"]["status"] == "recorded" and first["reflection"]["edits"] == []  # nothing tried yet
    assert "Experience library: earlier" not in prompts["Plan"][0]
    lesson_id = first["reflection"]["entry"]
    assert (root / "python-fastapi" / INDEX_FILE).read_text().count("When: The project's tests use only") == 1

    prompts.clear()
    second = await build_to_iterate_gate(graph, "second", tune=True)
    assert [item["id"] for item in second["library"]["lessons"]] == [lesson_id]
    assert all("When: The project's tests use only" in prompt for prompt in [*prompts["Plan"], *prompts["HarnessSpec"], *prompts["HarnessEdit"]])
    assert len(prompts["HarnessEdit"]) == 2  # both proposers got it
    reflection = second["reflection"]
    assert reflection["status"] == "recorded" and reflection["lesson"] is None  # the demo lesson is known already
    assert {row["edit"]: row["effect"] for row in reflection["edits"]} == {
        "harness:inv": "confounded", "harness:dom": "confounded", f"lesson:{lesson_id}": "confounded",
    }  # fmt: skip
    stored = {e["id"]: e for e in load_entries(root, "python-fastapi")}
    assert stored[reflection["entry"]]["harness"]["decision"] == "merge"  # the demo composer merges both edits
    index = (root / "python-fastapi" / INDEX_FILE).read_text()
    assert "later projects: confounded 1" in index and "## Harness edits adopted by earlier projects" in index

    kinds = [entry.kind for entry in transcript_from_state(second, None)]
    assert "library" in kinds and kinds.index("reflection") == kinds.index("delivery_report") + 1


# ------------------------------------------------------------------ the panel


def test_the_library_panel_lists_a_stack_and_forgets_an_entry(tmp_path: Path) -> None:
    root = tmp_path / "library"
    assert "The library is empty" in library_markdown(read_view(root, None))
    write_entry(root, entry(), forbidden=NOTHING)
    write_entry(root, entry("rows-v1-bbbb", created=2_000.0, lesson=None, edits=[{"edit": "harness:inv", "effect": "hurt", "evidence": "e"}]), forbidden=NOTHING)
    view = read_view(root, "missing")  # a stack that is not there: the first one is shown
    assert view.domain == "python-fastapi" and [e["id"] for e in view.entries] == ["proj-v1-aaaa", "rows-v1-bbbb"]
    text = library_markdown(view, enabled=False)
    assert "EXPERIENCE_LIBRARY=off" in text and "### python-fastapi" in text and "`harness:inv`: hurt" in text
    assert "(proj v0.1)" in text  # the project's name without the pack folder's timestamp
    assert [value for _label, value in entry_choices(view.entries)] == ["rows-v1-bbbb", "proj-v1-aaaa"]


async def test_the_service_browses_and_forgets_library_entries(demo_service: Any) -> None:
    service, _graphs = demo_service
    root = get_settings().library_dir
    write_entry(root, entry(), forbidden=NOTHING)
    domains, text, entries = service.library_panel(None)
    assert domains["value"] == "python-fastapi" and entries["value"] == "proj-v1-aaaa" and "The tests import" in text
    domains, text, entries = service.forget_library_entry("python-fastapi", "proj-v1-aaaa")
    assert text.startswith("Forgot `proj-v1-aaaa`.") and entries["choices"] == [] and "No lessons yet." in text
    _domains, text, _entries = service.forget_library_entry("python-fastapi", None)
    assert text.startswith("Pick an entry first.")
