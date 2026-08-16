#!/usr/bin/env python3
"""Idempotent installer for infoguana's Claude Code SessionStart hooks.

For Codex, run scripts/install-infoguana-codex.py instead — both agents
talk to the same server and the same corpus, so installing both is a
supported setup and lets you switch agents freely.

Does two things:

1. Auto-generates ~/.infoguana.env with the server's URL + bearer token
   (see _infoguana_setup.resolve_credentials for where those come from).
   Idempotent — if ~/.infoguana.env already exists, INFOGUANA_URL and
   INFOGUANA_TOKEN are refreshed in place and other lines are preserved.

2. Registers N entries of scripts/infoguana-onboard-chunk.py in
   ~/.claude/settings.json — each pinned to a different chunk index
   of the project's onboard blob. Each hook's additionalContext is
   capped at ~2KB inline but the cap is *per-hook*, so all N chunks
   land inline at session start with no truncation.

N is measured, not hardcoded: the installer asks /onboard/sizing for
the largest project's blob and registers enough chunks to keep every
slice under the inline cap. Hardcoding is what broke it before — 16 was
right for a ~22KB blob and silently wrong once the largest reached
~59KB, at which point each slice ran ~2x over cap and lost its tail
mid-rule. Set INFOGUANA_HOOK_CHUNKS to override (1..128); the installer
prints any project that still won't fit.

Re-running is a no-op for unchanged state: existing entries for any
infoguana hook script are stripped (matched by script *name*, so a moved
or duplicated checkout's entries are replaced rather than left to
accumulate alongside) and re-registered with the resolved count. A
changed count removes the old entries and registers the new ones, so
re-run this after the corpus grows.

If the config already registers hooks from a different checkout, the
installer asks before repointing them, and refuses outright when there
is no TTY — pass --force (or --yes) to replace them unattended.

The hook command is registered as `{sys.executable} {abs_hook_path} i N`
— absolute paths to both the Python interpreter and the hook script, so
it works regardless of the user's PATH or cwd at session start.

Usage:
    docker compose up -d --build
    python scripts/install-infoguana-hooks.py
    # then restart Claude Code

    INFOGUANA_HOOK_CHUNKS=20 python scripts/install-infoguana-hooks.py
    python scripts/install-infoguana-hooks.py --force   # unattended
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _infoguana_setup import (  # noqa: E402
    FALLBACK_CHUNKS,
    MAX_CHUNKS,
    atomic_write,
    confirm_replacement,
    ensure_infoguana_env,
    is_infoguana_hook,
    other_install_dirs,
    parse_chunk_override,
    quote,
    resolve_chunks,
    resolve_credentials,
)

REPO_DIR = Path(__file__).resolve().parent.parent
HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.py"
SETTINGS = Path.home() / ".claude" / "settings.json"



def _build_command(index: int, total: int) -> str:
    return (
        f"{quote(sys.executable)} {quote(str(HOOK))} {index} {total}"
    )


def _strip_existing(entries: list[dict]) -> list[dict]:
    """Drop any inner hook that runs an infoguana hook script, from any
    checkout. Preserve the entry wrapper if other hooks remain.

    Matching by script name rather than by this checkout's absolute path
    is what makes a re-install idempotent across a moved or duplicated
    repo: path-matching left the old entries in place and appended a full
    second set beside them.
    """
    out: list[dict] = []
    for entry in entries:
        kept = [
            h for h in entry.get("hooks", [])
            if not is_infoguana_hook(h.get("command") or "")
        ]
        if kept:
            new_entry = dict(entry)
            new_entry["hooks"] = kept
            out.append(new_entry)
    return out


def _registered_commands(hooks: dict) -> list[str]:
    """Every hook command currently registered, across all events."""
    return [h.get("command") or ""
            for event in hooks.values() if isinstance(event, list)
            for entry in event
            for h in entry.get("hooks", [])]


def main() -> int:
    force = "--force" in sys.argv or "--yes" in sys.argv
    if not HOOK.is_file():
        print(f"error: hook script not found at {HOOK}", file=sys.stderr)
        return 1

    # Validated before any credential or network work, so a typo fails
    # immediately instead of after a server round-trip.
    try:
        override = parse_chunk_override(os.environ.get("INFOGUANA_HOOK_CHUNKS"))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        token, base_url = resolve_credentials(REPO_DIR)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
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
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if not SETTINGS.exists():
        SETTINGS.write_text("{}")

    raw = SETTINGS.read_text().strip() or "{}"
    data = json.loads(raw)
    hooks = data.setdefault("hooks", {})

    # An integration already pointing at a different checkout is replaced
    # only with the user's say-so — it may be the one they actually use.
    #
    # Asked *before* ensure_infoguana_env, and the order is load-bearing:
    # ~/.infoguana.env lives in $HOME and is shared by every checkout, so
    # writing it first meant a refused install had already repointed the
    # other checkout's still-registered hooks at this server with this
    # bearer. That is the same takeover the guard exists to prevent, just
    # via the credential instead of the registration — and the installer
    # said it had refused while it happened.
    others = other_install_dirs(_registered_commands(hooks), HOOK.parent)
    if not confirm_replacement(SETTINGS, others, HOOK.parent, force):
        return 1

    env_status = ensure_infoguana_env(token, base_url)

    n, sizing = resolve_chunks(base_url, token, override,
                               lambda m: print(m, file=sys.stderr))

    for event in ("SessionStart", "UserPromptSubmit"):
        if event in hooks:
            hooks[event] = _strip_existing(hooks[event])

    ss = hooks.setdefault("SessionStart", [])
    for i in range(n):
        ss.append({
            "hooks": [{
                "type": "command",
                "command": _build_command(i, n),
            }]
        })
    # One more entry for the memory-system override. It gets its own hook
    # rather than being folded into a slice so it can't push that slice
    # over the inline cap, and so the sizing search doesn't have to model
    # a client-side addition it never sees. Matched by the same script
    # path as the chunk hooks, so re-running still strips it cleanly.
    ss.append({
        "hooks": [{
            "type": "command",
            "command": f"{quote(sys.executable)} {quote(str(HOOK))} --override",
        }]
    })

    # Atomic: this file holds the user's permissions allowlist, env vars
    # and statusline config, none of which this project can regenerate.
    # A truncated write also breaks the *next* run, since json.loads on
    # the remains raises before any of our error handling is reached.
    atomic_write(SETTINGS, json.dumps(data, indent=2) + "\n")
    print(env_status)
    print(f"registered {n} SessionStart hooks in {SETTINGS}")
    print(f"command template: {_build_command(0, n)}")
    if sizing:
        target = sizing["chunk_target_bytes"]
        biggest = sizing["projects"][0] if sizing["projects"] else None
        if biggest:
            print(f"derived from largest blob: {biggest['project']} "
                  f"({biggest['bytes']} B) / {target} B per chunk")
        # Name the projects that would still lose content, so a corpus
        # that has outgrown even the chunk route's ceiling is stated outright
        # rather than left to show up as garbled context later. Compares
        # the server's measured `needed`, not a byte estimate — the two
        # disagree, and the estimate is the optimistic one.
        over = [
            p for p in sizing["projects"]
            if p.get("widest_at_recommended", 0) > target
        ]
        if over:
            print()
            print(f"warning: {len(over)} project(s) still split over "
                  f"{target} B at {n} chunks and may be truncated:")
            for p in over[:5]:
                print(f"    {p['project']}: {p['bytes']} B, worst slice "
                      f"{p['widest_at_recommended']} B")
            print(f"    Trim the pinned rule set for these projects — "
                  f"{MAX_CHUNKS} hook")
            print("    entries is the chunk route's ceiling.")
    elif override is None:
        print(f"note: count is the {FALLBACK_CHUNKS}-chunk fallback, not measured")
    print()
    print("All set. Open a new Claude Code session in any project — its first")
    print(f"system context will carry {n} inline chunks (~{n}x ~1.7KB ≈ {n*17//10}KB)")
    print("of project-specific rules + plans + memories, no Read-tool round-trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
