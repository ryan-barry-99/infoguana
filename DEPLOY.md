# Deploying infoguana

Quick start is in the [README](README.md#quick-start-docker). This doc
covers the rest: Claude CLI auth, filesystem tools, backups, updating.

## Requirements

- Docker with Compose. v2 is what CI builds and boots; the compose file
  avoids v2.24-only syntax, so older Compose parses it too.
- A `.env` file in the repo root. Every setting has a default, so it may be
  empty, but compose reads it and refuses to start when it is absent:
  `cp .env.example .env`.
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

If infoguana host is reachable at a name other than `localhost`,
bake that into the generated `mcp.json` so the installer wires Claude
Code at the right address:

```bash
cp .env.example .env   # if you have not already
INFOGUANA_PUBLIC_HOST=infoguana.example.com docker compose up -d --build
python scripts/install-infoguana-mcp.py
```

## Filesystem read tools (off by default)

Infoguana can expose read-only filesystem tools (`read_file`,
`list_dir`, `grep`) so the chat agent and connected Claude Code
sessions can ground answers in actual source files. **They are disabled
until you name the roots they may read under** — with no allowlist, every
call is refused with a message saying the feature is off:

```
INFOGUANA_FS_ALLOWLIST=/root/code            # colon-separated absolute roots
INFOGUANA_FS_READ_MAX_BYTES=512000           # per-read cap (default 500 KiB)
```

Under Docker these are container paths, so a root only works if it is also
bind-mounted into the container.

Paths outside the allowlist are refused. A hardcoded denylist (`.env*`,
SSH/GPG keys, `.git/` internals, `*.sqlite`, cloud creds) applies
additionally — don't rely on the allowlist alone to keep secrets out.
Binary files are refused outright; tools are for source code.

Every successful read is recorded in the `fs_reads` table for audit.

## MCP Host/Origin allowlist (optional)

The bearer token is the gate on the MCP endpoint. As defense in depth you
can also enable the SDK's DNS-rebinding protection, which checks the `Host`
and `Origin` headers:

```
INFOGUANA_MCP_ALLOWED_HOSTS=10.0.0.5:*,infoguana.tailnet.ts.net
```

Comma-separated; `:*` wildcards the port. Loopback is always included, so
list only the LAN or tailnet names clients actually use. Leave it unset —
the default — to perform no Host/Origin checks; setting it to a value that
omits a name your clients use will lock them out.

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
