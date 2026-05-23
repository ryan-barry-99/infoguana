#!/bin/sh
set -eu

# Generate the MCP shared secret on first run if the operator didn't supply one.
# Persisted to /data/.mcp_secret (a volume) so it survives container rebuilds
# and recreates without invalidating Claude Code's bearer token.
SECRET_FILE=/data/.mcp_secret
MCP_CONFIG_FILE=/data/mcp.json
PLACEHOLDER=change-me-lan-shared-secret

# Host UID/GID for bind-mounted files. Defaults to 1000:1000 (the typical
# first-user UID on Ubuntu/Debian). Linux users on other UIDs should set:
#   PUID=$(id -u) PGID=$(id -g) docker compose up -d
# On Docker Desktop (Windows/macOS), file ownership is virtualized by the
# storage layer, so these values don't affect host-side visibility.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /data /backups /claude-config

if [ -z "${INFOGUANA_MCP_SECRET:-}" ] || [ "${INFOGUANA_MCP_SECRET:-}" = "$PLACEHOLDER" ]; then
    if [ ! -s "$SECRET_FILE" ]; then
        python -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo "infoguana: generated new MCP secret at $SECRET_FILE"
    fi
    INFOGUANA_MCP_SECRET="$(cat "$SECRET_FILE")"
    export INFOGUANA_MCP_SECRET
fi

# Write a ready-to-paste mcp.json snippet to /data/mcp.json so the operator
# can copy it into ~/.claude/mcp.json without hand-assembling the bearer.
# Host placeholder defaults to localhost; override via INFOGUANA_PUBLIC_HOST
# when the container is reachable at a non-localhost address.
PUBLIC_HOST="${INFOGUANA_PUBLIC_HOST:-localhost}"
PUBLIC_PORT="${INFOGUANA_PORT:-8789}"
cat > "$MCP_CONFIG_FILE" <<EOF
{
  "mcpServers": {
    "infoguana": {
      "type": "http",
      "url": "http://${PUBLIC_HOST}:${PUBLIC_PORT}/mcp/",
      "headers": {
        "Authorization": "Bearer ${INFOGUANA_MCP_SECRET}"
      }
    }
  }
}
EOF

# Take ownership of bind-mounted state so the host user can read+manage it
# without sudo. Applied every run so an installation that started owned by
# root (from before this fix) self-heals on the next `docker compose up`.
# Best-effort: chown can fail if the container is configured to run as a
# non-root user (e.g. compose `user:` directive) — in that case files are
# already owned by whatever the container runs as, and chown is unnecessary.
chown -R "${PUID}:${PGID}" /data /backups /claude-config 2>/dev/null || true

cat <<EOF
────────────────────────────────────────────────────────────────────────
  infoguana ready on :${PUBLIC_PORT}
  Web UI:     http://${PUBLIC_HOST}:${PUBLIC_PORT}/
  MCP secret: $(cat "$SECRET_FILE" 2>/dev/null || echo "$INFOGUANA_MCP_SECRET")
  Wire it up: python scripts/install-infoguana-mcp.py
              (merges ./data/mcp.json into ~/.claude.json)
  Optional:   docker compose exec infoguana claude /login
              (one-time Claude CLI auth for auto-classification)
────────────────────────────────────────────────────────────────────────
EOF

exec "$@"
