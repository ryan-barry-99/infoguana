# Deploying infoguana

Quick start is in the [README](README.md#quick-start-docker). This doc
covers the rest: Claude CLI auth, filesystem tools, backups, updating.

## Requirements

- Docker with Compose v2.
- ~2 GB RAM and ~10 GB disk for the image, model weights, and DB growth.

## Authenticate the containerised claude CLI (optional)

The auto-classifier shells out to `claude -p`. Auth it once; credentials
persist in the `./claude-config/` volume across restarts.

```bash
docker compose exec infoguana claude /login
# follow the URL, paste the code back
```

Skip this if you don't care about auto-classification — notes stay
`unsorted` until you label them in the UI, and everything else (capture,
embedding, search, MCP) still works.

## Access from other devices

The web UI and MCP endpoint listen on `:8789`. From the host, use
`http://localhost:8789`. From other devices on the same network, use the
host's hostname or IP. The MCP endpoint always requires the bearer
token; the web UI is open to anything that can reach the port, so don't
expose `:8789` directly to the public internet — keep it on a trusted
network or behind a reverse proxy / mesh VPN.

If the infoguana host is reachable at a name other than `localhost`,
bake that into the generated `mcp.json` so the installer wires Claude
Code at the right address:

```bash
INFOGUANA_PUBLIC_HOST=infoguana.example.com docker compose up -d --build
python scripts/install-infoguana-mcp.py
```

## Filesystem read tools (optional)

The infoguana exposes read-only filesystem tools (`read_file`,
`list_dir`, `grep`) so the chat agent and connected Claude Code
sessions can ground answers in actual source files. Access is scoped by
an allowlist:

```
INFOGUANA_FS_ALLOWLIST=/root/code            # colon-separated absolute roots
INFOGUANA_FS_READ_MAX_BYTES=512000           # per-read cap (default 500 KiB)
```

Paths outside the allowlist are refused. A hardcoded denylist (`.env*`,
SSH/GPG keys, `.git/` internals, `*.sqlite`, cloud creds) applies
additionally — don't rely on the allowlist alone to keep secrets out.
Binary files are refused outright; tools are for source code.

Every successful read is recorded in the `fs_reads` table for audit.

## Backups

The app snapshots `/data/infoguana.db` to `/backups/infoguana-<ts>.db`
every 24h using SQLite's online backup API (safe while writes are
happening). It keeps the last 30 snapshots. On the host these land in
`./backups/` — point any external backup tool at that directory.

To restore:
```bash
docker compose down
cp backups/infoguana-<ts>.db data/infoguana.db
docker compose up -d
```

## Updating

```bash
git pull
docker compose up -d --build
```

The DB schema uses `CREATE TABLE IF NOT EXISTS`, so restarts are safe.
Breaking schema changes will be documented in commit messages.
