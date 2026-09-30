import importlib.util
from pathlib import Path

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
