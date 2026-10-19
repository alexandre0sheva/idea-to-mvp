"""Command-line entry point: `idea-to-mvp` starts the UI, `doctor` checks the setup, `models` shows the model catalog."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="idea-to-mvp", description="Take a product idea to a built, tested first version."
    )
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="check API keys, models, and the agent CLI")
    doctor.add_argument(
        "--offline", action="store_true", help="only check configuration; make no model calls"
    )
    models = commands.add_parser("models", help="show every profile's models, prices, and the cost of a typical run")
    models.add_argument(
        "--check",
        action="store_true",
        help="also list each provider's available models (one live call per provider) and flag unknown, "
        "deprecated, or outdated configured models",
    )
    args = parser.parse_args(argv)

    if args.command == "doctor":
        from idea_to_mvp.config import get_settings
        from idea_to_mvp.doctor import run_doctor

        return run_doctor(get_settings(), offline=args.offline)

    if args.command == "models":
        from idea_to_mvp.config import get_settings
        from idea_to_mvp.model_report import run_models

        return run_models(get_settings(), check=args.check)

    from idea_to_mvp.ui.app import main as launch_ui

    launch_ui()
    return 0
