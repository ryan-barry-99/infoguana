#!/usr/bin/env python3
"""Idempotent installer for infoguana's Codex integration.

Codex is the sibling of scripts/install-infoguana-hooks.py (Claude Code).
Both point the same server at the same corpus; which agent you run is a
free choice.

Does two things:

1. Auto-generates ~/.infoguana.env with the server's URL + bearer token,
   exactly as the Claude Code installer does. The SessionStart hook reads
   it at runtime, so both agents share one credential file.

2. Writes a managed block into ~/.codex/config.toml registering:
     - `mcp_servers.infoguana`  — streamable-HTTP MCP + bearer auth
     - `hooks.SessionStart`     — N slices of
       scripts/infoguana-onboard-chunk.py, plus one --override entry

Codex's hook wire protocol is Claude-Code-compatible (it consumes the
same {"hookSpecificOutput": {"hookEventName", "additionalContext"}} shape
on stdout), so a single hook script serves both agents.

The managed block is delimited by marker comments and rewritten in place
on re-run; everything outside it — including comments and hand-edited
settings — is preserved byte-for-byte. The result is parsed with tomllib
before it replaces the existing file, so a conflicting hand-written
[mcp_servers.infoguana] surfaces as an error instead of a corrupt config.

Usage:
    python scripts/install-infoguana-codex.py
    python scripts/install-infoguana-codex.py --print   # show, write nothing

Two manual steps remain after this script (both are Codex-side and
cannot be scripted); it prints them on success.
"""
from __future__ import annotations

import argparse
import os
import sys
import json
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _infoguana_setup import (  # noqa: E402
    ENV_FILE,
    atomic_write,
    authed_request,
    ensure_infoguana_env,
    quote,
    resolve_credentials,
)

REPO_DIR = Path(__file__).resolve().parent.parent
HOOK = REPO_DIR / "scripts" / "infoguana-onboard-chunk.py"
# Registered when the server can't be measured. Generous on purpose:
# a surplus hook is a no-op (the route returns "" and the hook emits
# nothing), while a shortfall truncates every slice.
FALLBACK_CHUNKS = 40
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CONFIG = CODEX_HOME / "config.toml"

BEGIN = "# >>> infoguana (managed by scripts/install-infoguana-codex.py) >>>"
END = "# <<< infoguana <<<"

# Codex reads the bearer token from the environment at launch rather than
# from its config file, so the secret never lands in config.toml.
#
# Deliberately NOT INFOGUANA_MCP_SECRET, which is the *server's* variable:
# docker-compose interpolates it from the invoking shell and the entrypoint
# treats a non-empty value as "the operator supplied the secret", skipping
# generation. Exporting that name from a login shell — which is exactly what
# the setup instructions below ask for — would make `docker compose up` re-pin
# the server to the old token after a rotation, silently and with no signal.
TOKEN_ENV_VAR = "INFOGUANA_TOKEN"


def _resolve_chunks(base_url: str, token: str) -> int:
    """How many slice hooks to register, measured by the server.

    Mirrors the Claude Code installer: /onboard/sizing splits every
    project's blob and reports the count at which no slice exceeds the
    inline budget. INFOGUANA_HOOK_CHUNKS overrides (1..64, the chunk
    route's accepted range).
    """
    override = os.environ.get("INFOGUANA_HOOK_CHUNKS")
    if override is not None:
        try:
            n = int(override)
        except ValueError:
            raise RuntimeError("INFOGUANA_HOOK_CHUNKS must be an integer")
        if not 1 <= n <= 64:
            raise RuntimeError("INFOGUANA_HOOK_CHUNKS must be 1..64")
        return n
    req = authed_request(f"{base_url.rstrip('/')}/onboard/sizing", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return int(data["recommended_chunks"])
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, KeyError, ValueError):
        print(f"warning: could not reach {base_url}/onboard/sizing — "
              f"registering {FALLBACK_CHUNKS} chunks. Re-run once the server "
              f"is up so the count is measured.", file=sys.stderr)
        return FALLBACK_CHUNKS


def render_block(base_url: str, chunks: int) -> str:
    """Emit the managed block: the MCP server plus `chunks` slice hooks and
    one override hook.

    Chunked rather than single-shot, matching the Claude Code installer.
    A single hook returning the whole blob exceeds Codex's inline budget:
    it truncates the injection and spills the remainder to a file, handing
    the agent a path. That looks like graceful degradation and isn't — the
    agent only reads the file if it decides to, so rules and plans past
    the cut are silently absent from the session that needed them. A
    Codex session read it once, when asked point-blank what it had
    received, and otherwise reached for its own on-disk skills. Forced
    injection across N slices is the point; a pointer defeats it.
    """
    lines = [
        BEGIN,
        "# Regenerate with: python scripts/install-infoguana-codex.py",
        "",
        "[mcp_servers.infoguana]",
        # json.dumps, not an f-string: TOML basic strings share JSON's
        # escaping rules, and `quote()` below does *shell* quoting, which
        # emits bare `"` and leaves backslashes alone. Interpolated raw,
        # a path containing a space closed the TOML string early and a
        # Windows path read `\U` as a unicode escape — and main() then
        # blamed the user's hand-written config for the parse error.
        "url = " + json.dumps(f"{base_url}/mcp/"),
        "bearer_token_env_var = " + json.dumps(TOKEN_ENV_VAR),
        "",
        "# Injects the project's rules + plans + memories at session",
        "# start, one slice per hook. Each hook's output has its own inline",
        "# budget, so N slices land whole where one blob would be truncated.",
        "# Emits Claude Code's hook wire format, which Codex also consumes.",
    ]
    for i in range(chunks):
        lines += [
            "",
            "[[hooks.SessionStart]]",
            "[[hooks.SessionStart.hooks]]",
            'type = "command"',
            "command = " + json.dumps(
                f"{quote(sys.executable)} {quote(str(HOOK))} {i} {chunks}"),
        ]
    lines += [
        "",
        "# Steers durable memory to infoguana rather than Codex's own store.",
        "[[hooks.SessionStart]]",
        "[[hooks.SessionStart.hooks]]",
        'type = "command"',
        "command = " + json.dumps(
            f"{quote(sys.executable)} {quote(str(HOOK))} --override"),
        END,
    ]
    return "\n".join(lines) + "\n"


# Tables this script emits, and the keys it owns within them. Anything
# else found inside the managed block belongs to Codex or the user and is
# carried across a regeneration — see _merge for why that matters.
#
# Hook tables are deliberately absent. Table *name* is the wrong ownership
# test for them: `[[hooks.SessionStart]]` is an array, so a user's own hook
# lands under the identical header as ours, and claiming the header claims
# their entry too. Hook ownership is decided per entry by `_is_ours`, on
# the command it runs — the same test install-infoguana-hooks.py applies.
OWNED: dict[str, set[str]] = {
    "[mcp_servers.infoguana]": {"url", "bearer_token_env_var"},
}

HOOK_HEADERS = ("[[hooks.SessionStart]]", "[[hooks.SessionStart.hooks]]")
HOOK_OWNED_KEYS = {"type", "command"}


def _key(line: str) -> str:
    return line.split("=", 1)[0].strip()


def _value(lines: list[str], key: str) -> str:
    """The raw value of `key` among `lines`, or "" if absent."""
    for ln in lines:
        if _key(ln) == key:
            return ln.split("=", 1)[1].strip() if "=" in ln else ""
    return ""


def _hook_entries(segments: list[tuple[str, list[str]]]) -> list[list[int]]:
    """Group segment indices into hook entries.

    A `[[hooks.SessionStart]]` opens an entry; the `[[hooks.SessionStart.hooks]]`
    tables after it belong to that entry until the next one. The pair is the
    unit that has to be kept or dropped together — preserving an inner table
    without its parent produces a config that no longer describes a hook.
    """
    entries: list[list[int]] = []
    for i, (header, _) in enumerate(segments):
        if header == "[[hooks.SessionStart]]":
            entries.append([i])
        elif header == "[[hooks.SessionStart.hooks]]" and entries:
            entries[-1].append(i)
    return entries


def _is_ours(segments: list[tuple[str, list[str]]], entry: list[int]) -> bool:
    """True when this hook entry runs our hook script."""
    marker = str(HOOK)
    return any(marker in _value(segments[i][1], "command") for i in entry)


def _segments(body: str) -> list[tuple[str, list[str]]]:
    """Split TOML text into (table header, lines-under-it) pairs. Lines
    before the first header are returned under the empty header."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped in (BEGIN, END):
            # Markers are structural, not content — a trailing END would
            # otherwise be carried along as part of the last table and
            # duplicated on re-emit.
            continue
        if stripped.startswith("["):
            out.append((stripped, []))
        else:
            out[-1][1].append(line)
    return out


def _merge(old_block: str, new_block: str) -> str:
    """Regenerate the managed block, carrying over anything inside it that
    this script does not own.

    Codex writes into config.toml itself — `[hooks.state]` records the
    per-hook trust hash you approve in the UI, and per-server keys like
    `default_tools_approval_mode` are set from its settings pane. Because
    it appends at end-of-file and the END marker is a trailing comment,
    that content lands *inside* our markers. Blindly overwriting the block
    would silently revoke hook trust and reset tool-approval settings, so
    unowned tables and unowned keys within our tables are preserved.

    Hook entries get their own treatment for two reasons the header-based
    rule got wrong. A user's hook lands under the same array header as
    ours, so owning the header deleted their entry outright — both its
    keys were filtered as ours, nothing was left to carry, and it was
    never re-emitted. And unowned keys inside our own hook tables are
    keyed here by the command they sit beside rather than by header,
    because N+1 tables share one header: keying by header let each
    occurrence overwrite the last, so N-1 trust hashes were lost and the
    survivor was re-attached to the wrong hook.
    """
    old_segments = _segments(old_block)
    known = {h for h, _ in _segments(new_block)}
    extra_keys: dict[str, list[str]] = {}
    foreign: list[str] = []

    hook_entries = _hook_entries(old_segments)
    ours = {i for entry in hook_entries if _is_ours(old_segments, entry)
            for i in entry}
    theirs = [entry for entry in hook_entries if not _is_ours(old_segments, entry)]

    for entry in theirs:
        # Preserve the whole entry — parent table and its hook tables —
        # verbatim. Half an entry is not a hook.
        block = []
        for i in entry:
            header, lines = old_segments[i]
            body = "\n".join(lines).strip("\n")
            block.append(header + ("\n" + body if body else ""))
        foreign.append("\n".join(block))

    # Keys Codex may have written into our own parent hook tables. No
    # command sits in a parent table to key on, so these carry by
    # occurrence order — the parents we emit are interchangeable.
    parent_extra: list[list[str]] = []

    for idx, (header, lines) in enumerate(old_segments):
        if not header or header in (BEGIN, END):
            continue
        if header in HOOK_HEADERS:
            if idx not in ours:
                continue
            if header == "[[hooks.SessionStart]]":
                parent_extra.append([
                    ln for ln in lines
                    if ln.strip() and not ln.strip().startswith("#")
                ])
                continue
            kept = [
                ln for ln in lines
                if ln.strip() and not ln.strip().startswith("#")
                and _key(ln) not in HOOK_OWNED_KEYS
            ]
            if kept:
                extra_keys[_value(lines, "command")] = kept
        elif header in OWNED:
            kept = [
                ln for ln in lines
                if ln.strip() and not ln.strip().startswith("#")
                and _key(ln) not in OWNED[header]
            ]
            if kept:
                extra_keys[header] = kept
        elif header not in known:
            body = "\n".join(lines).strip("\n")
            foreign.append(header + ("\n" + body if body else ""))

    lines_out: list[str] = []
    new_lines = new_block.splitlines()
    for i, line in enumerate(new_lines):
        lines_out.append(line)
        stripped = line.strip()
        if stripped in OWNED and stripped in extra_keys:
            lines_out.extend(extra_keys.pop(stripped))
        elif stripped == "[[hooks.SessionStart]]":
            if parent_extra:
                lines_out.extend(parent_extra.pop(0))
        elif stripped == "[[hooks.SessionStart.hooks]]":
            # Match on the command this table is about to declare, so a
            # carried-over key lands beside the hook it was recorded for.
            command = _value(new_lines[i + 1:i + 4], "command")
            if command in extra_keys:
                lines_out.extend(extra_keys.pop(command))
    rendered = "\n".join(lines_out)

    if foreign:
        rendered = rendered.replace(END, "\n\n".join(foreign) + "\n" + END)
    return rendered + "\n"


def splice(existing: str, block: str) -> str:
    """Replace the managed block in `existing`, or append it. Content
    outside the markers is untouched; content inside it that this script
    does not own is carried across (see _merge)."""
    start = existing.find(BEGIN)
    if start == -1:
        prefix = existing.rstrip("\n")
        return (prefix + "\n\n" if prefix else "") + block
    end = existing.find(END, start)
    if end == -1:  # truncated block (hand-edited): replace to end of file
        return existing[:start] + _merge(existing[start:], block)
    merged = _merge(existing[start:end + len(END)], block)
    return existing[:start] + merged + existing[end + len(END):].lstrip("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the managed block and exit")
    args = parser.parse_args()

    if not HOOK.is_file():
        print(f"error: hook script not found at {HOOK}", file=sys.stderr)
        return 1

    try:
        token, base_url = resolve_credentials(REPO_DIR)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except PermissionError as e:
        # Same stale-container case the Claude Code installer handles: old
        # containers (pre-PUID/PGID entrypoint) wrote secret files as root
        # with chmod 600. This path is newer, so it is *more* likely to
        # meet an older data/ directory, not less.
        print(f"error: can't read {e.filename or 'data/'} — owned by another user.",
              file=sys.stderr)
        print("hint:  rebuild + restart the container so the entrypoint can chown",
              file=sys.stderr)
        print("       host-side files to your UID/GID, or manually:", file=sys.stderr)
        print(f"           sudo chown -R $USER:$USER {REPO_DIR / 'data'}",
              file=sys.stderr)
        return 1

    chunks = _resolve_chunks(base_url, token)
    block = render_block(base_url, chunks)
    if args.print_only:
        print(block, end="")
        return 0

    env_status = ensure_infoguana_env(token, base_url)

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = CONFIG.read_text() if CONFIG.exists() else ""
    updated = splice(existing, block)

    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as e:
        print(f"error: refusing to write {CONFIG} — result would not parse: {e}",
              file=sys.stderr)
        print("hint:  you likely already define [mcp_servers.infoguana] or "
              "[[hooks.SessionStart]] outside the managed block; remove it "
              "and re-run.", file=sys.stderr)
        return 1

    if updated == existing:
        print(f"codex config already up-to-date ({CONFIG})")
    else:
        atomic_write(CONFIG, updated)
        verb = "updated" if BEGIN in existing else "installed"
        print(f"{verb} infoguana block in {CONFIG}")
    print(env_status)
    print()
    print("Two manual steps remain.")
    print()
    print(f"1. Codex reads the bearer token from the {TOKEN_ENV_VAR}")
    print("   environment variable of its own process, at startup — not from")
    print("   config.toml. Add this to your ~/.bashrc or ~/.zshrc:")
    print()
    print('       if [ -f "$HOME/.infoguana.env" ]; then')
    print('           . "$HOME/.infoguana.env"')
    print(f"           export {TOKEN_ENV_VAR}")
    print("       fi")
    print()
    print("   Then open a new terminal (CLI), or fully restart your editor")
    print("   (IDE extension). Reloading the window is usually not enough —")
    print("   a remote/server process survives reloads with its old env.")
    print(f"   Confirm with:  echo ${{#{TOKEN_ENV_VAR}}}   (expect non-zero)")
    print()
    print("2. Approve the SessionStart hook in the Codex UI. Codex tracks a")
    print("   trust hash per hook and ignores ones it hasn't been told to")
    print("   trust, so it stays inert until accepted. MCP tools work either")
    print("   way; the auto-injected project context is what's gated.")
    print()
    print("Verify with:  codex mcp list   (infoguana, Auth: Bearer token)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
