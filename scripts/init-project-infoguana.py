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

import argparse
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
    # argparse rather than a hand-rolled scan: both `--agent codex` and
    # `--agent=codex` have to work, and hand-rolling that meant a branch
    # per spelling, each repeating the same membership check, each
    # mutating argv before the positionals were read. The space-only
    # version once swallowed `--agent=codex` as the target dir — failing
    # with "target dir does not exist: --agent=codex" while silently
    # defaulting the agent back to claude. `choices` covers both
    # spellings and the error message for free.
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Wire a repo up to the shared infoguana.")
    parser.add_argument("project",
                        help="what infoguana keys notes on, usually the "
                             "cwd basename")
    parser.add_argument("target_dir", nargs="?", default=None,
                        metavar="target-dir",
                        help="directory to write into (default: cwd)")
    parser.add_argument("--agent", choices=list(AGENT_FILES), default="claude",
                        help="which instruction file to write "
                             "(default: %(default)s)")
    args = parser.parse_args()

    project = args.project
    target_dir = Path(args.target_dir) if args.target_dir else Path.cwd()
    agent = args.agent

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
