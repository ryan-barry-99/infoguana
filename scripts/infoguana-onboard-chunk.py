#!/usr/bin/env python3
"""SessionStart hook for Claude Code: fetches one slice of the infoguana's
onboard blob and emits it as additionalContext.

Each hook's additionalContext is capped at ~2KB inline, but the cap is
*per-hook*. The installer registers N (default 16) entries of this script
with different chunk indices; all N slices land inline at session start
with no truncation, so the agent sees the full ~22KB blob without a
Read-tool round-trip.

Args:
    sys.argv[1]: chunk index (0-based)
    sys.argv[2]: total chunks (matches the of= query param)

Reads INFOGUANA_URL, INFOGUANA_TOKEN, INFOGUANA_ONBOARD_BUDGET from
~/.infoguana.env (preferred) or the env. Fails open: any error path
emits nothing rather than blocking the session.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Parse a simple shell-style .env file (KEY=VALUE, optional `export`,
    surrounding quotes, # comments) into os.environ without overriding
    existing vars. Cross-platform replacement for bash `source`."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main() -> int:
    if len(sys.argv) < 3:
        return 0
    try:
        index = int(sys.argv[1])
        total = int(sys.argv[2])
    except ValueError:
        return 0

    # Opt-out for callers that already seed context themselves (e.g. the
    # infoguana web-UI chat, which prepends a project-scoped seed before
    # spawning `claude -p`). Without this, the hook fires from the
    # service's cwd and injects `infoguana` memories on every chat.
    if os.environ.get("INFOGUANA_HOOK_DISABLE") == "1":
        return 0

    # Stagger by chunk index so the agent sees chunks in registration
    # order. The harness assembles hook outputs in completion order; with
    # server-side caching, the first chunk takes ~300ms (cold) and the
    # rest serve in ~5-10ms. 50ms-per-index stagger dominates that.
    if index > 0:
        time.sleep(index * 0.05)

    _load_env_file(Path.home() / ".infoguana.env")
    url = (os.environ.get("INFOGUANA_URL") or "http://localhost:8789").rstrip("/")
    token = os.environ.get("INFOGUANA_TOKEN", "")
    budget = os.environ.get("INFOGUANA_ONBOARD_BUDGET", "4000")
    if not token:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = Path(project_dir).name

    req = urllib.request.Request(
        f"{url}/onboard/{project}/chunk/{index}?of={total}&budget_tokens={budget}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            chunk = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0

    if not chunk:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": chunk,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
