#!/usr/bin/env bash
# SessionStart hook for Claude Code that fetches one slice of the
# infoguana's onboard blob and emits it as additionalContext.
#
# Note #374: each hook's additionalContext is capped at ~2KB inline, but
# the cap is *per-hook*. The installer registers N (default 16) entries
# of this script with different chunk indices; all N slices land inline
# at session start with no truncation, so the agent sees the full
# ~22KB blob without a Read-tool round-trip.
#
# Args:
#   $1  chunk index (0-based)
#   $2  total chunks (matches the of= query param)
#
# Reads INFOGUANA_URL, INFOGUANA_TOKEN, INFOGUANA_ONBOARD_BUDGET from
# ~/.infoguana.env. Fails open: any error path emits nothing rather than
# blocking the session.

set -euo pipefail

INDEX="${1:-}"
TOTAL="${2:-}"

[[ -z "$INDEX" || -z "$TOTAL" ]] && exit 0

# Opt-out for callers that already seed context themselves (e.g. the
# infoguana web-UI chat, which prepends a project-scoped seed in
# _seed_context before spawning `claude -p`). Without this, the hook
# fires from the service's cwd and injects `infoguana` memories on
# top of every chat regardless of the chat's selected project.
[[ "${INFOGUANA_HOOK_DISABLE:-0}" == "1" ]] && exit 0

# Stagger by chunk index so the agent sees chunks in registration order.
# Empirically the harness assembles hook outputs in completion order
# (note #374's 5-hook test: registered A,B,C,D,E — agent saw A,C,D,B,E).
# With onboard.build_cached() (server-side 10s TTL), the first chunk
# request takes ~300ms (cold build) and the rest serve from cache in
# ~5-10ms. A 50ms-per-index stagger comfortably dominates that, putting
# completion order = registration order.
if [[ "$INDEX" -gt 0 ]]; then
  python3 -c "import time; time.sleep($INDEX * 0.05)"
fi

if [[ -f "${HOME}/.infoguana.env" ]]; then
  # shellcheck disable=SC1091
  source "${HOME}/.infoguana.env"
fi

URL="${INFOGUANA_URL:-http://localhost:8789}"
TOKEN="${INFOGUANA_TOKEN:-}"
BUDGET="${INFOGUANA_ONBOARD_BUDGET:-4000}"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT="$(basename "$PROJECT_DIR")"

[[ -z "$TOKEN" ]] && exit 0

chunk=$(curl -fsS --max-time 5 \
  -H "Authorization: Bearer $TOKEN" \
  "${URL%/}/onboard/${PROJECT}/chunk/${INDEX}?of=${TOTAL}&budget_tokens=${BUDGET}" \
  2>/dev/null || true)

# Empty chunk (blob shorter than N * chunk-target) → emit nothing. The
# harness treats no-output as a successful no-op hook.
[[ -z "$chunk" ]] && exit 0

printf '%s' "$chunk" | python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": sys.stdin.read()
    }
}))
'
