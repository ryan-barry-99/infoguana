#!/usr/bin/env python3
"""SessionStart hook: pulls infoguana's memory protocol + project context
for the current cwd's project and emits it as additionalContext.

Works with both Claude Code and Codex — Codex consumes the same
`hookSpecificOutput` wire format, so one script serves either agent and
the same corpus follows you between them.

The project is taken from CLAUDE_PROJECT_DIR when set (Claude Code) and
otherwise from the process cwd, which is the workspace root under Codex.
Note that stdin is deliberately left unread: both agents pipe a JSON
payload to hooks, but blocking on an empty pipe would hang session start,
and cwd already resolves correctly under both.

Reads INFOGUANA_URL and INFOGUANA_TOKEN from ~/.infoguana.env (preferred)
or the env. Fails open: if infoguana is unreachable, silently emits
nothing so the session still starts cleanly.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _infoguana_agent import memory_override  # noqa: E402
from _infoguana_setup import authed_request  # noqa: E402
from _infoguana_setup import load_env_file as _load_env_file  # noqa: E402


def main() -> int:
    _load_env_file(Path.home() / ".infoguana.env")
    url = (os.environ.get("INFOGUANA_URL") or "http://localhost:8789").rstrip("/")
    token = os.environ.get("INFOGUANA_TOKEN", "")
    # 1500 was set when the corpus had few rules. Rules are pinned before
    # any note is considered, so once a project accumulates a handful of
    # them they consume the whole budget and the agent gets rules and zero
    # memories — the failure looks like an empty corpus rather than a
    # too-small budget. Chunked delivery (Claude Code) can afford more;
    # this single-shot path is capped by what one hook may return, so 6000
    # is a compromise. Raise INFOGUANA_ONBOARD_BUDGET if your host allows
    # a larger payload.
    budget = os.environ.get("INFOGUANA_ONBOARD_BUDGET", "6000")
    if not token:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = Path(project_dir).name

    req = authed_request(f"{url}/onboard/{project}?budget_tokens={budget}", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            context = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0

    if not context:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context + memory_override(),
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
