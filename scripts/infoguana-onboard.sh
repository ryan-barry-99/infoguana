#!/usr/bin/env bash
# SessionStart hook for Claude Code: pulls the infoguana's memory protocol +
# project context for the current cwd's project, and emits the JSON shape
# Claude Code expects (additionalContext).
#
# Reads INFOGUANA_URL and INFOGUANA_TOKEN from ~/.infoguana.env (preferred) or the env.
# Fails open: if the infoguana is unreachable, silently emits nothing so the
# session still starts cleanly.

set -euo pipefail

# ----- config -----
if [[ -f "${HOME}/.infoguana.env" ]]; then
  # shellcheck disable=SC1091
  source "${HOME}/.infoguana.env"
fi

URL="${INFOGUANA_URL:-http://localhost:8789}"
TOKEN="${INFOGUANA_TOKEN:-}"
BUDGET="${INFOGUANA_ONBOARD_BUDGET:-1500}"

# ----- project name -----
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT="$(basename "$PROJECT_DIR")"

# ----- fetch context -----
if [[ -z "$TOKEN" ]]; then
  exit 0
fi
context=$(curl -fsS --max-time 5 \
  -H "Authorization: Bearer $TOKEN" \
  "${URL%/}/onboard/${PROJECT}?budget_tokens=${BUDGET}" \
  2>/dev/null || true)

if [[ -z "$context" ]]; then
  exit 0
fi

# ----- memory-system override -----
# Claude Code ships with a built-in "auto memory" protocol that writes notes to
# ~/.claude/projects/<project>/memory/. The user prefers the infoguana MCP for all
# persistent memory, so override that default here.
override=$(cat <<'EOF'

# memory system override

The user uses the `infoguana` MCP server as their authoritative persistent memory — NOT the built-in Claude Code auto-memory at `~/.claude/projects/<project>/memory/`.

- Do NOT write to `~/.claude/projects/*/memory/*.md` or create/update `MEMORY.md` files there.
- Save everything worth retaining (user facts, feedback, project context, references) via `add(content=..., project=<this>)`.
- If you see an existing `~/.claude/projects/*/memory/` directory, treat it as legacy — do not extend it. Migrate substance to the infoguana if still relevant.
EOF
)

# ----- emit JSON -----
# Use python3 for safe JSON encoding of the context (which may contain quotes,
# backslashes, control chars, etc).
printf '%s%s' "$context" "$override" | python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": sys.stdin.read()
    }
}))
'
