"""Agent trajectories: what every session did, kept on disk as `<workspace>/.idea-to-mvp/trajectories/<label>.md`.

One grammar for every session. After a short header, a file is a sequence of entries, each starting at the
beginning of a line with its tag; the lines of a longer entry are indented by two spaces, so text an agent or
a tool produced can never start an entry of its own:

    [ACTION] Bash {"command": "pytest -q"}      a tool call: the tool and its (clipped) input as JSON
    [OBSERVATION] 3 passed                      the tool's result, truncated; "(error)" when the tool failed
    [TEXT] I'll start with the model layer.     what the agent said
    [ERROR] RuntimeError: agent process died    the session or a tool broke
    [RESULT] success · $0.5000 · 12 turns · session abc123

The file is written while the session runs, so a killed run leaves the evidence up to that point; the
`[RESULT]` line and the ledger row (`ledger.py`) are written when the session ends. Thinking blocks are not
kept. The console falls back to these files after a restart (`events_from_trajectories`).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from idea_to_mvp.implementation.events import ImplEvent, make_event, shorten, tool_detail
from idea_to_mvp.implementation.ledger import (
    CALL_KINDS,
    LEDGER_DIR,
    LedgerRow,
    SessionKind,
    SessionOutcome,
    append_row,
    load_ledger,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
    from idea_to_mvp.model_catalog import AgentRole

TRAJECTORY_DIR = "trajectories"
TEXT_LIMIT = 4000
OBSERVATION_LIMIT = 1500
INPUT_VALUE_LIMIT = 500
TAGS = ("ACTION", "OBSERVATION", "TEXT", "ERROR", "RESULT")

_ENTRY = re.compile(r"^\[(ACTION|OBSERVATION|TEXT|ERROR|RESULT)\](?: (.*))?$")
_INDENT = "  "
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LANE_LABEL = re.compile(r"lane-([a-z]+)")
_PILOT_LABEL = re.compile(r"pilot-([a-z]+)")


def model_label(settings: Settings, role: AgentRole) -> str:
    """The model a session of kind `role` runs on, for the ledger ("demo" when no model is called)."""
    if settings.demo_mode:
        return "demo"
    from idea_to_mvp.roles import resolve_agent_model

    return resolve_agent_model(settings, role)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [truncated {len(text) - limit} characters]"


def _clip_values(value: Any, limit: int) -> Any:
    """A copy of a tool input with every long string cut, so the JSON stays valid however big the input was."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return f"{value[:limit]}… [truncated {len(value) - limit} characters]"
    if isinstance(value, dict):
        return {key: _clip_values(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_values(item, limit) for item in value]
    return value


def _safe_label(label: str) -> str:
    cleaned = _UNSAFE.sub("_", label).strip("._") or "session"
    return cleaned[:80]


def _entry(tag: str, text: str) -> str:
    first, *rest = text.rstrip("\n").split("\n")
    return "\n".join([f"[{tag}] {first}".rstrip(), *(_INDENT + line for line in rest)]) + "\n\n"


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
    return "\n".join(part for part in parts if part)


class SessionLog:
    """The trajectory file and the ledger row of one agent session.

    Nothing touches the disk until the session produces its first entry (or ends), so a session that could
    not start leaves no file. The label is made unique within the folder: a second session of a task whose
    first attempt failed is `T01-2`.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        kind: SessionKind,
        label: str,
        task_id: str | None = None,
        iteration: int = 1,
        attempt: int | None = None,
        model: str = "",
        trajectory_dir: Path | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        # A session whose trajectory agents must not read (the acceptance lane's shows the held-out tests) keeps it
        # elsewhere; its ledger row is still the workspace's, so the spend adds up.
        self._folder = Path(trajectory_dir) if trajectory_dir is not None else self._workspace / LEDGER_DIR / TRAJECTORY_DIR
        self._base = _safe_label(label)
        self._kind: SessionKind = kind
        self._task_id = task_id
        self._iteration = iteration
        self._model = model
        self._attempt = attempt if attempt is not None else 1 + sum(
            1 for row in load_ledger(self._workspace) if row["kind"] == kind and row["task_id"] == task_id
        )
        self._started = time.time()
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        """The trajectory file, once it exists."""
        return self._path

    @property
    def label(self) -> str:
        return self._path.stem if self._path else self._base

    # ------------------------------------------------------------------ writing

    def _open(self) -> Path:
        if self._path is not None:
            return self._path
        folder = self._folder
        folder.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Trajectory: {self._base} ({self._kind})\n\n"
            f"model {self._model or 'unknown'} · iteration {self._iteration} · attempt {self._attempt} · "
            f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._started))}\n\n"
        )
        index = 1
        while True:
            path = folder / (f"{self._base}.md" if index == 1 else f"{self._base}-{index}.md")
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(header)
            except FileExistsError:
                index += 1
                continue
            self._path = path
            return path

    def _write(self, tag: str, text: str) -> None:
        with self._open().open("a", encoding="utf-8") as stream:
            stream.write(_entry(tag, text))

    def message(self, message: Any) -> None:
        """Record one SDK message: the agent's text and tool calls, and the tools' results."""
        from claude_agent_sdk import (
            AssistantMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    payload = json.dumps(_clip_values(block.input, INPUT_VALUE_LIMIT), ensure_ascii=False)
                    self._write("ACTION", f"{block.name} {payload}")
                elif isinstance(block, TextBlock) and block.text.strip():
                    self._write("TEXT", _clip(block.text.strip(), TEXT_LIMIT))
            if message.error:
                self._write("ERROR", f"The model call failed: {message.error}")
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    text = _clip(_result_text(block.content).strip(), OBSERVATION_LIMIT)
                    self._write("OBSERVATION", f"(error) {text}".rstrip() if block.is_error else text)

    def event(self, event: ImplEvent) -> None:
        """Record a simulated session's event (demo mode has no SDK messages) in the same grammar."""
        if event["kind"] == "tool":
            self._write("ACTION", f"{event['label']} {json.dumps({'detail': event['detail']}, ensure_ascii=False)}")
        elif event["kind"] == "text":
            self._write("TEXT", event["detail"])

    def tee(self, emit: Callable[[ImplEvent], None]) -> Callable[[ImplEvent], None]:
        """An `emit` that records every event here and still passes it on."""

        def recording(event: ImplEvent) -> None:
            self.event(event)
            emit(event)

        return recording

    def error(self, text: str) -> None:
        self._write("ERROR", _clip(text, TEXT_LIMIT))

    def finish(
        self,
        *,
        outcome: SessionOutcome,
        cost_usd: float,
        turns: int,
        session_id: str | None,
        note: str = "",
    ) -> LedgerRow:
        """Close the file with its `[RESULT]` line and add the session to the ledger."""
        if note:
            self.error(note)
        facts = [outcome, f"${cost_usd:.4f}", f"{turns} turns"]
        if session_id:
            facts.append(f"session {session_id}")
        self._write("RESULT", " · ".join(facts))
        path = self._open()
        row: LedgerRow = {
            "kind": self._kind,
            "label": path.stem,
            "task_id": self._task_id,
            "iteration": self._iteration,
            "attempt": self._attempt,
            "model": self._model,
            "cost_usd": float(cost_usd),
            "turns": int(turns),
            "outcome": outcome,
            "session_id": session_id,
            # Agents may read the ledger: a trajectory kept outside the workspace (the acceptance lane's, next to the
            # held-out suite) is not named in it.
            "trajectory": path.relative_to(self._workspace).as_posix() if path.is_relative_to(self._workspace) else "",
            "started_at": self._started,
            "ended_at": max(time.time(), self._started),
        }
        append_row(self._workspace, row)
        return row


# ------------------------------------------------------------------------ reading


def parse_trajectory(text: str) -> list[tuple[str, str]]:
    """The `(tag, text)` entries of a trajectory file; the header before the first entry is skipped."""
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = _ENTRY.match(line)
        if found:
            entries.append((found.group(1), [found.group(2) or ""]))
        elif entries and line.startswith(_INDENT):
            entries[-1][1].append(line[len(_INDENT) :])
    return [(tag, "\n".join(lines).strip("\n")) for tag, lines in entries]


def pilot_arm(label: str) -> str:
    """The harness a pilot session ran under (`base`, `inv`, `dom`), from its label (`pilot-inv`, `pilot-inv-tests`)."""
    found = _PILOT_LABEL.search(label)
    return found.group(1) if found else "base"


def _task_id_of(row: LedgerRow) -> str | None:
    """The console's name for a session: the plan task, `verify:<lane>` / `verify:fix`, or `pilot:<arm>`."""
    if row["kind"] == "pilot":
        return f"pilot:{pilot_arm(row['label'])}"
    if row["kind"] == "pilot_lane":
        return f"pilot:{pilot_arm(row['label'])}:tests"
    if row["kind"] == "lane":
        found = _LANE_LABEL.search(row["label"])
        return f"verify:{found.group(1)}" if found else "verify:lane"
    if row["kind"] == "fix":
        return "verify:fix"
    return row["task_id"]


def _title_of(row: LedgerRow, titles: Mapping[str, str]) -> str:
    if row["kind"] == "pilot":
        return f"Pilot {row['task_id']} ({pilot_arm(row['label'])} harness)"
    if row["kind"] == "pilot_lane":
        return f"Tests lane (pilot {pilot_arm(row['label'])})"
    if row["kind"] == "lane":
        found = _LANE_LABEL.search(row["label"])
        return f"{found.group(1).capitalize()} lane" if found else "Lane"
    if row["kind"] == "fix":
        return "Fix"
    if row["kind"] == "resolver":
        return f"Merge resolver for {row['task_id']}"
    return titles.get(row["task_id"] or "") or row["task_id"] or row["label"]


def _action_event(text: str, task_id: str | None, ts: float) -> ImplEvent:
    name, _, payload = text.partition(" ")
    try:
        detail = tool_detail(json.loads(payload))
    except ValueError:
        detail = shorten(payload)
    return {**make_event("tool", task_id=task_id, label=name, detail=detail), "ts": ts}


def events_from_trajectories(
    workspace: Path, limit: int = 2000, titles: Mapping[str, str] | None = None
) -> list[ImplEvent]:
    """The console's events rebuilt from the ledger and the trajectory files, oldest first (the last `limit`).

    `titles` maps a plan task to its title (the ledger only knows ids). A session becomes `task_start`, its text and tool calls, its cost, and `task_end`. The files carry no
    per-entry times, so a session's entries all show the time it started.
    """
    workspace = Path(workspace)
    titles = titles or {}
    events: list[ImplEvent] = []
    for row in load_ledger(workspace):
        if row["kind"] in CALL_KINDS:
            continue  # a model call, not a session: nothing for the console
        task_id = _task_id_of(row)
        started, ended = row["started_at"], row["ended_at"]
        events.append({**make_event("task_start", task_id=task_id, label=_title_of(row, titles)), "ts": started})
        try:
            text = (workspace / row["trajectory"]).read_text(encoding="utf-8")
        except OSError:
            text = ""
        for tag, body in parse_trajectory(text):
            if tag == "ACTION":
                events.append(_action_event(body, task_id, started))
            elif tag == "TEXT":
                events.append({**make_event("text", task_id=task_id, label="agent", detail=shorten(body, 240)), "ts": started})
        if row["cost_usd"] > 0:
            detail = f"{row['turns']} turns"
            events.append({**make_event("cost", task_id=task_id, label="session", detail=detail, cost_usd=row["cost_usd"]), "ts": ended})
        detail = "done" if row["outcome"] == "success" else row["outcome"]
        events.append({**make_event("task_end", task_id=task_id, label=_title_of(row, titles), detail=detail), "ts": ended})
    return events[-limit:] if limit > 0 else []
