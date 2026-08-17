"""Tests for the chunked SessionStart hook's opt-out.

`INFOGUANA_HOOK_DISABLE` exists for callers that seed context themselves
— the web-UI chat sets it before spawning `claude -p`, because otherwise
the hook resolves the project from the service's own cwd and injects
`infoguana` memories into every chat turn.

The hook is exercised as a subprocess rather than imported: it is a
script with a hyphenated filename, and what matters is what it writes to
stdout, since that is the entire interface the harness consumes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "infoguana-onboard-chunk.py"


def _run(args: list[str], **env_overrides) -> subprocess.CompletedProcess:
    """Run the hook with a clean environment plus the given overrides.

    The agent-identifying variables are cleared for the same reason as in
    test_agent_detection: the suite inherits whichever agent invoked it,
    and the override text varies by agent.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("INFOGUANA_AGENT", "CLAUDECODE", "CLAUDE_PROJECT_DIR",
                        "CODEX_HOME", "CODEX_SANDBOX", "INFOGUANA_HOOK_DISABLE")}
    env.update(env_overrides)
    return subprocess.run([sys.executable, str(HOOK), *args],
                          capture_output=True, text=True, timeout=30, env=env)


@pytest.mark.parametrize("args", [["--override"], ["0", "4"]])
def test_hook_disable_suppresses_every_mode(args):
    """Both the override slice and a context slice must stay silent.

    The override branch used to return before the check ran, so a caller
    that asked for silence still got ~1KB of directive text on every
    turn — invisible, because the caller only ever inspected the slices.
    """
    r = _run(args, INFOGUANA_HOOK_DISABLE="1")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_the_override_slice_is_emitted_when_not_disabled():
    """The guard must not be so eager that it suppresses normal use —
    otherwise the test above passes against a hook that does nothing."""
    r = _run(["--override"])
    assert r.returncode == 0
    assert "hookSpecificOutput" in r.stdout
    assert "authoritative persistent memory" in r.stdout
