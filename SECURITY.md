# Security Policy

## Supported Versions

This project currently supports the latest `main` branch.

## Reporting a Vulnerability

Please do not open public issues for sensitive vulnerabilities.

- Report privately to the project maintainer.
- Include reproduction steps, impact, and affected configuration.
- If the report includes API keys or secrets, rotate them before sharing logs.

The maintainer will acknowledge receipt and provide remediation status updates.

## Threat model: the implementation agents

The implementation stage hands a product idea to autonomous coding agents (Claude Agent SDK) that write
files and run shell commands on your machine, then runs the project they built. Treat everything in that
stage as **untrusted code execution**: the idea text, the model's output, packages it installs, and the
generated project itself can all carry a prompt injection or malware. The agents are confined in layers;
none is sufficient alone.

### What confines an agent

| Layer | What it does | Where |
|---|---|---|
| **OS sandbox** (primary control for shell commands) | Seatbelt (macOS) or bubblewrap + socat (Linux): shell commands can write only inside the workspace, reach only the allowed package-registry domains (`IMPLEMENTER_ALLOWED_DOMAINS`), cannot read credential directories (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.kube`, `~/.netrc`, ...) or the orchestrator's `.env`, and cannot opt out with `dangerouslyDisableSandbox` | `implementation/options.py` |
| **Tool-call guard** (defence in depth) | A `PreToolUse` hook. File tools do not run in the OS sandbox, so it is their only check: paths are confined to the workspace with `realpath` (catches `..` and symlink escapes) and `.git` is read-only. For shell commands it refuses `sudo`, `ssh`/`scp`, `git push`, global git config, `curl ... \| sh`, recursive deletes outside the workspace, reading credentials or the environment of other processes, sending `env` over the network, and absolute paths outside the workspace (system and temp locations excepted) | `implementation/guard.py` |
| **Environment scrub** | Secret-looking variables (`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, ...) of the orchestrator process are blanked for the agent, except its own `ANTHROPIC_*` / `CLAUDE_*` access. Needed because the SDK merges the agent's environment over the inherited one | `implementation/options.py` |
| **Settings isolation** | `setting_sources=[]`: the agent never loads `~/.claude/settings.json` or a `.claude/settings.json` that an earlier agent wrote into the workspace | `implementation/options.py` |
| **No web tools** | `WebFetch` and `WebSearch` are disabled (a URL can carry data out, and they are outside the sandbox's network allowlist) | `implementation/options.py` |
| **Hardened git** | Workspaces are git repositories, committed to by the orchestrator only. Because it runs git outside the sandbox on a tree an agent has written to, git runs with hooks, fsmonitor, filters, and user/global configuration neutralised, the repository config is reset before each commit, and a `.git` that is not a plain directory is refused | `implementation/workspace.py` |
| **Worktree isolation** | With parallel execution each task runs in its own git worktree beside the workspace; that folder is the session's sandbox root and guard root. The orchestrator never trusts the worktree's `.git` file (an agent can rewrite it): task commits pin `--git-dir` to the worktree's admin directory inside the main repository, and the main repository's config is reset before every merge. Project test commands are never run on the host | `implementation/merge.py` |
| **Budget cap** | `IMPLEMENTER_MAX_TOTAL_USD` for the whole run (shared by all sessions, surviving restarts), `IMPLEMENTER_MAX_TASK_USD` and `IMPLEMENTER_MAX_TASK_TURNS` per task session | `implementation/executor.py` |

The implement gate states the sandbox status, permission mode, and allowed domains truthfully, and shows a
red warning when the sandbox is off or unavailable or the permission mode is `bypassPermissions`.

### Settings that change the picture

- `IMPLEMENTER_SANDBOX`: `auto` (default) enables the sandbox where the platform supports it and otherwise
  logs a loud warning and relies on the guard alone; `on` refuses to run without it; `off` disables it.
- `IMPLEMENTER_PERMISSION_MODE`: `acceptEdits` (default) auto-approves edits, and shell commands while the
  sandbox is on. With the sandbox off, shell commands need approval a headless run cannot give, so agents
  cannot run tests until you choose `bypassPermissions`, which approves every tool unattended.
- An existing `.env` copied from an older `.env.example` may still set
  `IMPLEMENTER_PERMISSION_MODE=bypassPermissions`; that overrides the safer default.

### What is not protected

- **Reads are not confined at the OS level.** The sandbox denies a list of credential locations; it does not
  deny reading everything else the user can read. Shell reads outside the workspace are refused only by the
  guard's denylist, which a determined command can rephrase around (`python -c`, encodings, ...).
- **A denylist is not complete.** The guard catches mistakes and copy-pasted attack one-liners.
- **The network allowlist covers sandboxed shell commands only**, and the allowed domains (package
  registries, `github.com`) can themselves host or receive data.
- **Dependencies run as code.** Installing packages executes their install scripts inside the sandbox; a
  malicious package can do whatever the sandbox permits, including writing into the workspace.
- **The generated project is untrusted.** When you run it yourself, it has your full permissions. Read it
  first, or run it in a container.
- **Model API keys stay in the orchestrator process**, and `OUTPUT_DIR/sessions.db` holds every session's
  ideas and transcript; neither is reachable by agents through the sandbox's credential list.
- **Residual git risk:** an agent could plant a `.gitattributes` file or other in-tree content aimed at
  git itself; the orchestrator's hardening neutralises the known vectors (hooks, fsmonitor, filters) but
  the workspace should still not be treated as trustworthy.
- **Platform:** on Windows, and on Linux without bubblewrap and socat, no OS sandbox is available.

### Recommendations

- For ideas or prompts you do not fully trust, or whenever the sandbox is off: run `idea-to-mvp` inside a
  container or VM with only the API keys it needs, no access to your home directory, and restricted egress.
- Keep `IMPLEMENTER_SANDBOX=on` where you can, so a missing sandbox stops the run instead of degrading it.
- Review the blueprint pack (and `REVIEW.md` when present) before approving the implement gate.

### Verifying the confinement

The unit tests cover the guard, the options, and the workspace hardening. Whether a real agent is stopped is
checked by opt-in smoke tests that spend a little Anthropic money:

```bash
uv run pytest -m live tests/test_agent_sandbox_live.py -v
```
