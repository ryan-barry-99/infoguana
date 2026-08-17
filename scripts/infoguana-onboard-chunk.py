#!/usr/bin/env python3
"""SessionStart hook for Claude Code: fetches one slice of infoguana's
onboard blob and emits it as additionalContext.

Each hook's additionalContext is capped at ~2KB inline, but the cap is
*per-hook*. The installer registers N entries of this script with
different chunk indices; all N slices land inline at session start with
no truncation, so the agent sees the whole blob without a Read-tool
round-trip. N is derived from measured blob size at install time — see
routes/onboard.chunks_needed and _chunks_fitting.

Args:
    sys.argv[1]: chunk index (0-based)
    sys.argv[2]: total chunks (matches the of= query param)

Reads INFOGUANA_URL, INFOGUANA_TOKEN, INFOGUANA_ONBOARD_BUDGET from
~/.infoguana.env (preferred) or the env.

Never blocks the session: a missing token or unparseable argv is a
no-op, and a failed fetch degrades to a one-line notice instead of
silence. Silence was the original behavior and it was wrong — a dropped
slice looked exactly like a complete load, so an unseen rule read as a
rule that didn't apply.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _infoguana_agent import memory_override  # noqa: E402
from _infoguana_setup import authed_request  # noqa: E402
from _infoguana_setup import load_env_file as _load_env_file  # noqa: E402


def _emit(text: str) -> int:
    """Write one SessionStart additionalContext payload to stdout."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))
    return 0


def main() -> int:
    # The memory-system override rides its own hook rather than being
    # appended to a slice. Appending it would add ~1.1KB to whichever
    # slice carried it and push that one over the inline cap, and the
    # sizing search would then have to model a client-side addition it
    # cannot see. A dedicated entry costs one hook and keeps the override
    # emitted exactly once regardless of assembly order.
    #
    # This is also how the chunked path gained the override at all: it
    # previously existed only in the single-shot script, so Claude Code —
    # which has used chunked delivery all along — never received the
    # directive telling it to prefer infoguana over its own file-based
    # memory. The store it was told to avoid is the one its harness
    # actively points it at.
    # Opt-out for callers that already seed context themselves (e.g. the
    # infoguana web-UI chat, which prepends a project-scoped seed before
    # spawning `claude -p`). Without this, the hook fires from the
    # service's cwd and injects `infoguana` memories on every chat.
    #
    # Checked ahead of --override, not just ahead of the slice path: the
    # override text is context too, and a caller that asked for silence
    # should not receive it on every turn.
    if os.environ.get("INFOGUANA_HOOK_DISABLE") == "1":
        return 0

    if len(sys.argv) >= 2 and sys.argv[1] == "--override":
        # Load the env file before detecting the agent, not after. The
        # documented escape hatch is INFOGUANA_AGENT in ~/.infoguana.env
        # — the only file either installer writes — and returning here
        # before reading it meant the knob did nothing on the one path
        # whose whole output depends on it.
        _load_env_file(Path.home() / ".infoguana.env")
        return _emit(memory_override())

    if len(sys.argv) < 3:
        return 0
    try:
        index = int(sys.argv[1])
        total = int(sys.argv[2])
    except ValueError:
        return 0

    # Stagger by chunk index so the agent sees chunks in registration
    # order. The harness assembles hook outputs in completion order; with
    # server-side caching, the first chunk takes ~300ms (cold) and the
    # rest serve in ~5-10ms. 50ms-per-index stagger dominates that.
    #
    # Capped, because the delay is not free: every slice re-slices
    # whatever the server's build cache holds at the moment it asks, so
    # cross-slice consistency lasts only as long as that cache entry. An
    # uncapped ramp reached 6.4s at 128 chunks — close enough to the 10s
    # TTL that a concurrent write could shift the boundaries mid-delivery
    # and leave the stitched brief with a lost or duplicated seam. The
    # cap only compresses the tail, where the ordering nudge has already
    # done its work.
    if index > 0:
        time.sleep(min(index, 16) * 0.05)

    _load_env_file(Path.home() / ".infoguana.env")
    url = (os.environ.get("INFOGUANA_URL") or "http://localhost:8789").rstrip("/")
    token = os.environ.get("INFOGUANA_TOKEN", "")
    budget = os.environ.get("INFOGUANA_ONBOARD_BUDGET", "4000")
    if not token:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = Path(project_dir).name

    req = authed_request(
        f"{url}/onboard/{project}/chunk/{index}?of={total}&budget_tokens={budget}",
        token,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            chunk = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Emitting nothing here is what made a partial memory load
        # indistinguishable from a complete one: the agent proceeds
        # confidently on a brief with a hole in it, and the only symptom
        # is a rule that never fired.
        #
        # The notice says what the agent can act on and nothing else. The
        # slice index, the total, and the transport error belong on stderr
        # — they go to the hook log for whoever maintains this, not into
        # the agent's context. An agent told "chunk 8/46 failed" cannot do
        # anything with 8 or 46; it can only re-fetch its memory, so that
        # is all the notice asks for.
        #
        # Only slice 0 emits it. An outage fails every slice at once, so
        # per-slice emission multiplies one 127 B sentence by the whole
        # registered count — ~2.2 KB at 17 chunks, ~9 KB at 71 — inside a
        # delivery path whose entire premise is a ~1.7 KB per-hook budget.
        # The stderr line stays on every slice, so the operator log still
        # shows exactly which ones failed. The tradeoff is that a lone
        # failure of some slice other than 0 is now silent to the agent;
        # that case is rare next to the whole-server outage, and it is the
        # case where the rest of the brief did arrive intact.
        print(f"infoguana: chunk {index + 1}/{total} failed for project "
              f"{project!r}: {type(e).__name__}: {e}", file=sys.stderr)
        if index != 0:
            return 0
        return _emit(
            "_Part of this project's memory failed to load. Call "
            "`context(project=...)` for the full set before relying on "
            "rules or plans._\n"
        )

    # An empty body is legitimate: _line_aligned_chunks yields empty
    # leading/trailing slices for short blobs, and the route returns ""
    # for those. Distinguish "nothing to send" from "couldn't send" —
    # warning on the former would fire on every small project.
    if not chunk:
        return 0

    return _emit(chunk)


if __name__ == "__main__":
    raise SystemExit(main())
