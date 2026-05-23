#!/usr/bin/env python3
"""Write a CLAUDE.md into a project that wires it up to the shared infoguana.

Usage:
    python init-project-infoguana.py <project-name> [target-dir]

If <target-dir> is omitted, writes into the current directory.
<project-name> should match what you want the infoguana to key notes on —
usually the cwd basename (e.g., "my-api").
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR.parent / "docs" / "CLAUDE.md.template"


def main() -> int:
    if len(sys.argv) < 2:
        print(
            f"usage: {Path(sys.argv[0]).name} <project-name> [target-dir]",
            file=sys.stderr,
        )
        return 2

    project = sys.argv[1]
    target_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()

    if not TEMPLATE.is_file():
        print(f"error: template not found at {TEMPLATE}", file=sys.stderr)
        return 1
    if not target_dir.is_dir():
        print(f"error: target dir does not exist: {target_dir}", file=sys.stderr)
        return 1

    dest = target_dir / "CLAUDE.md"
    if dest.exists():
        print(
            f"error: {dest} already exists. Remove it first or edit in place.",
            file=sys.stderr,
        )
        return 1

    body = TEMPLATE.read_text().replace("<PROJECT NAME>", project)
    # Rewrite the H1 (first line) to `# <project>`, preserving its trailing newline.
    lines = body.splitlines(keepends=True)
    if lines:
        first = lines[0]
        newline = "\n" if first.endswith("\n") else ""
        lines[0] = f"# {project}{newline}"
    dest.write_text("".join(lines))

    print(f"wrote {dest}")
    print()
    print("next steps:")
    print(f"  1. edit {dest} and replace the <One or two sentences…> line with a real description")
    print("  2. make sure ~/.claude/mcp.json has the `infoguana` server configured")
    print("  3. open this project in Claude Code — the agent will call context() on task start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
