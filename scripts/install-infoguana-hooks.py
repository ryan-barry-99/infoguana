#!/usr/bin/env python3
"""Idempotent installer for infoguana's Claude Code SessionStart hooks.

Does two things:

1. Auto-generates ~/.infoguana.env from the running container's
   data/.mcp_secret (token) and data/mcp.json (URL). Idempotent — if
   ~/.infoguana.env already exists, INFOGUANA_URL and INFOGUANA_TOKEN
   are refreshed in place and other lines are preserved.

2. Registers N (default 16) entries of scripts/infoguana-onboard-chunk.py
   in ~/.claude/settings.json — each pinned to a different chunk index
   of the project's onboard blob. Each hook's additionalContext is
   capped at ~2KB inline but the cap is *per-hook*, so all N chunks
   land inline at session start with no truncation.

Re-running is a no-op for unchanged state: existing entries for this
hook script are stripped (matched by script path) and re-registered
with the requested count. Changing INFOGUANA_HOOK_CHUNKS removes the
old entries and registers the new count.

The hook command is registered as `{sys.executable} {abs_hook_path} i N`
— absolute paths to both the Python interpreter and the hook script, so
it works regardless of the user's PATH or cwd at session start.

Usage:
    docker compose up -d --build
    python scripts/install-infoguana-hooks.py
    # then restart Claude Code

    INFOGUANA_HOOK_CHUNKS=20 python scripts/install-infoguana-hooks.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

REPO_DIR = Path(__file__).resolve().parent.parent
HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.py"
LEGACY_BASH_HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.sh"
LEGACY_FIRST_TURN = REPO_DIR / "scripts" / "infoguana-first-turn.sh"
SETTINGS = Path.home() / ".claude" / "settings.json"
MCP_SECRET_FILE = REPO_DIR / "data" / ".mcp_secret"
MCP_JSON_FILE = REPO_DIR / "data" / "mcp.json"
ENV_FILE = Path.home() / ".infoguana.env"


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


def _base_url_from_mcp_json() -> str:
    """Pull the http URL from data/mcp.json and strip the /mcp/ path
    suffix to get the server's base URL (used for /onboard/* requests).
    Honors INFOGUANA_PUBLIC_HOST overrides the user passed at container
    start time, since the entrypoint baked them into mcp.json."""
    mcp = json.loads(MCP_JSON_FILE.read_text())
    full_url = mcp["mcpServers"]["infoguana"]["url"]  # e.g. http://host:8789/mcp/
    parsed = urlparse(full_url)
    # Strip path entirely — onboard endpoints live at /onboard, not /mcp/onboard.
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _ensure_infoguana_env(token: str, base_url: str) -> str:
    """Create or update ~/.infoguana.env so the SessionStart hooks can
    reach the server. Preserves any other lines the user added (other
    env vars, comments). Returns a short status string for the log."""
    desired = {"INFOGUANA_URL": base_url, "INFOGUANA_TOKEN": token}
    preserved: list[str] = []
    existing: dict[str, str] = {}

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                preserved.append(line)
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in desired:
                existing[key] = value.strip()
                continue
            preserved.append(line)

    if all(existing.get(k) == v for k, v in desired.items()) and ENV_FILE.exists():
        return f"~/.infoguana.env already up-to-date ({ENV_FILE})"

    lines = preserved + [f"{k}={v}" for k, v in desired.items()]
    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n")
    try:
        ENV_FILE.chmod(0o600)  # POSIX only; no-op on Windows
    except OSError:
        pass

    if not existing:
        return f"created ~/.infoguana.env at {ENV_FILE}"
    return f"refreshed ~/.infoguana.env at {ENV_FILE}"


def main() -> int:
    if not HOOK.is_file():
        print(f"error: hook script not found at {HOOK}", file=sys.stderr)
        return 1
    if not MCP_SECRET_FILE.is_file() or not MCP_JSON_FILE.is_file():
        print(f"error: {MCP_SECRET_FILE.name} and/or {MCP_JSON_FILE.name} not found in {REPO_DIR / 'data'}.",
              file=sys.stderr)
        print("hint:  is the container running? (docker compose up -d --build)",
              file=sys.stderr)
        return 1

    try:
        n = int(os.environ.get("INFOGUANA_HOOK_CHUNKS", "16"))
    except ValueError:
        print("error: INFOGUANA_HOOK_CHUNKS must be an integer", file=sys.stderr)
        return 1

    try:
        token = MCP_SECRET_FILE.read_text().strip()
        base_url = _base_url_from_mcp_json()
    except PermissionError as e:
        # Old containers (pre-PUID/PGID entrypoint) wrote secret files as
        # root with chmod 600. The current entrypoint chowns to the host
        # user, so this only triggers on stale state from before the fix.
        print(f"error: can't read {e.filename or 'data/'} — owned by another user.",
              file=sys.stderr)
        print("hint:  rebuild + restart the container so the entrypoint can chown",
              file=sys.stderr)
        print("       host-side files to your UID/GID, or manually:", file=sys.stderr)
        print(f"           sudo chown -R $USER:$USER {REPO_DIR / 'data'}",
              file=sys.stderr)
        return 1
    env_status = _ensure_infoguana_env(token, base_url)

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
    print(env_status)
    print(f"registered {n} SessionStart hooks in {SETTINGS}")
    print(f"command template: {_build_command(0, n)}")
    print()
    print("All set. Open a new Claude Code session in any project — its first")
    print(f"system context will carry {n} inline chunks (~{n}x ~1.7KB ≈ {n*17//10}KB)")
    print("of project-specific rules + plans + memories, no Read-tool round-trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
