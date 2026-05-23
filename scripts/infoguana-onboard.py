#!/usr/bin/env python3
"""SessionStart hook for Claude Code: pulls the infoguana's memory
protocol + project context for the current cwd's project, and emits the
JSON shape Claude Code expects (additionalContext).

Reads INFOGUANA_URL and INFOGUANA_TOKEN from ~/.infoguana.env (preferred)
or the env. Fails open: if the infoguana is unreachable, silently emits
nothing so the session still starts cleanly.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MEMORY_OVERRIDE = """
# memory system override

The user uses the `infoguana` MCP server as their authoritative persistent memory — NOT the built-in Claude Code auto-memory at `~/.claude/projects/<project>/memory/`.

- Do NOT write to `~/.claude/projects/*/memory/*.md` or create/update `MEMORY.md` files there.
- Save everything worth retaining (user facts, feedback, project context, references) via `add(content=..., project=<this>)`.
- If you see an existing `~/.claude/projects/*/memory/` directory, treat it as legacy — do not extend it. Migrate substance to the infoguana if still relevant.
"""


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
    _load_env_file(Path.home() / ".infoguana.env")
    url = (os.environ.get("INFOGUANA_URL") or "http://localhost:8789").rstrip("/")
    token = os.environ.get("INFOGUANA_TOKEN", "")
    budget = os.environ.get("INFOGUANA_ONBOARD_BUDGET", "1500")
    if not token:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = Path(project_dir).name

    req = urllib.request.Request(
        f"{url}/onboard/{project}?budget_tokens={budget}",
        headers={"Authorization": f"Bearer {token}"},
    )
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
            "additionalContext": context + MEMORY_OVERRIDE,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
