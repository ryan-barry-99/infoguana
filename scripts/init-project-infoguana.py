#!/usr/bin/env python3
"""Write a project instruction file that wires a repo up to the shared infoguana.

Usage:
    python init-project-infoguana.py <project-name> [target-dir] [--agent NAME]

If <target-dir> is omitted, writes into the current directory.
<project-name> should match what you want infoguana to key notes on —
usually the cwd basename (e.g., "my-api").

The filename follows the agent, since each reads a different one: Claude
Code reads CLAUDE.md, Codex reads AGENTS.md. The body is identical — it
only points at infoguana — so `--agent both` writes the pair for a repo
whose contributors use different agents.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR.parent / "docs" / "CLAUDE.md.template"

# The instruction file each agent actually reads. Writing the wrong name
# is silent: the agent simply never loads it, and the project looks like
# it has no infoguana wiring at all.
AGENT_FILES = {"claude": ["CLAUDE.md"], "codex": ["AGENTS.md"],
               "both": ["CLAUDE.md", "AGENTS.md"]}


def main() -> int:
    argv = sys.argv[1:]
    agent = "claude"
    # Both spellings, because `--agent=codex` is standard for long options
    # and the space-only form swallowed it as the positional target dir —
    # failing with "target dir does not exist: --agent=codex" while
    # silently defaulting the agent back to claude.
    inline = [a for a in argv if a.startswith("--agent=")]
    if inline:
        value = inline[0].split("=", 1)[1]
        if value not in AGENT_FILES:
            print(f"error: --agent must be one of {', '.join(AGENT_FILES)}",
                  file=sys.stderr)
            return 2
        agent = value
        argv = [a for a in argv if not a.startswith("--agent=")]
    elif "--agent" in argv:
        i = argv.index("--agent")
        if i + 1 >= len(argv) or argv[i + 1] not in AGENT_FILES:
            print(f"error: --agent must be one of {', '.join(AGENT_FILES)}",
                  file=sys.stderr)
            return 2
        agent = argv[i + 1]
        del argv[i:i + 2]

    if not argv:
        print(
            f"usage: {Path(sys.argv[0]).name} <project-name> [target-dir] "
            f"[--agent {'|'.join(AGENT_FILES)}]",
            file=sys.stderr,
        )
        return 2

    project = argv[0]
    target_dir = Path(argv[1]) if len(argv) > 1 else Path.cwd()

    if not TEMPLATE.is_file():
        print(f"error: template not found at {TEMPLATE}", file=sys.stderr)
        return 1
    if not target_dir.is_dir():
        print(f"error: target dir does not exist: {target_dir}", file=sys.stderr)
        return 1

    dests = [target_dir / name for name in AGENT_FILES[agent]]
    # Checked before writing any of them, so `--agent both` cannot leave
    # one file written and the other refused.
    for dest in dests:
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
    for dest in dests:
        dest.write_text("".join(lines))
        print(f"wrote {dest}")

    written = " and ".join(str(d) for d in dests)
    print()
    print("next steps:")
    print(f"  1. edit {written} and replace the <One or two sentences…> line "
          f"with a real description")
    print("  2. make sure your agent has the `infoguana` MCP server configured")
    print("  3. open this project in your agent — it will call context() on task start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
