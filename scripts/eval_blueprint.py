"""Opt-in LLM-as-judge for blueprint packs (spends tokens; never run by CI).

uv run python scripts/eval_blueprint.py --live --idea climbing-log
uv run python scripts/eval_blueprint.py --live --pack ~/idea-to-mvp/blueprints/<folder>
DEMO_MODE=true uv run python scripts/eval_blueprint.py --live --all     # offline, canned scores
"""

import sys

from idea_to_mvp.bench import DEFAULT_IDEAS as GOLDEN
from idea_to_mvp.evals import main

if __name__ == "__main__":
    sys.exit(main(golden_path=GOLDEN))
