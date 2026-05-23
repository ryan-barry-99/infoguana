#!/usr/bin/env bash
# Idempotent installer for the infoguana's Claude Code SessionStart hooks.
#
# Registers N (default 16) entries of scripts/infoguana-onboard-chunk.sh
# in ~/.claude/settings.json — each pinned to a different chunk index of
# the project's onboard blob. Per note #374, each hook's additionalContext
# is capped at ~2KB inline but the cap is *per-hook*, so all N chunks
# land inline at session start with no truncation.
#
# Re-running is a no-op: matches by hook command (script + args). Changing
# INFOGUANA_HOOK_CHUNKS removes the old entries and registers the new
# count.
#
# Usage:
#   ./scripts/install-infoguana-hooks.sh
#   INFOGUANA_HOOK_CHUNKS=20 ./scripts/install-infoguana-hooks.sh
#
# After install, set INFOGUANA_URL and INFOGUANA_TOKEN in ~/.infoguana.env
# so the hooks can reach your running infoguana server.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_DIR}/scripts/infoguana-onboard-chunk.sh"
SETTINGS="${HOME}/.claude/settings.json"
N="${INFOGUANA_HOOK_CHUNKS:-16}"

if [[ ! -f "$HOOK" ]]; then
  echo "error: hook script not found at $HOOK" >&2
  exit 1
fi
chmod +x "$HOOK"

mkdir -p "$(dirname "$SETTINGS")"
[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"

# Use python for safe JSON merge — preserves user's other hooks, replaces
# any existing infoguana-onboard-chunk.sh entries with the requested count.
python3 - "$SETTINGS" "$HOOK" "$N" <<'PY'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
hook_path = sys.argv[2]
n = int(sys.argv[3])

raw = settings_path.read_text().strip() or "{}"
data = json.loads(raw)
hooks = data.setdefault("hooks", {})

# Drop any existing entries for this hook script across SessionStart and
# (legacy) UserPromptSubmit, regardless of chunk args. Then re-register.
def _strip(entries):
    out = []
    for entry in entries:
        kept_hooks = [
            h for h in entry.get("hooks", [])
            if hook_path not in (h.get("command") or "")
        ]
        if kept_hooks:
            entry = dict(entry)
            entry["hooks"] = kept_hooks
            out.append(entry)
    return out

for event in ("SessionStart", "UserPromptSubmit"):
    if event in hooks:
        hooks[event] = _strip(hooks[event])

# Also drop the legacy infoguana-first-turn.sh (UserPromptSubmit) if the
# user is upgrading from the pre-chunked design.
legacy = str(Path(hook_path).parent / "infoguana-first-turn.sh")
for event in ("UserPromptSubmit", "SessionStart"):
    if event in hooks:
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(legacy in (h.get("command") or "")
                       for h in entry.get("hooks", []))
        ]

ss = hooks.setdefault("SessionStart", [])
for i in range(n):
    ss.append({
        "hooks": [{
            "type": "command",
            "command": f"{hook_path} {i} {n}",
        }]
    })

settings_path.write_text(json.dumps(data, indent=2) + "\n")
print(f"registered {n} SessionStart hooks ({hook_path} <i> {n})")
PY

echo
echo "Next steps:"
echo "  1. Make sure ~/.infoguana.env exports INFOGUANA_URL and INFOGUANA_TOKEN."
echo "  2. Open a new Claude Code session in any project — its first system"
echo "     context will carry $N inline chunks (~${N}x ~1.7KB ≈ $((N*17/10))KB)"
echo "     of project-specific rules + plans + memories, no Read-tool round-trip."
