#!/usr/bin/env bash
# Idempotent installer: merges the infoguana's generated mcp.json into the
# user's Claude Code config (~/.claude.json -> mcpServers.infoguana).
#
# The container entrypoint writes ./data/mcp.json on every start with the
# current bearer + URL. Re-running this script picks up secret rotations
# without touching any of the user's other mcpServers entries.
#
# Usage:
#   docker compose up -d --build
#   ./scripts/install-infoguana-mcp.sh
#
# Override the URL host in the generated snippet via:
#   INFOGUANA_PUBLIC_HOST=brain.tail.ts.net docker compose up -d --build
# (then re-run this installer).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_DIR}/data/mcp.json"
DEST="${HOME}/.claude.json"

# The entrypoint writes ./data/mcp.json before exec'ing the server. If the
# stack was just brought up, give it a couple seconds to land.
for _ in 1 2 3 4 5; do
  [[ -f "$SRC" ]] && break
  sleep 1
done

if [[ ! -f "$SRC" ]]; then
  echo "error: $SRC not found." >&2
  echo "hint:  is the container running? (docker compose up -d --build)" >&2
  exit 1
fi

if [[ ! -f "$DEST" ]]; then
  echo '{}' > "$DEST"
fi

python3 - "$SRC" "$DEST" <<'PY'
import json, sys
from pathlib import Path

src_path = Path(sys.argv[1])
dest_path = Path(sys.argv[2])

src = json.loads(src_path.read_text())
infoguana_entry = src.get("mcpServers", {}).get("infoguana")
if not infoguana_entry:
    sys.exit(f"error: {src_path} has no mcpServers.infoguana block")

raw = dest_path.read_text().strip() or "{}"
dest = json.loads(raw)
servers = dest.setdefault("mcpServers", {})

prev = servers.get("infoguana")
servers["infoguana"] = infoguana_entry
dest_path.write_text(json.dumps(dest, indent=2) + "\n")

url = infoguana_entry.get("url", "?")
if prev == infoguana_entry:
    print(f"infoguana MCP already up-to-date in {dest_path} ({url})")
elif prev is None:
    print(f"installed infoguana MCP in {dest_path} ({url})")
else:
    print(f"updated infoguana MCP in {dest_path} ({url})")
PY

echo
echo "Next: restart any open Claude Code sessions, then test with /mcp list."
