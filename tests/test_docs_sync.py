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
    for node in ("discussion", "summarizer", "architect", "strategy", "implementer", "delivery_report"):
        assert node in diagram
