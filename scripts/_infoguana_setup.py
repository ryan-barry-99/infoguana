"""Shared helpers for the agent installers (Claude Code, Codex).

Both installers need the same two things before they can write any
agent-specific config: the server's bearer token + base URL, and a
~/.infoguana.env for the SessionStart hook to read at runtime. The
resolution order below is deliberately broad — infoguana is deployed
both as the documented Docker stack and as a bare systemd/venv service,
and only the former produces data/mcp.json.
"""
from __future__ import annotations

import json
import os
import shlex
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ENV_FILE = Path.home() / ".infoguana.env"

DEFAULT_URL = "http://localhost:8789"


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write `text` to `path` without ever leaving it truncated.

    `Path.write_text` opens with "w", which truncates to zero before a
    single byte is written. An interrupt or a full disk between those two
    moments leaves the target empty, and these are user-owned files this
    project cannot regenerate — `~/.claude/settings.json` carries the
    permissions allowlist, `~/.infoguana.env` carries the token.

    Writes a sibling temp file (same directory, so `os.replace` is a
    rename within one filesystem and therefore atomic), then replaces.
    `os.replace` preserves neither mode nor ownership, so `mode` is
    applied to the temp file *before* the rename — which also closes the
    window where a 0600 file briefly exists at the umask default.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)  # POSIX only; no-op-ish on Windows
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style .env file (KEY=VALUE, optional `export`,
    surrounding quotes, # comments) into a dict. Missing file yields {}.
    Cross-platform replacement for bash `source`."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = _unquote(value.strip())
    return out


def _unquote(value: str) -> str:
    """Undo shell quoting on an env-file value.

    Stripping matching outer quotes is not enough: `shlex.quote` renders
    an embedded single quote as `'has'"'"'quote'`, which has matching
    outer quotes and is *not* what the shell would produce on read. Ask
    shlex instead, so what `ensure_infoguana_env` writes is exactly what
    comes back.

    Falls back to the raw text whenever shlex sees anything other than a
    single token — an unquoted value with spaces, or a hand-edited line
    with unbalanced quotes. Those are the legacy shapes this file has
    always tolerated, and guessing at them is worse than passing them
    through.
    """
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


def load_env_file(path: Path) -> None:
    """Load a shell-style .env file into os.environ without overriding
    vars that are already set."""
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)


def authed_request(url: str, token: str, **kwargs) -> urllib.request.Request:
    """A Request carrying the bearer token, set so it survives no redirect.

    `urlopen` follows 30x responses by default, and
    `HTTPRedirectHandler.redirect_request` copies every header except
    Content-Length/Content-Type onto the follow-up request —
    Authorization is not among the exclusions. A token passed in the
    plain `headers` dict therefore rides along to whatever host the
    redirect names, cross-origin included, and this token grants full
    read/write access to the whole note corpus. `add_unredirected_header`
    keeps it on the first hop only, which is the only hop that should
    ever see it: the infoguana endpoints do not redirect.

    The bash variants never had this — `curl -fsS` without `-L` does not
    follow redirects — so it arrived with the Python port.
    """
    req = urllib.request.Request(url, **kwargs)
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


def _base_url(url: str) -> str:
    """Strip any path from a URL, leaving scheme://netloc. The MCP endpoint
    lives at /mcp/ but the onboard endpoints live at /onboard, so hooks
    need the bare origin."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def resolve_credentials(repo_dir: Path) -> tuple[str, str]:
    """Find the bearer token + base URL, raising RuntimeError with an
    actionable message if nothing works.

    Order, most authoritative first:
      1. data/.mcp_secret + data/mcp.json — written by the Docker
         entrypoint on every start, so it tracks secret rotations.
      2. The repo's own .env — the bare-metal (systemd/venv) install has
         no data/mcp.json, but INFOGUANA_MCP_SECRET is the same secret.
      3. An existing ~/.infoguana.env — last resort, e.g. a hand-rolled
         install whose server config lives somewhere non-standard.
    """
    # An explicit INFOGUANA_URL always wins. Every autodetected URL below
    # resolves to localhost, which is wrong whenever the agent and the
    # server aren't in the same network namespace — an agent in a
    # container reaching a server on the host needs the gateway name
    # (host.docker.internal / host.containers.internal) or the host's IP.
    url_override = (os.environ.get("INFOGUANA_URL") or "").rstrip("/")

    data = repo_dir / "data"
    secret_file, mcp_json = data / ".mcp_secret", data / "mcp.json"

    if secret_file.is_file():
        token = secret_file.read_text().strip()
        url = DEFAULT_URL
        if mcp_json.is_file():
            # The entrypoint rewrites this file on every container start,
            # so a container killed mid-write leaves it truncated. Falling
            # back to the default URL beats a JSONDecodeError traceback
            # that names json/decoder.py rather than infoguana — and the
            # default is right for every install that isn't remapping the
            # port anyway.
            try:
                mcp = json.loads(mcp_json.read_text())
                url = _base_url(mcp["mcpServers"]["infoguana"]["url"])
            except (json.JSONDecodeError, KeyError, TypeError):
                print(f"warning: {mcp_json} is unreadable or incomplete — "
                      f"falling back to {DEFAULT_URL}. If the server is on a "
                      f"non-default port, set INFOGUANA_URL.")
        return token, url_override or url

    repo_env = parse_env_file(repo_dir / ".env")
    token = repo_env.get("INFOGUANA_MCP_SECRET", "")
    if token:
        port = repo_env.get("INFOGUANA_PORT", "8789")
        return token, url_override or f"http://localhost:{port}"

    existing = parse_env_file(ENV_FILE)
    token = existing.get("INFOGUANA_TOKEN", "")
    if token:
        return token, url_override or existing.get("INFOGUANA_URL", DEFAULT_URL).rstrip("/")

    raise RuntimeError(
        f"could not find the infoguana bearer token.\n"
        f"hint:  looked in {secret_file}, {repo_dir / '.env'} "
        f"(INFOGUANA_MCP_SECRET), and {ENV_FILE} (INFOGUANA_TOKEN).\n"
        f"       is the server running? (docker compose up -d --build)"
    )


def ensure_infoguana_env(token: str, base_url: str) -> str:
    """Create or update ~/.infoguana.env so the SessionStart hooks can
    reach the server. Preserves any other lines the user added (other env
    vars, comments). Returns a short status string for the log."""
    desired = {"INFOGUANA_URL": base_url, "INFOGUANA_TOKEN": token}
    preserved: list[str] = []
    existing: dict[str, str] = {}

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                preserved.append(line)
                continue
            body = stripped
            if body.startswith("export "):
                body = body[len("export "):].lstrip()
            key, _, value = body.partition("=")
            key = key.strip()
            if key in desired:
                existing[key] = value.strip()
                continue
            preserved.append(line)

    # This file is `source`d by shell (the hook wrappers, and the README
    # tells users to add it to their rc file), so a value with a space, a
    # `#`, or a `;` in it is not just mangled on read — it executes. The
    # generated secret is hex today, which makes this insurance rather
    # than a live bug, but a hand-set token or a future base64 generator
    # turns it live. Quote on write; `parse_env_file` already strips
    # quotes on read, so the round trip is lossless.
    #
    # The comparison below has to be against the *quoted* form, since
    # that is what a previous run wrote — comparing the raw value would
    # report "refreshed" on every single run.
    quoted = {k: shlex.quote(v) for k, v in desired.items()}

    if ENV_FILE.exists() and all(existing.get(k) in (v, quoted[k])
                                 for k, v in desired.items()):
        return f"~/.infoguana.env already up-to-date ({ENV_FILE})"

    lines = preserved + [f"{k}={v}" for k, v in quoted.items()]
    atomic_write(ENV_FILE, "\n".join(lines).rstrip() + "\n", mode=0o600)

    if not existing:
        return f"created ~/.infoguana.env at {ENV_FILE}"
    return f"refreshed ~/.infoguana.env at {ENV_FILE}"


def quote(s: str) -> str:
    """Quote a path/arg for inclusion in a shell command string. Handles
    both POSIX sh and Windows cmd.exe — both treat double-quoted strings
    as a single argument."""
    if any(c in s for c in (" ", "\t", '"', "'", "(", ")", "&", "|", ";")):
        return '"' + s.replace('"', r"\"") + '"'
    return s
