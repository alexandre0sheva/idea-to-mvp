"""Turn a finished run into a folder under `examples/` that is safe to publish.

    uv run python scripts/make_example.py --thread <session> --name climbing-log [--screenshot dashboard.png]
    uv run python scripts/make_example.py --demo "a habit tracker" --name habit-tracker

`--thread` reads a delivered session from the configured checkpoint (a live run you started with the UI, `run`, or
`bench`; point `OUTPUT_DIR` / `CHECKPOINT_DB` at it). `--demo` runs the offline demo pipeline for an idea in a
throwaway folder first. Either way the example holds the blueprint pack, the delivery report, the session export
(Markdown and JSON), an optional dashboard screenshot you took, and a README that says where it came from. Nothing
from the project workspace or the orchestrator's `.idea-to-mvp/` records is copied, and the export stops if any file
holds something shaped like a key, a secret of this process, or a line of the held-out acceptance tests.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from idea_to_mvp.config import Settings, clear_settings_cache, get_settings
from idea_to_mvp.exporter import build_session_json, build_session_markdown
from idea_to_mvp.graph import ThreadView, open_graph, read_thread
from idea_to_mvp.headless import HeadlessOptions, run_idea
from idea_to_mvp.library import Forbidden, forbidden_for, leaks
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.ui.view import mode_from_state, transcript_from_state

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TEXT_SUFFIXES = {".md", ".json", ".txt", ".toml", ".yaml", ".yml", ".py", ".ts", ".js", ".sh", ""}


class ExampleError(Exception):
    """Something the user can fix; the message says what."""


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ExampleError(
            f"The name {name!r} must be a plain folder name: lowercase letters, digits, and dashes."
        )
    return name


def check_for_secrets(folder: Path, forbidden: Forbidden) -> None:
    """Refuse to publish a folder with a key-shaped string, a secret of this process, or hidden test lines."""
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if problems := leaks(text, forbidden):
            raise ExampleError(
                f"{path.relative_to(folder)} was not published: {'; '.join(problems)}."
            )


def _verdict(values: dict[str, Any]) -> tuple[str, str]:
    passed = bool((values.get("verification") or {}).get("passed"))
    lanes = (values.get("verification") or {}).get("lanes") or []
    detail = ", ".join(
        f"{lane.get('lane', '?')} {'passed' if lane.get('passed') else 'failed'}" for lane in lanes
    )
    return ("passed" if passed else "did not pass"), detail


def _readme(
    values: dict[str, Any], *, source: str, name: str, has_screenshot: bool, note: str = ""
) -> str:
    verdict, lanes = _verdict(values)
    usage = values.get("usage") or []
    cost = sum(float(record.get("cost_usd") or 0) for record in usage)
    kind = "a demo run (canned models, no API calls)" if source == "demo" else "a live run"
    lines = [
        f"# Example: {name}",
        "",
        f"> {values.get('user_idea', '')}",
        "",
        f"This folder is {kind} of idea-to-mvp, version v0.{int(values.get('iteration') or 1)}. Verification {verdict}"
        + (f" ({lanes})." if lanes else "."),
        f"Recorded model spend: ${cost:.2f}." + (f" {note}" if note else ""),
        "",
        "| File | What it is |",
        "|---|---|",
        "| `blueprint/` | the blueprint pack the agents were given: PRD, architecture, plan, guides, agent harness |",
        "| `DELIVERY_REPORT.md` | the delivery report: verdict per lane, requirements, how to run the project |",
        "| `session.md`, `session.json` | the whole session as the chat showed it, from the idea to the report |",
    ]
    if has_screenshot:
        lines += [
            "| `dashboard.png` | the delivery dashboard |",
            "",
            "![The delivery dashboard](dashboard.png)",
        ]
    return "\n".join(lines) + "\n"


def _blueprint_filter(_folder: str, names: list[str]) -> list[str]:
    return [name for name in names if name in {".git", ".idea-to-mvp", "__pycache__", ".env"}]


def build_example(
    values: dict[str, Any],
    view: ThreadView,
    *,
    thread_id: str,
    name: str,
    source: str,
    examples_dir: Path,
    forbidden: Forbidden,
    screenshot: Path | None = None,
    force: bool = False,
    note: str = "",
) -> Path:
    """Write the example folder for a delivered session and return it."""
    check_name(name)
    if not values.get("delivery_report"):
        raise ExampleError("The session has no delivery report yet: finish a run first.")
    blueprint = Path(str(values.get("project_bundle_dir") or ""))
    if not blueprint.is_dir():
        raise ExampleError(f"The blueprint folder {str(blueprint)!r} is gone.")
    folder = examples_dir / name
    if folder.exists() and not force:
        raise ExampleError(f"{folder} already exists: pass --force to replace it.")
    staging = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    try:
        shutil.copytree(blueprint, staging / "blueprint", ignore=_blueprint_filter)
        (staging / "DELIVERY_REPORT.md").write_text(
            str(values["delivery_report"]).rstrip() + "\n", encoding="utf-8"
        )
        entries = transcript_from_state(values, view.interrupt)
        mode = mode_from_state(values, view.interrupt, next_nodes=view.next_nodes)
        (staging / "session.md").write_text(
            build_session_markdown(entries=entries, thread_id=thread_id, mode=mode),
            encoding="utf-8",
        )
        (staging / "session.json").write_text(
            build_session_json(entries=entries, thread_id=thread_id, mode=mode), encoding="utf-8"
        )
        if screenshot is not None:
            shutil.copyfile(screenshot, staging / "dashboard.png")
        (staging / "README.md").write_text(
            _readme(
                values, source=source, name=name, has_screenshot=screenshot is not None, note=note
            ),
            encoding="utf-8",
        )
        check_for_secrets(staging, forbidden)
        examples_dir.mkdir(parents=True, exist_ok=True)
        if folder.exists():
            shutil.rmtree(folder)
        shutil.move(str(staging), folder)
        folder.chmod(0o755)  # mkdtemp makes it private
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return folder


async def make_thread_example(
    thread_id: str,
    name: str,
    *,
    examples_dir: Path,
    settings: Settings,
    screenshot: Path | None = None,
    force: bool = False,
    note: str = "",
) -> Path:
    async with open_graph(settings) as graph:
        view = await read_thread(graph, thread_id)
    if not view.values.get("user_idea"):
        raise ExampleError(f"No saved session {thread_id!r}: see `idea-to-mvp sessions`.")
    workspace = Path(str(view.values.get("workspace_dir") or "."))
    return build_example(
        view.values,
        view,
        thread_id=thread_id,
        name=name,
        source="live" if not settings.demo_mode else "demo",
        examples_dir=examples_dir,
        forbidden=forbidden_for(settings, workspace),
        screenshot=screenshot,
        force=force,
        note=note,
    )


async def make_demo_example(
    idea: str,
    name: str,
    *,
    examples_dir: Path,
    settings: Settings,
    screenshot: Path | None = None,
    force: bool = False,
) -> Path:
    """Run the offline demo pipeline for `idea` (autopilot, implement) and make an example of the delivery."""
    check_name(name)
    if not settings.demo_mode:
        raise ExampleError(
            "--demo needs DEMO_MODE=true (it is set for you when you run the script)."
        )
    options = HeadlessOptions(autopilot=True, implement=True)
    async with open_graph(settings) as graph:
        summary = await run_idea(
            graph,
            SessionRegistry(settings.checkpoint_path),
            idea,
            options,
            settings=settings,
            progress=lambda _line: None,
        )
        view = await read_thread(graph, summary.thread_id)
    if not view.values.get("delivery_report"):
        raise ExampleError(f"The demo run did not deliver: {summary.message}")
    return build_example(
        view.values,
        view,
        thread_id=summary.thread_id,
        name=name,
        source="demo",
        examples_dir=examples_dir,
        forbidden=Forbidden(),
        screenshot=screenshot,
        force=force,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--thread", help="a delivered session id (see `idea-to-mvp sessions`)")
    source.add_argument(
        "--demo", metavar="IDEA", help="run the offline demo pipeline for this idea first"
    )
    parser.add_argument(
        "--name", required=True, help="the folder under examples/ (lowercase, digits, dashes)"
    )
    parser.add_argument(
        "--screenshot", type=Path, help="a screenshot of the delivery dashboard to include"
    )
    parser.add_argument(
        "--examples-dir",
        type=Path,
        default=EXAMPLES_DIR,
        help="default: examples/ in the repository",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing example")
    parser.add_argument(
        "--note",
        default="",
        help="one sentence for the example's README (the profile, the settings)",
    )
    args = parser.parse_args(argv)

    if args.screenshot is not None and not args.screenshot.is_file():
        print(f"make_example: no such screenshot {args.screenshot}", file=sys.stderr)
        return 1
    scratch: Path | None = None
    if args.demo:  # a throwaway output root, so a demo run never touches the user's sessions
        scratch = Path(tempfile.mkdtemp(prefix="idea-to-mvp-demo-"))
        os.environ.update({"DEMO_MODE": "true", "OUTPUT_DIR": str(scratch)})
        os.environ.pop("CHECKPOINT_DB", None)
        clear_settings_cache()
    try:
        settings = get_settings()
        if args.demo:
            made = asyncio.run(
                make_demo_example(
                    args.demo,
                    args.name,
                    examples_dir=args.examples_dir,
                    settings=settings,
                    screenshot=args.screenshot,
                    force=args.force,
                )
            )
        else:
            made = asyncio.run(
                make_thread_example(
                    args.thread,
                    args.name,
                    examples_dir=args.examples_dir,
                    settings=settings,
                    screenshot=args.screenshot,
                    force=args.force,
                    note=args.note,
                )
            )
    except ExampleError as error:
        print(f"make_example: {error}", file=sys.stderr)
        return 1
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    print(f"Wrote {made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
