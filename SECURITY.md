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
| **Tool-call guard** (defence in depth) | A `PreToolUse` hook. File tools do not run in the OS sandbox, so it is their only check: paths are confined to the workspace with `realpath` (catches `..` and symlink escapes) and `.git`, `.idea-to-mvp/`, and the Claude Code settings files are read-only. For shell commands it refuses `sudo`, `ssh`/`scp`, `git push`, global git config, `curl ... \| sh`, recursive deletes outside the workspace, reading credentials or the environment of other processes, sending `env` over the network, and absolute paths outside the workspace (system and temp locations excepted) | `implementation/guard.py` |
| **Environment scrub** | Secret-looking variables (`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, ...) of the orchestrator process are blanked for the agent, except its own `ANTHROPIC_*` / `CLAUDE_*` access. Needed because the SDK merges the agent's environment over the inherited one | `implementation/options.py` |
| **Settings isolation** | No session loads the user's `~/.claude/settings.json`. Task, fix, and merge-resolver sessions load the workspace's *project* source for the pack's `CLAUDE.md` and skills; before each of them the orchestrator rewrites an existing `.claude/settings.json` to the fixed file the pack ships (no hooks, no permissions, project MCP servers not approved) and removes a symbolic link in its place, and the guard refuses agent writes to `.claude/settings*.json` and `.mcp.json`. Verification lanes load nothing (`setting_sources=[]`) | `implementation/options.py`, `harness_pack.py` |
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
  malicious package can do whatever the sandbox permits, including writing into the workspace. A starter template
  (`src/idea_to_mvp/templates/`) names its dependencies by version range, not by lock file, so the first install
  resolves whatever the registry then serves; the orchestrator writes the scaffold's files but never installs or
  runs them, the agents do, inside the sandbox.
- **The generated project is untrusted.** When you run it yourself, it has your full permissions. Read it
  first, or run it in a container.
- **Test reports are evidence, not proof.** The orchestrator parses the JUnit and coverage files the plan's
  `test_report` command leaves in `.idea-to-mvp/reports/` and lets them overrule the tests lane's claim (no
  report, no tests, or any failure fails the lane). The files are written by the project's own test command, so
  test code can write a green report without running anything, which no check on the file can tell apart. The
  folder is emptied before every tests lane, XML with a DOCTYPE or entity declaration and files over 20 MB are
  refused, and reading a report never runs agent code. A plan from before `test_report` existed has no such
  check, and the dashboard says its result is agent-reported.
- **Held-out acceptance tests are hidden, not secret.** The acceptance suite is written before any code, from the
  PRD and the plan's public contracts, and kept in `OUTPUT_DIR/acceptance/<workspace>/`, outside every workspace,
  so the file tools of task, resolver, and fix sessions (confined to their workspace by the guard) cannot open it,
  and fix sessions are given only failing test names and assertion messages. A shell command can still read
  outside the workspace where the guard's denylist does not catch it (a path built at run time inside `python -c`, an
  encoded string),
  and the folder's location follows a fixed pattern, so a determined agent can find the tests (the ledger does not
  name it). The lane that runs them works in a
  throwaway copy beside the workspace (no `.git`, no orchestrator files) inside the same OS sandbox as every
  session, and its trajectory, which shows the tests, is stored with the suite. Like the tests lane, its verdict
  is the JUnit report the suite's command writes, so project code that the tests import can forge it.
- **The test-integrity guard is a tripwire, not proof.** After every task, merge-resolver, and fix session the
  orchestrator compares the test files with their state before the session (a file read in the orchestrator, no
  project code runs) and, with `TEST_INTEGRITY=block` (default), fails the verdict until the change is undone or
  justified in `quality/TEST_CHANGES.md`. It catches the shortcuts: a deleted or renamed test file, new skip / xfail
  / `.only` markers in the common frameworks (pytest, unittest, Jest / Vitest / Mocha, Go, Rust, JUnit, xUnit,
  RSpec), assertion lines removed from a named test whose other lines did not change, and a drop in the test count
  of the test report. It does not catch an assertion made trivially true (`assert True`, an expected value changed
  to match the bug), a test rewritten to check less, skips through a mechanism it does not know (a custom decorator,
  a runner config that excludes files, a conditional `return`), test code that never calls the code it claims to
  test, or tests outside the files it recognises as tests. A justification is the agent's own word (and must be
  written after the change it justifies): the dashboard shows it so you can judge it. The findings are kept in the orchestrator's checkpoint, out of the agents' reach,
  except that a resumed run re-reads the findings of tasks finished before the stop from `progress.json`, which an
  agent's shell could edit.
- **The project harness is instructions, and agents can edit it.** A task session loads the workspace's
  `CLAUDE.md` and `.claude/skills/` as part of its instructions, and an earlier task session (or a resolver or fix
  session) could have rewritten them, as it could always rewrite `AGENTS.md`, which every task prompt asks the agent
  to read. So one session can steer the next; it cannot give the next session a hook or a tool permission, because
  the settings file is restored before every session that loads it (a shell write to it gets past the guard's
  denylist, not past the restore). The verification lanes load none of it, so agent-written text never becomes a
  judge's instructions. Claude Code also reads `CLAUDE.md` files in the folders above the session's folder; keep
  `OUTPUT_DIR` (outside the repository by default) free of them. The orchestrator never runs the pack's check
  script itself; the agents run it inside the sandbox.
- **Harness tuning writes model output, derived from agent trajectories, into the harness.** With
  `HARNESS_TUNING=pilot` the harness editor and composer read what pilot sessions did (tool output and file text an
  agent produced, possibly web text) and their edit becomes instructions, and possibly a helper script under
  `scripts/`, for every later session. Their prompts carry the evidence boundary, and the edit must pass the harness
  validator: harness paths only, never the check script (the one the verification lanes run) or
  `.claude/settings.json`, every base guardrail kept, at most four files. That limits an injected instruction to
  what a task session could already write into `AGENTS.md` or a skill; it runs only inside the sandbox, the
  orchestrator never runs a script it wrote, and the verification lanes load none of it. The pilot sessions run in
  throwaway worktrees under the same sandbox and guard as task sessions, and the test-integrity guard watches them.
- **A pilot build can become a task's result without being rebuilt.** The build selector reads agent-written diffs
  and test output, so its prompt carries the evidence boundary; an injected "choose me" can at most pick one of the
  builds, each made under the sandbox like any task, and the orchestrator never selects a build whose tests did not
  pass or whose session weakened the tests over a clean one. The selected build is applied with git by the
  orchestrator (hardened like a task merge; refused on a dirty tree, undone on a conflict) and carries its
  integrity findings into the verdict, so it is judged by the same verification as any other task.
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
