#!/usr/bin/env python3
"""SessionStart hook: pulls infoguana's memory protocol + project context
for the current cwd's project and emits it as additionalContext.

NOT REGISTERED BY EITHER INSTALLER, and it cannot carry a real corpus.
A hook's inline `additionalContext` window is ~2KB; the protocol intro
and pinned rules alone are budget-exempt and already exceed that, so this
blob measures ~4.8KB at even a 300-token budget and grows from there. The
overflow does not error — it is truncated, or spilled to a file the agent
is merely handed a path to, so rules past the cut go missing from the
session that needed them and nothing says so.

Use infoguana-onboard-chunk.py, which both installers register: it splits
the same blob across N hooks that each get their own inline window. This
script is kept only for one-shot debugging of what build() produces.

The wire format is agent-neutral (Codex consumes the same
`hookSpecificOutput` shape), so the limitation above is the delivery
window, not the agent.

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
    # Raising this does not buy more delivered context — see the module
    # docstring. The blob is already several times the inline window at
    # any budget, so a larger number only moves the truncation point
    # further into content nobody receives. Left where it was rather than
    # tuned, because the fix for this path is the chunked hook.
    budget = os.environ.get("INFOGUANA_ONBOARD_BUDGET", "1500")
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
