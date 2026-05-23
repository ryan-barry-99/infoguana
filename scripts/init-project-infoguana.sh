#!/usr/bin/env bash
# Write a CLAUDE.md into a project that wires it up to the shared infoguana.
#
# Usage:
#   init-project-infoguana.sh <project-name> [target-dir]
#
# If <target-dir> is omitted, writes into the current directory.
# <project-name> should match what you want the infoguana to key notes on —
# usually the cwd basename (e.g., "my-api").

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $(basename "$0") <project-name> [target-dir]" >&2
  exit 2
fi

PROJECT="$1"
TARGET_DIR="${2:-$(pwd)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../docs/CLAUDE.md.template"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template not found at $TEMPLATE" >&2
  exit 1
fi

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "error: target dir does not exist: $TARGET_DIR" >&2
  exit 1
fi

DEST="$TARGET_DIR/CLAUDE.md"

if [[ -e "$DEST" ]]; then
  echo "error: $DEST already exists. Remove it first or edit in place." >&2
  exit 1
fi

# Substitute the project name, keep the top-of-file description placeholder
# so the user can fill it in (the shell refuses to guess what the repo does).
sed "s|<PROJECT NAME>|${PROJECT}|g" "$TEMPLATE" > "$DEST"

# Rename the H1 header specifically (first line).
sed -i "1s|.*|# ${PROJECT}|" "$DEST"

echo "wrote $DEST"
echo
echo "next steps:"
echo "  1. edit $DEST and replace the <One or two sentences…> line with a real description"
echo "  2. make sure ~/.claude/mcp.json has the \`infoguana\` server configured"
echo "  3. open this project in Claude Code — the agent will call context() on task start"
