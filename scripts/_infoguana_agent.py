"""Host-agent detection and the memory-system override text.

Shared by both SessionStart hook scripts so the override exists once:
infoguana-onboard.py (single-shot) and infoguana-onboard-chunk.py
(chunked, via its --override mode).

Why it lives outside the onboard blob: the blob is built server-side and
cached per (project, budget), but which built-in memory store to steer
away from depends on the *host agent*, which only the hook process knows.
Rendering it client-side keeps the server's cache key simple and one blob
correct for every agent.
"""
from __future__ import annotations

import os

_OVERRIDE_HEAD = """
# memory system override

The user uses the `infoguana` MCP server as their authoritative persistent memory — NOT your built-in per-agent memory. It is shared across every agent they run, which is the point: notes saved from one agent must be readable from another.

- Save everything worth retaining (user facts, feedback, project context, references) via `add(content=..., project=<this>)`.
"""

# Agent-specific stores to steer away from. Keeping infoguana the single
# writable store is what lets the user switch agents without the corpus
# forking into per-agent islands.
_OVERRIDE_CLAUDE = """- Do NOT write to `~/.claude/projects/*/memory/*.md` or create/update `MEMORY.md` files there.
- If you see an existing `~/.claude/projects/*/memory/` directory, treat it as legacy — do not extend it. Migrate substance to infoguana if still relevant.
"""

_OVERRIDE_CODEX = """- Do NOT persist durable memory into Codex's own store under `~/.codex/`. Treat anything already there as legacy — do not extend it.
- `AGENTS.md` stays a short pointer to infoguana; put the substance in infoguana, not in the file.
"""


def detect_agent() -> str:
    """Best-effort identification of the host agent, used only to name the
    right built-in memory store in the override text. Override with
    INFOGUANA_AGENT=claude|codex. An unrecognized host gets the generic
    text, which is correct for every agent — just less specific."""
    explicit = (os.environ.get("INFOGUANA_AGENT") or "").strip().lower()
    if explicit in ("claude", "codex"):
        return explicit
    # Claude is checked first on purpose: CODEX_HOME is exported into the
    # whole IDE environment wherever the Codex extension is installed, so
    # it leaks into other agents' sessions and is not evidence that Codex
    # is the host. CLAUDECODE is set by Claude Code and inherited by
    # anything it spawns — so it leaks too, just narrowly (a Codex session
    # started from a Claude Code shell reads as claude). Neither probe is
    # authoritative; INFOGUANA_AGENT is the fix when it matters.
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    return "generic"


def memory_override() -> str:
    """The memory-system directive for the detected host agent: the shared
    text, plus the clause naming that agent's own built-in store to leave
    alone. An unrecognized host gets the shared text only — naming a store
    we are guessing at would be an instruction pointing somewhere real."""
    agent = detect_agent()
    if agent == "claude":
        return _OVERRIDE_HEAD + _OVERRIDE_CLAUDE
    if agent == "codex":
        return _OVERRIDE_HEAD + _OVERRIDE_CODEX
    return _OVERRIDE_HEAD
