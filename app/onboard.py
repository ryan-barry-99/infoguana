"""SessionStart context builder. The /onboard/<project> endpoint and the
infoguana-onboard hook script both call build() to produce a single text blob
that gets injected into a fresh agent session. The blob is agent-neutral —
Claude Code and Codex both consume it — so nothing here should name a
specific agent or its private file layout."""
import threading
import time

from app import db, graph


# Per-project memoization of build() output. The chunked SessionStart
# hooks fire 16 simultaneous GETs against /onboard/<project>/chunk/<i>; each
# would otherwise call build() concurrently and race on the single shared
# SQLite connection in db.py, producing InterfaceError / IndexError 500s.
# Caching also guarantees all 16 chunks derive from the same blob, so the
# agent sees a self-consistent view.
_BUILD_CACHE_TTL_SECONDS = 10.0
_build_cache: dict[str, tuple[str, float]] = {}
_build_lock = threading.Lock()


DEFAULT_PROTOCOL = """\
You are a coding agent (Claude Code, Codex, or similar) connected to the \
user's shared cross-project \
**infoguana** via the `infoguana` MCP server. Infoguana is the user's authoritative \
memory across every project they work on. Use it.

## When to call which tool

**At task start**: you are receiving this message because a `SessionStart` \
hook ran `context()` for the current project. The notes below are the \
most reachable memories — read them. Pending plans and tasks for this \
project are pinned to the top; check those before starting new work. If you \
need more, call:
- `context(project=<this>, budget_tokens=6000)` for more from this project
- `search(query=...)` for hybrid BM25+semantic search across all projects
- `similar(text=...)` to find notes near a verbatim problem description
- `get(id=NNN)` to fetch a specific note when a `#NNN` reference points at one you haven't seen yet
- `plans(project=<this>, status='pending')` to list outstanding tracked work

For plan/decision lookups — where the answer hinges on the typed-edge graph \
(`implements`, `supersedes`, `bundled_with`, `prerequisite_for`) — pass \
`include_edges=True` to `search` / `similar`. Each hit then \
carries `edges_out` and `edges_in` (grouped by edge type, with target id \
+ preview) inline, so you see what a plan implements or what supersedes a \
decision in one shot. For multi-hop walks across the graph, call \
`traverse(start_id, edge_type=...)`.

**During a task** that feels familiar: search infoguana *before* you start \
implementing. If past you (or another project) has solved this, use that.

**Previews vs full bodies**: hits from `search` / `similar` and \
notes from `context` are returned as 1-5 line haiku previews by \
default, marked with `preview: True`. Previews are for **triage** — they \
tell you which notes are relevant; they are not safe to cite. Before \
stating a fact, decision, design point, or recommendation that's anchored \
on a preview, fetch the full body via `get(id)`, \
`get_many(ids=[...])`, or `expand_top=N` on the next search call. \
Cite from verified content, not from a hand-sized summary.

**When the user asks something as if you already know the context** — \
references a project, person, decision, acronym, or past event you don't \
recognize, or asks a question that assumes background you don't have — \
search infoguana *before* answering or asking them to explain. The user's \
memory and yours are meant to be shared via infoguana; if you're missing \
context, that's a signal to read, not to guess or punt.

**When you learn something worth retaining**: call `add(content=..., \
project=<this>)`. Capture the **how** and **why**, not just the category. \
Include code snippets inline — infoguana is cross-repo, so a bare path like \
`foo.cpp:42` rots when accessed from a different project. Save the substance.

**When two notes meaningfully connect**: propose a \
`link(from_id, to_id, edge_type)` and confirm with the user before \
creating it. Edge types: `implements`, `caused_by`, `supersedes`, \
`references`, `bundled_with`, `prerequisite_for`. Only propose links you can \
justify in one sentence — speculative links pollute the graph.

**When updating something you already saved**: call `update(id=NNN, \
content=...)` rather than re-adding a near-duplicate. Search first to find \
the existing id; edit in place so links and history survive.

**When tracked work finishes** (a plan or task you've been carrying): call \
`plan_complete(id=NNN, pr_urls=[...])` so it retires gracefully. Plans \
graduate into `feature` notes via a synthesis step; tasks just close. Don't \
leave stale `pending` items at the top of every future session's context.

## Good vs bad memories

❌ Bad — what without how:
```
"figured out FTS5 BM25 tuning"
"fixed the rate limiting bug"
"set up Docker properly"
```

✅ Good — actionable, self-contained:
```
"FTS5 BM25 for short notes: k1=1.2, b=0.65. Default b=0.75 over-penalized
short content (~30-100 tokens) and tanked recall."

"Postgres advisory locks (pg_try_advisory_lock) don't survive connection
drops — fine for in-request critical sections, wrong for long-running
cron jobs. Use a row-level lock or a leases table for those."
```

## What NOT to save

- Ephemeral task state — use the todo list instead
- Anything already in the PR description / commit message
- Duplicates — `search` first; update an existing note rather than \
near-duplicate it
"""


def build(project: str, budget_tokens: int = 4000) -> str:
    """Produce the plain-text blob that goes into additionalContext.

    The harness caps each hook's `additionalContext` at ~2KB
    inline, but the cap is *per-hook*, not aggregate. The install script
    therefore registers N (default 16) UserPromptSubmit hook entries that
    each fetch a different line-aligned slice of this blob via
    /onboard/<project>/chunk/<i>?of=<n>; all N slices land inline at
    session start with no truncation. So this function still produces
    the full blob — protocol intro included — and chunking happens at
    the route layer."""
    protocol = db.get_protocol("default") or ""
    proj_meta = db.get_project(project)
    project_desc = (proj_meta or {}).get("description") or ""

    ctx = graph.build_context(project=project, budget_tokens=budget_tokens)

    parts: list[str] = []
    parts.append("# infoguana memory protocol\n")
    parts.append(protocol.rstrip())
    parts.append(f"\n\n# current project: `{project}`\n")
    if project_desc:
        parts.append(project_desc.rstrip() + "\n")
    rules = ctx.get("rules") or []
    global_rules = [r for r in rules if r.get("scope") == "global"]
    project_rules = [r for r in rules if r.get("scope") != "global"]

    def _emit_rule(r: dict) -> None:
        tags_str = " ".join(f"#{t}" for t in r.get("tags") or [])
        header = f"\n### rule #{r['id']}"
        if tags_str:
            header += f" · {tags_str}"
        parts.append(header + "\n")
        content = (r.get("content") or "").strip()
        if content:
            parts.append(content + "\n")
        desc = (r.get("description") or "").strip()
        if desc:
            parts.append(f"\n_{desc}_\n")

    if global_rules:
        parts.append(
            "\n## global rules "
            "(apply in every project — read before acting)\n"
        )
        for r in global_rules:
            _emit_rule(r)

    if project_rules:
        parts.append(
            f"\n## rules for `{project}` "
            f"(project-specific constraints — read before acting)\n"
        )
        for r in project_rules:
            _emit_rule(r)

    parts.append(
        f"\n## relevant memories from infoguana "
        f"(~{ctx['total_tokens_est']} tokens, budget {budget_tokens})\n"
    )
    notes = ctx.get("notes") or []
    if not notes:
        parts.append(
            "\n_No relevant memories yet. As you learn things in this project, "
            "call `add(content=..., project=...)` to start populating it._\n"
        )
    else:
        for n in notes:
            tags_str = " ".join(f"#{t}" for t in n.get("tags") or [])
            proj_str = n.get("project") or "(no project)"
            header = f"\n### `{n['type']}` · {proj_str}"
            if tags_str:
                header += f" · {tags_str}"
            header += f" · reach={n.get('reachability', 0)}"
            parts.append(header + "\n")
            content = (n.get("content") or "").strip()
            if content:
                parts.append(content + "\n")
            desc = (n.get("description") or "").strip()
            if desc:
                parts.append(f"\n_{desc}_\n")

    return "".join(parts)


def build_cached(project: str, budget_tokens: int = 4000) -> str:
    """Memoized wrapper around build(). Serializes concurrent callers and
    reuses the result for ~10s. See cache-block docstring above."""
    key = f"{project}|{budget_tokens}"
    now = time.time()
    with _build_lock:
        cached = _build_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
        # Expiry is checked on read but nothing else removes an entry, so
        # a miss is the only chance to drop stale ones. That was harmless
        # while a request inserted one key; /onboard/sizing inserts one
        # per project, and budget_tokens is caller-controlled, so every
        # distinct budget mints a fresh key for every project (~0.6 MB
        # retained per sizing request against a 27-project corpus).
        # Pruning here bounds the dict at the live working set.
        for stale in [k for k, v in _build_cache.items() if v[1] <= now]:
            del _build_cache[stale]
        blob = build(project=project, budget_tokens=budget_tokens)
        _build_cache[key] = (blob, now + _BUILD_CACHE_TTL_SECONDS)
        return blob
