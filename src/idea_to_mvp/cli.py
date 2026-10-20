"""Command-line entry point: `idea-to-mvp` starts the UI, `run` / `resume` / `sessions` drive the pipeline
headless, `bench` scores it on the golden ideas, `doctor` checks the setup, `models` shows the model catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn


class _Parser(argparse.ArgumentParser):
    """A usage error exits 1 ("error"): argparse's own 2 means "delivered with failures" for `run`."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--answers", metavar="FILE", type=Path, help="answers to the panel's questions, one per line")
    parser.add_argument(
        "--autopilot",
        action="store_true",
        help="take the suggested answers and the recommended architecture, and generate the pack",
    )
    parser.add_argument("--arch", type=int, choices=(1, 2), help="the architecture to build: 1 = option A, 2 = option B")
    parser.add_argument(
        "--implement",
        action="store_true",
        help="pass the implement gate: build and verify with agent sessions (spends money); without it the run "
        "stops there and prints the cost estimate",
    )
    parser.add_argument(
        "--max-total-usd", type=_positive_float, metavar="N", help="whole-run agent budget (IMPLEMENTER_MAX_TOTAL_USD)"
    )
    parser.add_argument(
        "--parallel", type=_positive_int, metavar="N", help="tasks built at once (default: IMPLEMENTER_MAX_PARALLEL)"
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="idea-to-mvp", description="Take a product idea to a built, tested first version.")
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
    exits = "exit codes: 0 delivered and verified, 2 delivered with failures, 3 stopped at a gate, 1 error"
    run = commands.add_parser(
        "run",
        help="run the pipeline for an idea without the UI",
        description="Run the pipeline headless: progress on stderr, the summary on stdout.",
        epilog=exits,
    )
    run.add_argument("idea", help="the product idea")
    _add_run_flags(run)
    resume = commands.add_parser(
        "resume",
        help="carry a saved session on from its gate or last checkpoint",
        description="Continue a saved session (from the UI or `run`) with the same flags as `run`.",
        epilog=exits,
    )
    resume.add_argument("thread", help="the session id (see `idea-to-mvp sessions`)")
    _add_run_flags(resume)
    resume.add_argument(
        "--feedback", metavar="TEXT", help="at the iterate gate: the changes for the next version (needs --implement)"
    )
    sessions = commands.add_parser("sessions", help="list the saved sessions, newest first")
    sessions.add_argument("--limit", type=_positive_int, default=50, help="how many to list (default 50)")
    sessions.add_argument("--json", action="store_true", help="print the list as JSON")
    _add_bench(commands)
    return parser


def _add_bench(commands: Any) -> None:
    bench = commands.add_parser(
        "bench",
        help="run the golden ideas end to end and score the result (--compare puts runs side by side)",
        description="Run the golden ideas headless (autopilot, --implement) and record per idea: delivered, lanes "
        "passed, acceptance pass rate, integrity flags, fix attempts, cost, wall time. A run is a folder "
        "<OUTPUT_DIR>/bench/<name>/ with its own output root and a settings snapshot: calling again with the same "
        "name keeps the scored ideas, retries the unscored ones, and refuses other settings. Spends money "
        "unless DEMO_MODE=true: --live needs --max-total-usd.",
        epilog="exit codes: 0 every idea verified, 2 a delivery failed verification, 3 some ideas unscored "
        "(call again to retry them), 1 error",
    )
    bench.add_argument("--name", help="the run's folder name (required unless --compare or --dry-run)")
    bench.add_argument("--live", action="store_true", help="use the real providers and agent sessions (spends money)")
    bench.add_argument("--dry-run", action="store_true", help="print the settings and the estimated cost; run nothing")
    bench.add_argument("--ideas", metavar="ID,ID", help="only these golden ideas (default: all)")
    bench.add_argument("--ideas-file", type=Path, metavar="FILE", help="a golden ideas file (default: tests/fixtures/golden_ideas.json)")
    bench.add_argument("--root", type=Path, metavar="DIR", help="where runs are kept (default: <OUTPUT_DIR>/bench)")
    bench.add_argument("--profile", metavar="NAME", help="MODEL_PROFILE for this run: fast, balanced, or quality")
    bench.add_argument("--harness-tuning", metavar="MODE", help="HARNESS_TUNING for this run: off or pilot")
    bench.add_argument("--library", metavar="MODE", help="EXPERIENCE_LIBRARY for this run: on or off (its folder is the run's own)")
    bench.add_argument("--set", action="append", metavar="KEY=VALUE", help="any other setting for this run, e.g. MAX_FIX_ATTEMPTS=1 (repeatable)")
    bench.add_argument("--max-usd-per-idea", type=_positive_float, metavar="N", help="agent budget of one idea (IMPLEMENTER_MAX_TOTAL_USD)")
    bench.add_argument("--max-total-usd", type=_positive_float, metavar="N", help="cap on everything the run spends; ideas it cannot afford stay unscored")
    bench.add_argument("--compare", nargs="+", metavar="RUN", help="print these runs (names or folders) side by side on the ideas all of them scored")
    bench.add_argument("--json", action="store_true", help="print the result as JSON")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        from idea_to_mvp.config import get_settings
        from idea_to_mvp.doctor import run_doctor

        return run_doctor(get_settings(), offline=args.offline)

    if args.command == "models":
        from idea_to_mvp.config import get_settings
        from idea_to_mvp.model_report import run_models

        return run_models(get_settings(), check=args.check)

    if args.command in ("run", "resume"):
        return _run(args)

    if args.command == "bench":
        if not (args.name or args.compare or args.dry_run):
            parser.error("bench needs --name (the run's folder), or --compare, or --dry-run")
        from idea_to_mvp.bench import run_command

        return run_command(args)

    if args.command == "sessions":
        return _sessions(args)

    from idea_to_mvp.ui.app import main as launch_ui

    launch_ui()
    return 0


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _run(args: argparse.Namespace) -> int:
    from idea_to_mvp.config import clear_settings_cache, get_settings
    from idea_to_mvp.headless import HeadlessOptions, render_summary

    answers: str | None = None
    if args.answers is not None:
        try:
            answers = args.answers.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"idea-to-mvp: cannot read --answers: {exc}", file=sys.stderr)
            return 1
        if not answers.strip():
            print(f"idea-to-mvp: --answers {args.answers} is empty", file=sys.stderr)
            return 1
    if args.max_total_usd is not None:
        # The nodes read the budget from the settings, so the override goes where the settings come from.
        os.environ["IMPLEMENTER_MAX_TOTAL_USD"] = str(args.max_total_usd)
        clear_settings_cache()
    options = HeadlessOptions(
        answers=answers,
        autopilot=args.autopilot,
        arch="B" if args.arch == 2 else "A" if args.arch == 1 else None,
        implement=args.implement,
        parallel=args.parallel or 0,
        feedback=(getattr(args, "feedback", None) or "").strip() or None,
    )
    settings = get_settings()
    if settings.demo_mode:
        _progress("demo mode: canned models and a stand-in implementation, no API calls")
    summary = asyncio.run(_drive(args, options, settings))
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        print(render_summary(summary))
    return summary.exit_code


async def _drive(args: argparse.Namespace, options: Any, settings: Any) -> Any:
    from idea_to_mvp.graph import open_graph
    from idea_to_mvp.headless import resume_thread, run_idea
    from idea_to_mvp.observability import apply_tracing_env
    from idea_to_mvp.sessions import SessionRegistry

    apply_tracing_env()
    registry = SessionRegistry(settings.checkpoint_path)
    async with open_graph(settings) as graph:
        if args.command == "run":
            return await run_idea(graph, registry, args.idea, options, settings=settings, progress=_progress)
        return await resume_thread(graph, registry, args.thread, options, settings=settings, progress=_progress)


def _sessions(args: argparse.Namespace) -> int:
    from idea_to_mvp.config import get_settings
    from idea_to_mvp.headless import render_sessions, session_rows
    from idea_to_mvp.sessions import SessionRegistry

    rows = session_rows(SessionRegistry(get_settings().checkpoint_path).list(limit=args.limit))
    print(json.dumps(rows, indent=2) if args.json else render_sessions(rows))
    return 0
