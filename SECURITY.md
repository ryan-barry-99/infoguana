# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports.
Instead, use GitHub's private vulnerability reporting:
https://github.com/ryan-barry-99/infoguana/security/advisories/new

Include:

- A description of the issue and the affected component
- Steps to reproduce (or a proof-of-concept if you have one)
- Your assessment of the impact
- Suggested mitigation, if you have one

You'll get an acknowledgement within 7 days. Expect to coordinate on
disclosure timing before any public discussion.

## Scope

In scope:

- The infoguana FastAPI app (`app/`), MCP server, web UI
- The install scripts (`scripts/install-infoguana-*.py`)
- The Docker image and entrypoint
- The packaged `infoguana-onboard` skill

Out of scope:

- Secrets the user configures (`INFOGUANA_MCP_SECRET`,
  `INFOGUANA_GITHUB_READ_TOKEN`, `INFOGUANA_GITHUB_BOT_TOKENS`) — these
  are the operator's responsibility.
- The user's own infoguana note content.
- Third-party dependencies — please report those upstream
  (FastAPI, pydantic, sqlite-vec, fastembed, mcp, etc.).
- Vulnerabilities that require the attacker to already have host
  access where infoguana is running (e.g. arbitrary file read via a
  malicious `INFOGUANA_FS_ALLOWLIST` entry — the allowlist is operator
  configuration).

## Secret handling

infoguana stores a few sensitive values; if you're auditing or
reporting, here's where they live:

| Secret | Location | Notes |
|---|---|---|
| MCP bearer token | `./data/.mcp_secret` (in the Docker volume); embedded in `~/.claude.json` and `~/.infoguana.env` after installer runs | Auto-generated on first container start, `chmod 600` on POSIX. Rotatable by deleting the file and restarting the container. |
| Claude Code CLI credentials | `./claude-config/` (in the Docker volume) | Only present if you ran `docker compose exec infoguana claude /login`. Persists across rebuilds. |
| GitHub PATs (optional) | `INFOGUANA_GITHUB_READ_TOKEN` / `INFOGUANA_GITHUB_BOT_TOKENS` env vars | Read from `.env` or the host environment. Never committed by default; `.gitignore` excludes `.env`. |
| User notes (which may contain anything the user chose to capture) | `./data/infoguana.db` (SQLite) | Bind-mounted from the host. Plain SQLite file — encrypt the filesystem if that matters in your threat model. |

The web UI is **not** authenticated; access is gated by who can reach
port 8789 on the host. Don't expose it directly to the public internet
— put it behind a reverse proxy with auth, a mesh VPN, or keep it on a
trusted network. The MCP endpoint (`/mcp/`) does require the bearer.

## Supported versions

infoguana is a single-track project — security fixes land on `main`
and are picked up via `git pull && docker compose up -d --build`.
There are no maintained backports.
