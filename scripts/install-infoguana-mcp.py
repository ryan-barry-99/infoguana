#!/usr/bin/env python3
"""Idempotent installer: merges infoguana's generated mcp.json into the
user's Claude Code config (~/.claude.json -> mcpServers.infoguana).

The container entrypoint writes ./data/mcp.json on every start with the
current bearer + URL. Re-running this script picks up secret rotations
without touching any of the user's other mcpServers entries.

Usage:
    docker compose up -d --build
    python scripts/install-infoguana-mcp.py

Override the URL host in the generated snippet via:
    INFOGUANA_PUBLIC_HOST=infoguana.example.com docker compose up -d --build
(then re-run this installer).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC = REPO_DIR / "data" / "mcp.json"
DEST = Path.home() / ".claude.json"


def main() -> int:
    # The entrypoint writes ./data/mcp.json before exec'ing the server.
    # If the stack was just brought up, give it a couple seconds to land.
    for _ in range(5):
        if SRC.is_file():
            break
        time.sleep(1)

    if not SRC.is_file():
        print(f"error: {SRC} not found.", file=sys.stderr)
        print("hint:  is the container running? (docker compose up -d --build)",
              file=sys.stderr)
        return 1

    try:
        src = json.loads(SRC.read_text())
    except PermissionError:
        # Stale state from pre-PUID/PGID entrypoint (pre-this-fix) — secret
        # files were written root-owned with chmod 600. Current entrypoint
        # chowns to the host user on every run, so a container restart fixes
        # this; otherwise chown manually.
        print(f"error: can't read {SRC} — owned by another user.", file=sys.stderr)
        print("hint:  rebuild + restart the container, or manually:", file=sys.stderr)
        print(f"           sudo chown -R $USER:$USER {SRC.parent}", file=sys.stderr)
        return 1
    infoguana_entry = src.get("mcpServers", {}).get("infoguana")
    if not infoguana_entry:
        print(f"error: {SRC} has no mcpServers.infoguana block", file=sys.stderr)
        return 1

    if not DEST.exists():
        DEST.write_text("{}")

    raw = DEST.read_text().strip() or "{}"
    dest = json.loads(raw)
    servers = dest.setdefault("mcpServers", {})

    prev = servers.get("infoguana")
    servers["infoguana"] = infoguana_entry
    DEST.write_text(json.dumps(dest, indent=2) + "\n")

    url = infoguana_entry.get("url", "?")
    if prev == infoguana_entry:
        print(f"infoguana MCP already up-to-date in {DEST} ({url})")
    elif prev is None:
        print(f"installed infoguana MCP in {DEST} ({url})")
    else:
        print(f"updated infoguana MCP in {DEST} ({url})")

    print()
    print("Next: restart any open Claude Code sessions, then test with /mcp list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
