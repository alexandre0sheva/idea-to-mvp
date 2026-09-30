"""PreToolUse guard for implementation agents: a **defence-in-depth denylist**.

The OS sandbox (Seatbelt / bubblewrap, see `options.py`) is the primary control for shell commands. This
guard is the second layer and the *only* tool-level path check: file tools (`Read`, `Write`, `Edit`, ...)
do not run inside the sandbox, so their paths are confined to the workspace here (`realpath`, so `..` and
symlink escapes are caught), and obviously dangerous shell commands are refused before they run.

A denylist cannot be complete: a determined command can always be rephrased (`python -c`, base64, ...).
It catches the mistakes and the copy-pasted attack one-liners; it is not a substitute for the sandbox or,
for untrusted ideas, a container or VM (SECURITY.md).
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# Tools whose input names a path, and the input keys that hold it. The bool marks tools that modify files.
_PATH_TOOLS: dict[str, tuple[tuple[str, ...], bool, bool]] = {
    # tool: (input keys, required, writes)
    "Read": (("file_path",), True, False),
    "Write": (("file_path",), True, True),
    "Edit": (("file_path",), True, True),
    "MultiEdit": (("file_path",), True, True),
    "NotebookEdit": (("notebook_path",), True, True),
    "Grep": (("path",), False, False),
    "LS": (("path",), False, False),
    "Glob": (("path",), False, False),
}
GUARDED_TOOLS = "Bash|" + "|".join(_PATH_TOOLS)  # the hook matcher

# Directories the orchestrator owns: agents may read them but never write them.
_PROTECTED_DIRS = (".git", ".idea-to-mvp")
_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders")
# Read-mostly system locations shell commands legitimately name (interpreters, certificates, devices).
_SYSTEM_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/opt", "/etc", "/private/etc", "/nix", "/snap",
    "/System", "/Library", "/Applications",
    "/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom", "/dev/random",
)  # fmt: skip
_PATH_SAFE = re.compile(r"[\w.@%+=:,~/-]+")
_GLOB_CHARS = "*?[{"

_SENSITIVE_LOCATIONS = re.compile(
    r"(?<![\w.-])\.(ssh|aws|gnupg|kube|azure)(?![\w-])"
    r"|\.config/(gh|gcloud)\b|\.netrc\b|\.git-credentials\b|\.docker/config\.json"
    r"|\bid_(rsa|ed25519|ecdsa|dsa)\b"
    r"|(~|\$HOME|\$\{HOME\})/\.(npmrc|pypirc)\b",
    re.IGNORECASE,
)
_DOTENV = re.compile(r"^\.env(\.(?!example$|sample$|template$|dist$)[\w.-]+)?$")

_WHOLE_COMMAND_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bsudo\b|(^|[;&|(])\s*(doas|pkexec)\b", "privilege escalation (sudo) is not allowed"),
        (
            r"\b(curl|wget|fetch)\b[^\n;&]*\|\s*(sudo\s+)?(env\s+)?(sh|bash|zsh|dash|ksh|fish|python3?|perl|ruby|node|php)\b",
            "piping a download into an interpreter is not allowed",
        ),
        (
            r"\b(sh|bash|zsh|dash|source)\s+<\(\s*(curl|wget)\b|\beval\b[^\n;&]*\$\(\s*(curl|wget)\b",
            "executing a downloaded script is not allowed",
        ),
        (
            r"\b(printenv|env|set|declare\s+-x|export\s+-p)\b[^\n|;&]*\|[^\n]*"
            r"\b(curl|wget|nc|ncat|netcat|socat|telnet|openssl|ssh|scp)\b",
            "sending the environment over the network is not allowed",
        ),
        (r"/proc/[^/\s]+/environ", "reading another process's environment is not allowed"),
        (_SENSITIVE_LOCATIONS.pattern, "credential locations are off limits"),
        (r"(^|[\s/])\.git/(config|hooks)\b", "the git configuration and hooks are managed by the orchestrator"),
        (
            r"\bcore\.(sshcommand|fsmonitor|hookspath|askpass|pager|editor)\s*=",
            "git settings that execute programs are not allowed",
        ),
    )
)

_BLOCKED_COMMANDS = {
    "sudo": "privilege escalation is not allowed",
    "doas": "privilege escalation is not allowed",
    "su": "privilege escalation is not allowed",
    "pkexec": "privilege escalation is not allowed",
    "ssh": "remote shells are not allowed",
    "scp": "remote copies are not allowed",
    "sftp": "remote copies are not allowed",
    "ssh-add": "SSH keys are off limits",
    "ssh-keygen": "SSH keys are off limits",
    "ssh-copy-id": "SSH keys are off limits",
    "nc": "raw network tools are not allowed",
    "ncat": "raw network tools are not allowed",
    "netcat": "raw network tools are not allowed",
    "socat": "raw network tools are not allowed",
    "telnet": "raw network tools are not allowed",
    "gh": "the GitHub CLI is not allowed (no pushing or publishing from agents)",
}
_WRAPPERS = {"time", "command", "nohup", "exec", "nice", "builtin", "stdbuf", "xargs"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_GIT_SENSITIVE_CONFIG = re.compile(r"^(core\.|filter\.|credential\.|alias\.|url\.|include)", re.IGNORECASE)
_MAX_NESTING = 3


# ------------------------------------------------------------------- helpers


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real(raw: str, workspace: Path) -> str:
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(workspace, expanded)
    return os.path.realpath(expanded)


def _workspace_root(workspace: Path) -> str:
    return os.path.realpath(workspace)


def _glob_prefix(pattern: str) -> str:
    cut = min((pattern.find(char) for char in _GLOB_CHARS if char in pattern), default=len(pattern))
    return pattern[:cut]


# ---------------------------------------------------------------- file tools


def _check_path_tool(tool_name: str, tool_input: Mapping[str, Any], workspace: Path) -> tuple[bool, str]:
    keys, required, writes = _PATH_TOOLS[tool_name]
    root = _workspace_root(workspace)
    candidates: list[str] = []
    for key in keys:
        value = tool_input.get(key)
        if value is None and not required:
            continue
        if not isinstance(value, str) or not value.strip():
            return False, f"{tool_name} needs a non-empty {key}"
        candidates.append(value)
    if tool_name == "Glob" and isinstance(tool_input.get("pattern"), str):
        candidates.append(_glob_prefix(tool_input["pattern"]) or ".")
    for raw in candidates:
        real = _real(raw, workspace)
        if not _inside(real, root):
            return False, f"{tool_name} path {raw!r} is outside the workspace"
        if writes:
            for protected in _PROTECTED_DIRS:
                if protected in Path(os.path.relpath(real, root)).parts:
                    return False, f"{tool_name} may not modify {protected} (the orchestrator manages it)"
    return True, ""


# ---------------------------------------------------------------------- bash


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:  # unbalanced quotes from naive splitting
        return segment.split()


def _expand_home_vars(token: str) -> str:
    home = os.path.expanduser("~")
    return token.replace("${HOME}", home).replace("$HOME", home)


def _path_candidate(token: str) -> str | None:
    """The path a shell word refers to, if it looks like one (handles `2>/dev/null`, `--out=/x`)."""
    word = re.sub(r"^\d*[<>]+&?", "", token)
    if word.startswith("-") and "=" in word:
        word = word.split("=", 1)[1]
    word = _expand_home_vars(word)
    if not word or "://" in word:
        return None
    if word.startswith(("/", "~")) or ".." in word.split("/"):
        return word
    return None


def _is_system_path(real: str) -> bool:
    roots = {*_TEMP_ROOTS, *_SYSTEM_ROOTS}
    roots |= {os.path.realpath(root) for root in roots}
    return any(_inside(real, root) for root in roots)


def _names_an_existing_location(word: str, real: str) -> bool:
    """Whether an absolute-looking word is plausibly a path and not a regex or URL fragment such as
    '/api/v1': it must use only path characters and start in a top-level directory that exists."""
    if not _PATH_SAFE.fullmatch(word):
        return False
    top = "/" + real.lstrip("/").split("/", 1)[0]
    return os.path.lexists(top)


def _check_path_word(word: str, workspace: Path) -> str | None:
    root = _workspace_root(workspace)
    home = os.path.realpath(os.path.expanduser("~"))
    real = _real(word, workspace)
    basename = os.path.basename(real.rstrip("/"))
    outside = not _inside(real, root)
    if outside and _DOTENV.match(basename):
        return f"reading {basename} outside the workspace is not allowed"
    if outside and _inside(real, home) and home != "/":
        return f"{word!r} is in your home directory outside the workspace"
    if (
        outside
        and word.startswith(("/", "~"))
        and _names_an_existing_location(word, real)
        and not _is_system_path(real)
    ):
        return f"{word!r} is outside the workspace; use paths inside it"
    return None


def _check_rm(args: list[str], workspace: Path) -> str | None:
    flags = [a for a in args if a.startswith("-")]
    recursive = any(a in ("--recursive",) or (not a.startswith("--") and re.search(r"[rR]", a)) for a in flags)
    if not recursive:
        return None
    root = _workspace_root(workspace)
    for target in (a for a in args if not a.startswith("-")):
        word = _expand_home_vars(target)
        if word in ("/", "/*", "~", "~/") or word.startswith(("/", "~")) or ".." in word.split("/"):
            real = _real(word, workspace)
            in_temp = word.startswith("/") and ".." not in word.split("/") and any(
                _inside(real, temp) for temp in _TEMP_ROOTS
            )  # only a literal temp path: `../..` that merely resolves into one is still an escape
            if not (_inside(real, root) or in_temp):
                return f"recursive delete of {target!r} reaches outside the workspace"
    return None


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, args[index + 1 :]
    return None, []


def _check_git(args: list[str]) -> str | None:
    sub, rest = _git_subcommand(args)
    if sub == "push":
        return "git push is not allowed (agents never publish; review and push yourself)"
    if sub == "credential":
        return "git credential helpers are off limits"
    if sub == "config":
        if any(flag in rest for flag in ("--global", "--system", "--file", "-f", "--worktree")):
            return "changing global or system git configuration is not allowed"
        keys = [a for a in rest if not a.startswith("-")]
        if keys and _GIT_SENSITIVE_CONFIG.match(keys[0]):
            return f"git setting {keys[0]!r} can execute programs and is managed by the orchestrator"
    return None


def _check_segment(tokens: list[str], workspace: Path, depth: int) -> str | None:
    index = 0
    while index < len(tokens) and (re.match(r"^[A-Za-z_]\w*=", tokens[index]) or tokens[index] in _WRAPPERS):
        index += 1
    if index >= len(tokens):
        return None
    name = os.path.basename(tokens[index])
    args = tokens[index + 1 :]
    if name in _BLOCKED_COMMANDS:
        return _BLOCKED_COMMANDS[name]
    if name in _SHELLS and "-c" in args and args.index("-c") + 1 < len(args):
        if depth >= _MAX_NESTING:
            return "deeply nested shell commands are not allowed"
        nested = _check_command(args[args.index("-c") + 1], workspace, depth + 1)
        if nested:
            return nested
    if name == "git" and (reason := _check_git(args)) is not None:
        return reason
    if name == "rm" and (reason := _check_rm(args, workspace)) is not None:
        return reason
    for token in tokens[index + 1 :]:
        word = _path_candidate(token)
        if word is not None and (reason := _check_path_word(word, workspace)) is not None:
            return reason
    return None


def _check_command(command: str, workspace: Path, depth: int = 0) -> str | None:
    for pattern, rule_reason in _WHOLE_COMMAND_RULES:
        if pattern.search(command):
            return rule_reason
    for segment in re.split(r"\|\||&&|[;|&\n]", command):
        if segment_reason := _check_segment(_tokens(segment), workspace, depth):
            return segment_reason
    return None


# --------------------------------------------------------------------- public


def check_tool_use(tool_name: str, tool_input: Any, workspace: Path) -> tuple[bool, str]:
    """(allowed, reason) for one tool call. Tools the guard does not know are left to the normal
    permission system; malformed input to a guarded tool is denied."""
    if not isinstance(tool_input, dict):
        return False, "malformed tool input"
    if tool_name in _PATH_TOOLS:
        return _check_path_tool(tool_name, tool_input, workspace)
    if tool_name == "Bash":
        if tool_input.get("dangerouslyDisableSandbox"):
            return False, "running a command outside the sandbox is not allowed"
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return False, "Bash needs a non-empty command"
        reason = _check_command(command, workspace)
        return (False, reason) if reason else (True, "")
    return True, ""


async def pre_tool_use_hook(
    input_data: Mapping[str, Any],
    tool_use_id: str | None,
    context: Any,
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Claude Agent SDK `PreToolUse` hook around `check_tool_use`; fails closed on any error.

    `workspace` is bound by `bind_workspace` (the hook's `cwd` is where the agent's shell currently is,
    which is not necessarily the workspace root); it falls back to the hook input's `cwd`.
    """
    try:
        cwd = input_data.get("cwd")
        root = workspace or (Path(str(cwd)) if cwd else None)
        if root is None:
            raise ValueError("no workspace to check against")
        allowed, reason = check_tool_use(str(input_data["tool_name"]), input_data.get("tool_input"), root)
    except Exception as exc:  # a guard that crashes must not let the call through
        allowed, reason = False, f"guard error: {exc}"
    if allowed:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by the idea-to-mvp guard: {reason}",
        }
    }


def bind_workspace(workspace: Path) -> Callable[..., Any]:
    """The hook callback for one session, bound to its workspace."""

    async def hook(input_data: Mapping[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        return await pre_tool_use_hook(input_data, tool_use_id, context, workspace=workspace)

    return hook
