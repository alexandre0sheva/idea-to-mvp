"""Opt-in LLM-as-judge for blueprint packs (spends tokens; never run by CI).

    uv run python scripts/eval_blueprint.py --live --idea climbing-log
    uv run python scripts/eval_blueprint.py --live --pack ~/idea-to-mvp/blueprints/<folder>
    DEMO_MODE=true uv run python scripts/eval_blueprint.py --live --all     # offline, canned scores
"""

import sys
from pathlib import Path

from idea_to_mvp.evals import main

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_ideas.json"

if __name__ == "__main__":
    sys.exit(main(golden_path=GOLDEN))
