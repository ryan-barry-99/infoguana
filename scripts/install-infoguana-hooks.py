#!/usr/bin/env python3
"""Idempotent installer for the infoguana's Claude Code SessionStart hooks.

Registers N (default 16) entries of scripts/infoguana-onboard-chunk.py in
~/.claude/settings.json — each pinned to a different chunk index of the
project's onboard blob. Each hook's additionalContext is capped at ~2KB
inline but the cap is *per-hook*, so all N chunks land inline at session
start with no truncation.

Re-running is a no-op: any existing entries for this hook script are
stripped (matched by script path) and re-registered with the requested
count. Changing INFOGUANA_HOOK_CHUNKS removes the old entries and
registers the new count.

The hook command is registered as `{sys.executable} {abs_hook_path} i N`
— absolute paths to both the Python interpreter and the hook script, so
it works regardless of the user's PATH or cwd at session start.

Usage:
    python scripts/install-infoguana-hooks.py
    INFOGUANA_HOOK_CHUNKS=20 python scripts/install-infoguana-hooks.py

After install, set INFOGUANA_URL and INFOGUANA_TOKEN in ~/.infoguana.env
so the hooks can reach your running infoguana server.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.py"
LEGACY_BASH_HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.sh"
LEGACY_FIRST_TURN = REPO_DIR / "scripts" / "infoguana-first-turn.sh"
SETTINGS = Path.home() / ".claude" / "settings.json"


def _quote(s: str) -> str:
    """Quote a path/arg for inclusion in a shell command string. Handles
    both POSIX sh and Windows cmd.exe — both treat double-quoted strings
    as a single argument."""
    if any(c in s for c in (" ", "\t", '"', "'", "(", ")", "&", "|", ";")):
        return '"' + s.replace('"', r"\"") + '"'
    return s


def _build_command(index: int, total: int) -> str:
    return (
        f"{_quote(sys.executable)} {_quote(str(HOOK))} {index} {total}"
    )


def _strip_existing(entries: list[dict], match_substrings: list[str]) -> list[dict]:
    """Drop any inner hook whose command contains any of the match
    substrings. Preserve the entry wrapper if other hooks remain."""
    out: list[dict] = []
    for entry in entries:
        kept = [
            h for h in entry.get("hooks", [])
            if not any(sub in (h.get("command") or "") for sub in match_substrings)
        ]
        if kept:
            new_entry = dict(entry)
            new_entry["hooks"] = kept
            out.append(new_entry)
    return out


def main() -> int:
    if not HOOK.is_file():
        print(f"error: hook script not found at {HOOK}", file=sys.stderr)
        return 1

    try:
        n = int(os.environ.get("INFOGUANA_HOOK_CHUNKS", "16"))
    except ValueError:
        print("error: INFOGUANA_HOOK_CHUNKS must be an integer", file=sys.stderr)
        return 1

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if not SETTINGS.exists():
        SETTINGS.write_text("{}")

    raw = SETTINGS.read_text().strip() or "{}"
    data = json.loads(raw)
    hooks = data.setdefault("hooks", {})

    # Match by script path so any old entries (including the legacy bash
    # variant) get cleaned up on upgrade.
    match_substrings = [
        str(HOOK),
        str(LEGACY_BASH_HOOK),
        str(LEGACY_FIRST_TURN),
    ]
    for event in ("SessionStart", "UserPromptSubmit"):
        if event in hooks:
            hooks[event] = _strip_existing(hooks[event], match_substrings)

    ss = hooks.setdefault("SessionStart", [])
    for i in range(n):
        ss.append({
            "hooks": [{
                "type": "command",
                "command": _build_command(i, n),
            }]
        })

    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"registered {n} SessionStart hooks in {SETTINGS}")
    print(f"command template: {_build_command(0, n)}")
    print()
    print("Next steps:")
    print(f"  1. Make sure ~/.infoguana.env exports INFOGUANA_URL and INFOGUANA_TOKEN.")
    print(f"  2. Open a new Claude Code session in any project — its first system")
    print(f"     context will carry {n} inline chunks (~{n}x ~1.7KB ≈ {n*17//10}KB)")
    print(f"     of project-specific rules + plans + memories, no Read-tool round-trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
