"""Read-only filesystem access for infoguana MCP clients.

The infoguana runs as root on the host, so the *allowlist* + *denylist* are the
real trust boundary — not the caller. Every exported helper enforces:

  1. `path` resolves (symlinks followed) to a descendant of at least one
     allowlist root from `settings.fs_allowlist`.
  2. No path component matches the hardcoded denylist globs (secrets,
     keys, `.git/`, `*.sqlite`, …).
  3. Reads are size-capped (`settings.fs_read_max_bytes`) and binary files
     are refused outright — this tool is for source code, not blobs.

Violations raise `FSAccessError`; callers should surface `.message` back
to the agent as a clean error.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app import db
from app.config import settings


# --- denylist --------------------------------------------------------------
#
# Case-insensitive glob patterns. A path is rejected if ANY component (or the
# full basename) matches ANY pattern. Keep this list conservative — false
# refusals are annoying, but leaking a secret is worse.

_DENY_PATTERNS: tuple[str, ...] = (
    # Secrets and tokens
    ".env", ".env.*", "*.env",
    ".netrc",
    "credentials*", "secrets*", "*.secret", "*.secrets",
    # SSH / GPG keys
    ".ssh", ".gnupg",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*.pem", "*.key", "*.pfx", "*.p12",
    # Cloud creds
    ".aws", ".gcloud", ".azure",
    # Docker auth (has registry tokens)
    "config.json",  # only matched under .docker/ — full-path filter below
    # VCS internals (hooks, config with tokens)
    ".git",
    # Infoguana's own DB / WAL / SHM — don't let an agent exfiltrate its own store
    "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.db",
)

# Full-path suffix patterns (checked against the whole path). Use when a
# bare name like "config.json" is too broad (e.g. every npm project has one).
_DENY_SUFFIXES: tuple[str, ...] = (
    "/.docker/config.json",
)


# --- binary-file detection -------------------------------------------------

# Conservative "looks like source" check: any NUL byte in the first 8 KiB
# marks the file as binary. ASCII/UTF-8 source code has no NULs.
_BINARY_PROBE_BYTES = 8192


# --- grep auto-excludes ----------------------------------------------------
#
# Directories that ripgrep's `--smart` / `--ignore` would skip anyway; we
# mirror them explicitly so the pure-Python fallback and `rg --no-ignore`
# callers behave consistently.

_GREP_SKIP_DIRS: tuple[str, ...] = (
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "target", "build", "dist", ".next", ".cache", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
)

_GREP_SKIP_FILE_GLOBS: tuple[str, ...] = (
    "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "uv.lock",
)


# --- errors ----------------------------------------------------------------


class FSAccessError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- resolver --------------------------------------------------------------


def _denied(path: Path) -> Optional[str]:
    """Return the matching deny pattern if `path` is forbidden, else None."""
    str_path = str(path)
    for suf in _DENY_SUFFIXES:
        if str_path.endswith(suf):
            return suf
    for part in path.parts:
        low = part.lower()
        for pat in _DENY_PATTERNS:
            if fnmatch.fnmatchcase(low, pat.lower()):
                return pat
    return None


def _under_allowlist(resolved: Path) -> bool:
    for root in settings.fs_allowlist:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def resolve(path: str) -> Path:
    """Resolve `path` to an absolute canonical path after enforcing allow/deny.

    Raises FSAccessError if the path is outside the allowlist, matches a deny
    pattern, or fails to resolve. Does NOT require the path to exist —
    `list_dir` callers may legitimately probe nonexistent paths; file-open
    callers get a clean FileNotFoundError.
    """
    try:
        raw = Path(path).expanduser()
        resolved = raw.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise FSAccessError(f"could not resolve path: {e}")

    if not _under_allowlist(resolved):
        roots = ", ".join(str(r) for r in settings.fs_allowlist)
        raise FSAccessError(
            f"path is outside the configured allowlist ({roots}): {resolved}"
        )
    deny = _denied(resolved)
    if deny:
        raise FSAccessError(
            f"path matches denylist pattern '{deny}': {resolved}"
        )
    return resolved


# --- reading ---------------------------------------------------------------


@dataclass
class ReadResult:
    path: str
    bytes_read: int
    lines_returned: int
    total_lines: int
    truncated: bool
    content: str


def _looks_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            chunk = f.read(_BINARY_PROBE_BYTES)
    except OSError:
        return False
    return b"\x00" in chunk


def read_file(
    path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> ReadResult:
    """Read a text file with optional line-offset / limit.

    `offset` is 1-based (matching Claude Code's Read tool). `limit` caps the
    number of lines returned. Even with offset+limit, the total bytes read
    is capped at `settings.fs_read_max_bytes`; files larger than that must
    be paginated via offset/limit.

    Returns content formatted with `line_no<tab>content` on each line, so
    line numbers survive in the agent's context.
    """
    resolved = resolve(path)
    if not resolved.exists():
        raise FSAccessError(f"file does not exist: {resolved}")
    if not resolved.is_file():
        raise FSAccessError(f"not a regular file: {resolved}")
    if _looks_binary(resolved):
        raise FSAccessError(
            f"refusing to read binary file: {resolved} "
            "(tool is source-code-only)"
        )

    size = resolved.stat().st_size
    cap = settings.fs_read_max_bytes
    if size > cap and (offset is None and limit is None):
        raise FSAccessError(
            f"file is {size} bytes, over the {cap}-byte cap; "
            "re-request with offset=N and limit=M to paginate"
        )

    # Line-based streaming read so we don't load huge files whole when paging.
    lines: list[str] = []
    total_lines = 0
    bytes_read = 0
    truncated = False
    start = (offset - 1) if (offset and offset > 0) else 0
    want = limit if (limit and limit > 0) else None

    with resolved.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            total_lines += 1
            bytes_read += len(line.encode("utf-8", errors="replace"))
            if bytes_read > cap:
                truncated = True
                break
            if i < start:
                continue
            if want is not None and len(lines) >= want:
                truncated = True
                # Drain the counter cheaply to report total_lines.
                for _ in f:
                    total_lines += 1
                break
            lines.append(f"{i + 1}\t{line.rstrip(os.linesep)}")

    content = "\n".join(lines)
    result = ReadResult(
        path=str(resolved),
        bytes_read=bytes_read,
        lines_returned=len(lines),
        total_lines=total_lines,
        truncated=truncated,
        content=content,
    )
    _audit("read_file", resolved, bytes_read, "ok" if not truncated else "truncated")
    return result


# --- listing ---------------------------------------------------------------


@dataclass
class DirEntry:
    name: str
    is_dir: bool
    size: Optional[int]
    mtime: Optional[str]
    hidden: bool
    denied: bool  # entry exists but is denylist-blocked (still surfaced)


def list_dir(path: str, max_entries: int = 200) -> list[DirEntry]:
    resolved = resolve(path)
    if not resolved.exists():
        raise FSAccessError(f"path does not exist: {resolved}")
    if not resolved.is_dir():
        raise FSAccessError(f"not a directory: {resolved}")

    entries: list[DirEntry] = []
    for child in sorted(resolved.iterdir(), key=lambda p: p.name):
        if len(entries) >= max_entries:
            break
        try:
            st = child.stat()
            is_dir = child.is_dir()
            size = None if is_dir else st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            size, mtime, is_dir = None, None, False
        entries.append(DirEntry(
            name=child.name,
            is_dir=is_dir,
            size=size,
            mtime=mtime,
            hidden=child.name.startswith("."),
            denied=_denied(child) is not None,
        ))
    _audit("list_dir", resolved, 0, f"ok:{len(entries)}")
    return entries


# --- grep ------------------------------------------------------------------


@dataclass
class GrepHit:
    path: str
    line: int
    text: str


_RG = shutil.which("rg")


def grep(
    pattern: str,
    path: str,
    glob: Optional[str] = None,
    max_matches: int = 200,
    case_insensitive: bool = False,
) -> list[GrepHit]:
    """Search for `pattern` (regex) under `path`. Uses ripgrep if available,
    falls back to a pure-Python walker. Auto-excludes VCS/dep directories
    and lockfiles. Denylist-rejected files are skipped silently.
    """
    resolved = resolve(path)
    if not resolved.exists():
        raise FSAccessError(f"path does not exist: {resolved}")

    if _RG:
        hits = _grep_rg(pattern, resolved, glob, max_matches, case_insensitive)
    else:
        hits = _grep_py(pattern, resolved, glob, max_matches, case_insensitive)
    _audit("grep", resolved, 0, f"ok:{len(hits)}")
    return hits


def _grep_rg(
    pattern: str,
    root: Path,
    glob: Optional[str],
    max_matches: int,
    case_insensitive: bool,
) -> list[GrepHit]:
    cmd: list[str] = [
        _RG or "rg",
        "--line-number", "--no-heading", "--color=never",
        "--max-count", str(max_matches),
    ]
    if case_insensitive:
        cmd.append("--ignore-case")
    for d in _GREP_SKIP_DIRS:
        cmd += ["--glob", f"!{d}/"]
    for g in _GREP_SKIP_FILE_GLOBS:
        cmd += ["--glob", f"!{g}"]
    if glob:
        cmd += ["--glob", glob]
    cmd += [pattern, str(root)]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FSAccessError(f"grep failed: {e}")
    # rg exit 1 = no matches, 2 = error.
    if res.returncode not in (0, 1):
        raise FSAccessError(f"grep failed: {res.stderr.strip() or res.returncode}")

    hits: list[GrepHit] = []
    for raw in res.stdout.splitlines():
        # Format: path:line:text
        try:
            p, ln, text = raw.split(":", 2)
        except ValueError:
            continue
        hit_path = Path(p).resolve(strict=False)
        # Re-check denylist on every hit (rg may follow things we'd block).
        if _denied(hit_path) or not _under_allowlist(hit_path):
            continue
        hits.append(GrepHit(path=str(hit_path), line=int(ln), text=text))
        if len(hits) >= max_matches:
            break
    return hits


def _grep_py(
    pattern: str,
    root: Path,
    glob: Optional[str],
    max_matches: int,
    case_insensitive: bool,
) -> list[GrepHit]:
    import re
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise FSAccessError(f"invalid regex: {e}")

    hits: list[GrepHit] = []

    def _walk(d: Path):
        if len(hits) >= max_matches:
            return
        try:
            children = list(d.iterdir())
        except OSError:
            return
        for child in children:
            if len(hits) >= max_matches:
                return
            name = child.name
            if child.is_dir():
                if name in _GREP_SKIP_DIRS:
                    continue
                if _denied(child):
                    continue
                _walk(child)
                continue
            # File
            if _denied(child):
                continue
            if any(fnmatch.fnmatchcase(name, g) for g in _GREP_SKIP_FILE_GLOBS):
                continue
            if glob and not fnmatch.fnmatchcase(name, glob):
                continue
            if _looks_binary(child):
                continue
            try:
                with child.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        if regex.search(line):
                            hits.append(GrepHit(
                                path=str(child.resolve()),
                                line=i,
                                text=line.rstrip("\n"),
                            ))
                            if len(hits) >= max_matches:
                                return
            except OSError:
                continue

    if root.is_file():
        _walk(root.parent)  # walk containing dir, glob filter will narrow
    else:
        _walk(root)
    return hits


# --- audit -----------------------------------------------------------------


def _audit(tool: str, path: Path, bytes_read: int, result: str) -> None:
    """Best-effort audit log. Never raise — audit failure shouldn't block
    the read itself."""
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO fs_reads (ts, tool, path, bytes, result) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), tool, str(path),
                 bytes_read, result),
            )
    except Exception:
        pass


# --- public helpers for the MCP tool layer ---------------------------------


def read_result_to_dict(r: ReadResult) -> dict:
    return {
        "path": r.path,
        "bytes_read": r.bytes_read,
        "lines_returned": r.lines_returned,
        "total_lines": r.total_lines,
        "truncated": r.truncated,
        "content": r.content,
    }


def entries_to_list(entries: Iterable[DirEntry]) -> list[dict]:
    return [
        {
            "name": e.name,
            "is_dir": e.is_dir,
            "size": e.size,
            "mtime": e.mtime,
            "hidden": e.hidden,
            "denied": e.denied,
        }
        for e in entries
    ]


def hits_to_list(hits: Iterable[GrepHit]) -> list[dict]:
    return [{"path": h.path, "line": h.line, "text": h.text} for h in hits]
