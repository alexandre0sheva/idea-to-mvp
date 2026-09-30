"""Safe execution of the implementation agents: options, tool guard, and workspace preparation.

`options.py` builds every agent session's configuration in one place, `guard.py` is the tool-call
denylist that sits in front of the OS sandbox, and `workspace.py` prepares the git workspace. See
SECURITY.md for the threat model.
"""
