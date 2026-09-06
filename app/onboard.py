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
    therefore registers N SessionStart hook entries that each fetch a
    different line-aligned slice of this blob via
    /onboard/<project>/chunk/<i>?of=<n>; all N slices land inline at
    session start with no truncation. So this function still produces
    the full blob — protocol intro included — and chunking happens at
    the route layer.

    N is derived from measured blob size at install time rather than
    hardcoded, because the blob outgrew every fixed value it was ever
    given: a 16-chunk split was sized when this produced ~22KB, and by
    the time the largest project's rule set reached ~38KB each slice was
    ~3.7KB against a ~2KB cap — roughly the back half of all sixteen
    was being dropped, silently and mid-rule. See
    routes/onboard.chunks_needed and _chunks_fitting."""
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
    # Skills pin as a menu, not the meals: one line each, body fetched by
    # id when the agent decides one applies (see graph._pin_skills).
    #
    # Formatted to match how a harness lists its *own* skills — a flat
    # `- name: description` list under a "the following skills are
    # available" lead-in — rather than the `### type · project · tags`
    # shape memories use below. The agent already knows how to read a
    # skill listing and dispatch off one; making infoguana's skills look
    # like a different kind of object would mean teaching it a second
    # convention for the same job. Scope isn't shown for the same reason:
    # a skill's presence in the list already means it applies here.
    #
    # The how-to-use-these text lives here rather than in the protocol
    # because the protocol is a DB row seeded once at first boot
    # (db.seed_protocol_if_missing) and thereafter owned by the user —
    # guidance added to DEFAULT_PROTOCOL would reach fresh installs only,
    # and never the machines already running. Rendering it from code puts
    # it in front of every session, and it costs nothing when a project
    # has no skills.
    #
    # Emitted BEFORE the rules, though rules outrank it in priority. The
    # manifest runs a few hundred tokens — ~870 at nine skills, and it
    # grows with each one added — while the rule set is the largest, fastest-
    # growing section of the blob (~38KB of rules on the largest project), so
    # any delivery path that truncates loses whatever sits behind them.
    # That is not hypothetical: this section used to render at 61% depth,
    # and Codex — which onboards through a single un-chunked hook rather
    # than N inline slices — received the rules and no skill manifest at
    # all, so it never learned which skills existed. Cheapest durable fix
    # is to put the short, load-bearing list where truncation can't reach
    # it. Ordering here is about surviving the transport, not precedence.
    skill_entries = ctx.get("skills") or []
    # Not `if skill_entries` alone: a cap exhausted by the first entry
    # returns an empty list with skills_truncated set, and guarding on the
    # rendered list would then suppress the very notice saying the listing
    # was cut. The blob would read exactly like a project with no skills.
    # Same argument as the rules block below, which already gets this right.
    if skill_entries or ctx.get("skills_truncated"):
        parts.append("\n## skills available\n")
        parts.append(
            "\nTreat this exactly as you would your harness's own skill "
            "listing: each entry is a name, an id, and the trigger condition "
            "its author wrote. The one difference is loading — call "
            "`get(id)`, or `get_skill(name)` if you no longer have this "
            "listing, to read the full instructions, then follow them in "
            "place of your default approach. Skills are stored as SKILL.md "
            "documents verbatim, so they carry their own structure. Never act "
            "on a skill from its one-line description alone: the description "
            "says *when*, the body says *how*.\n"
        )
        for s in skill_entries:
            parts.append(
                f"\n- {s['name']} (#{s['id']}): "
                f"{(s.get('description') or '').strip()}"
            )
        if ctx.get("skills_truncated"):
            parts.append(
                "\n\n_More skills exist than fit this listing — it was "
                "truncated. Call `search(query=..., type='skill')` if none "
                "of the above matches what you're doing._"
            )
        parts.append("\n")

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

    # Rules going missing is the failure the pin exists to prevent, so say
    # so in the blob rather than only in the context payload — nothing was
    # rendering `rules_truncated`, which made a dropped constraint look
    # exactly like a project that never had one.
    # The recovery call must be one that can actually recover them.
    # `context(include_types=['rule'])` cannot: RULES_TOKEN_CAP is applied
    # unconditionally inside `_pin_rules`, not relaxed by the type filter
    # and not derived from budget_tokens, so that call returns the
    # identical truncated set. An agent told to check, checking, and
    # seeing the same list concludes nothing is missing — worse than
    # silence, because now it believes it verified.
    # No `and rules exist` guard: truncation-to-zero is the case that most
    # needs saying, and it's the one a guard on the rendered list would
    # suppress. With no truncation this never fires regardless.
    # No tool returns a project's full rule set inline, so the notice
    # must not promise one. `search` takes `query` as a required
    # positional, so a call without it is a TypeError before it reaches
    # the server; its `project` filter is an equality match, so passing
    # this project drops every global rule — and globals sort first, so
    # they are the ones most likely still in the listing. `search` also
    # returns previews, hence the second call for the bodies the pin
    # would have emitted in full.
    if ctx.get("rules_truncated"):
        parts.append(
            "\n_Some rules were dropped from this listing — it hit a size "
            "bound. No tool returns the full set inline: call "
            "`search(query='<what you are about to do>', type='rule', "
            "limit=50)` with `project` left unset so globals are included, "
            "then `get_many` the hits to read the bodies, before acting on "
            "anything constraint-shaped._\n"
        )

    # Pinned tracked work. This section exists because the protocol text
    # promises it ("Pending plans and tasks for this project are pinned to
    # the top of context output") and because `_pin_active_work` charges
    # every one of these against `budget_tokens` and adds it to
    # `seen_note_ids` — so before this was rendered, the blob paid for the
    # plans AND suppressed them from the BFS, spending budget on content
    # it did not contain. Measured on the heaviest project at the 4000
    # default: 2,275 tokens, 57% of the notes budget, for 16 plans whose
    # bodies appeared nowhere.
    #
    # Rendered before the memories, matching the order the budget is spent
    # in and the priority the protocol claims.
    active = ctx.get("active_plans") or []
    if active:
        parts.append(
            f"\n## pending plans and tasks for `{project}` "
            f"(~{sum(p.get('tokens_est', 0) for p in active)} tokens)\n"
        )
        parts.append(
            "\nCheck these before starting new work. Call "
            "`plan_complete(id=...)` when one finishes so it stops pinning "
            "here; `get(id)` for the full body.\n"
        )
        for p in active:
            due = ""
            if p.get("due_state"):
                days = p.get("due_in_days")
                when = p.get("due_date")
                if p["due_state"] == "overdue" and isinstance(days, int):
                    due = f" · **overdue {abs(days)}d** (due {when})"
                elif p["due_state"] == "today":
                    due = " · **due today**"
                elif when:
                    due = f" · due {when}"
            tags = " ".join(f"#{t}" for t in p.get("tags") or [])
            head = f"\n- **#{p['id']}** ({p.get('type', 'plan')}"
            if p.get("status"):
                head += f", {p['status']}"
            parts.append(head + ")" + due + (f" · {tags}" if tags else ""))
            body = (p.get("content") or "").strip()
            if body:
                parts.append(f"\n  {body}")
        parts.append("\n")

    # Report what this section actually cost, not the whole payload.
    # `total_tokens_est` includes the pinned rules and the skill manifest,
    # which are exempt from budget_tokens — printing it beside the budget
    # under a heading that says "relevant memories" bills the exempt
    # sections to the notes allowance and reads as a 3x overrun. Agents
    # believed it and reported it: one session summarized its own context
    # as "~13k tokens of recalled notes (against a 4k budget)" when the
    # notes were 3,974 of 4,000 and the other 9,462 was exempt rules plus
    # manifest. The exempt cost is real and worth showing — it just isn't
    # this budget's, so it goes in its own clause.
    exempt = ctx.get("rules_tokens_est", 0) + ctx.get("skills_tokens_est", 0)
    header = (
        f"\n## relevant memories from infoguana "
        f"(~{ctx.get('notes_tokens_est', 0)} tokens, budget {budget_tokens}"
    )
    if exempt:
        header += (
            f"; the rules and skills above add ~{exempt} more, exempt from "
            f"that budget"
        )
    parts.append(header + ")\n")
    notes = ctx.get("notes") or []
    # An empty note list has two very different causes, and the reader
    # can't tell them apart from the output: the project genuinely has
    # nothing saved, or the pins ate the budget before any note could be
    # considered. Treat "nearly all of the budget already spent" as the
    # latter — a note that doesn't fit in the remainder is
    # indistinguishable from one that was never there.
    # Only what the BFS charged for notes counts here — rules and skills
    # are exempt from the budget, so including their cost would report a
    # rule-heavy project as crowded-out when its notes were never
    # squeezed at all.
    spent = ctx.get("notes_tokens_est", ctx.get("total_tokens_est", 0))
    crowded_out = spent >= budget_tokens * 0.9
    if not notes and crowded_out:
        # `spent` is notes_tokens_est, and the only pin still charging it
        # before the BFS reaches a note is _pin_active_work — rules and
        # skills accumulate against their own exempt counters. So active
        # work is what crowded the notes out here, and naming the rules
        # (as this message used to) pointed at the one section guaranteed
        # not to be responsible. Saying "no memories yet" would be a lie
        # the reader can't check — distinguish the causes and name the fix.
        parts.append(
            f"\n_No memories fit: pinned active work already cost "
            f"~{spent} of the {budget_tokens}-token "
            f"budget. This project may well have memories — they were "
            f"crowded out, not absent. Call `context(project=..., "
            f"budget_tokens={max(budget_tokens * 2, 8000)})` to read them, and "
            f"raise INFOGUANA_ONBOARD_BUDGET so future sessions include them "
            f"inline._\n"
        )
    elif not notes:
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
