"""MCP server exposing infoguana over Streamable HTTP.

Designed to be called by project-local Claude agents. Each tool is a thin
adapter over the same functions the web UI uses."""
import difflib
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import classify, db, duedate, embed, export, fs_access, graph, inference, pipeline, plans, skills, tag_suggest
from app import github as gh
from app.config import settings
from app.models import NoteCreate, NoteType, NoteUpdate


log = logging.getLogger(__name__)


LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def _transport_security() -> TransportSecuritySettings:
    """Build DNS-rebinding protection from `mcp_allowed_hosts`.

    Bearer auth (BearerAuthMiddleware in main.py) is the real gate; this is
    defense in depth.

    With no extra hosts configured this returns settings with the checks
    explicitly disabled. Returning None instead would NOT leave them off:
    FastMCP auto-enables a loopback-only allowlist whenever it is handed
    None and its own `host` is loopback, and that host defaults to
    127.0.0.1 because this module never passes one — `settings.host` is
    uvicorn's binding and does not reach the SDK. The result would be a
    421 for every client arriving by LAN or tailnet address, which is the
    common deployment and the opposite of the documented default.

    Each configured host yields both an http and an https origin. The SDK
    matches Origin as a whole string including scheme, so a name listed by
    an operator running behind a TLS proxy would otherwise pass the Host
    check and still be refused 403.
    """
    if not settings.mcp_allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = LOOPBACK_HOSTS + settings.mcp_allowed_hosts
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")],
    )


mcp = FastMCP(
    name="infoguana",
    instructions=(
        "Personal cross-project knowledge base. Use these tools at the start of a task to "
        "surface past notes, decisions, and ideas that might be relevant to the current "
        "project. ALWAYS pass the current project name (usually the cwd basename) when "
        "calling add so notes are attributed correctly. "
        "Pending plans for the current project are pinned to the top of context "
        "output — check them before starting new work, and call plan_complete when "
        "one finishes so it retires gracefully (and optionally spawns a lessons-learned note). "
        "When a note you save connects meaningfully to an existing note — supersedes a "
        "prior decision, implements an earlier plan, cites a #id, was caused by a known "
        "incident — propose a link with the appropriate edge_type and confirm with "
        "the user before creating it. Edge types: implements, caused_by, supersedes, "
        "references, bundled_with, prerequisite_for. Only propose links you can justify "
        "in one sentence; speculative links pollute the graph."
    ),
    streamable_http_path="/",
    transport_security=_transport_security(),
)


VALID_TYPES = {"idea", "memory", "feedback", "feature", "reference", "plan",
               "task", "rule", "skill"}
# Types a caller may *filter reads by*. Superset of the write set because
# `unsorted` is a real state worth listing but not worth assigning by hand.
# Kept as its own name: the two sets answer different questions, and
# collapsing them is what once made `search(type='skill')` silently return
# unfiltered results when `skill` was missing from the write gate.
READABLE_TYPES = VALID_TYPES | {"unsorted"}
VALID_EDGE_TYPES = {
    "implements", "caused_by", "supersedes", "references",
    "bundled_with", "prerequisite_for",
}
# Trust labels for the captured claim.
VALID_CONFIDENCES = {"stated", "inferred", "speculative", "unspecified"}

# Max hits a search/context call can expand into full bodies (rest stay as
# previews). Caps the structural budget win so a greedy `expand_top=999`
# can't reintroduce the full-mode regression. get_many is the explicit
# escape hatch when an agent legitimately needs many bodies at once.
MAX_EXPAND_TOP = 5
# Ceiling on `provenance_note` in preview-mode hits. See `_note_dict`.
PREVIEW_PROVENANCE_CHARS = 200
# Max ids get_many can pull in one call. Bounded so a runaway loop or
# misread can't drag in the whole corpus.
MAX_GET_MANY = 20

# Tools the in-app /chat agent is allowed to call. Subset of all @mcp.tool
# registrations below — admin-ish tools (read_file, list_dir, grep, export,
# infer_edges, tag_suggest, plans, get_many) are intentionally
# withheld from the chat allowlist. Single source of truth: chat.py imports
# this and assembles the `--allowedTools` arg from it, so adding a new
# chat-eligible tool only requires updating one list.
CHAT_ALLOWED_TOOLS: tuple[str, ...] = (
    "search", "similar", "add", "update", "delete",
    "recent", "get", "get_skill", "context",
    "link", "unlink", "traverse",
    "plan_complete",
    "gh_issue_get", "gh_issue_comments", "gh_issue_list",
    "gh_pr_get", "gh_pr_comments",
    "gh_issue_comment_post", "gh_issue_create",
)


def _note_dict(note, preview: bool = False) -> dict:
    """Serialize a note for MCP responses. With `preview=True` (used by
    search / similar), the `content` field carries a short
    haiku-generated summary instead of the full body — agents call
    get for the full text only on the hits they actually want to
    read. Falls back to first-line truncation if the preview
    column is unset for legacy rows. get / recent / add
    return values keep full content (preview=False)."""
    if preview:
        body = note.preview or classify.derive_fallback_preview(note.content)
    else:
        body = note.content
    d = {
        "id": note.id,
        "content": body,
        "type": note.type,
        "project": note.project,
        "tags": note.tags,
        "created_at": note.created_at.isoformat(),
        "version": note.version,
    }
    if preview:
        d["preview"] = True
    if note.type in ("plan", "task"):
        d["status"] = note.status
        d["linked_prs"] = note.linked_prs
        if note.due_date:
            d["due_date"] = note.due_date
            disp = duedate.display(note.due_date)
            if disp:
                d["due_state"] = disp["bucket"]
                d["due_in_days"] = disp["days_until"]
    # Surface the trust dimension. Always include confidence (so
    # 'unspecified' is visibly ugly and discourages skipping); include the
    # free-text detail only when set.
    d["confidence"] = note.confidence
    if note.provenance_note:
        # Clamp in preview mode. A preview hit is budgeted at roughly a
        # preview's worth of tokens, and provenance is the one field with
        # no length discipline at all — `skill` notes average ~1000 chars
        # of it against a ~140-char preview, so an unclamped hit costs
        # several times what the caller budgeted for it. Full text stays
        # on expand_top / get / get_many, where a full body is expected.
        if preview and len(note.provenance_note) > PREVIEW_PROVENANCE_CHARS:
            d["provenance_note"] = (
                note.provenance_note[:PREVIEW_PROVENANCE_CHARS].rstrip() + "…"
            )
        else:
            d["provenance_note"] = note.provenance_note
    return d


def _grouped_edges_for(note_ids: list[int],
                       confirmed_only: bool = True) -> dict[int, dict]:
    """For each id, return {edges_out, edges_in} grouped by edge_type. Each
    grouped value is a list of {id, preview} so an agent can read the
    relationship without an extra get. Empty edge types are omitted;
    a note with no edges yields {} so the caller can drop the keys entirely."""
    if not note_ids:
        return {}
    by_id = db.batch_links_for(note_ids, confirmed_only=confirmed_only)
    out: dict[int, dict] = {}
    for nid, views in by_id.items():
        grouped: dict[str, dict[str, list[dict]]] = {"edges_out": {}, "edges_in": {}}
        for v in views:
            bucket = grouped["edges_out"] if v.direction == "out" else grouped["edges_in"]
            bucket.setdefault(v.edge_type, []).append({
                "id": v.target_id,
                "preview": v.target_preview,
            })
        result: dict = {}
        if grouped["edges_out"]:
            result["edges_out"] = grouped["edges_out"]
        if grouped["edges_in"]:
            result["edges_in"] = grouped["edges_in"]
        out[nid] = result
    return out


@mcp.tool(name="search")
def infoguana_search(
    query: str,
    limit: int = 10,
    type: Optional[str] = None,
    project: Optional[str] = None,
    include_edges: bool = False,
    confirmed_only: bool = True,
    expand_top: int = 0,
) -> dict:
    """Search infoguana using hybrid BM25 + semantic vector search.

    Use this at task start to find past notes, ideas, or decisions relevant to
    what you're working on — especially across different projects.

    Each hit's `content` field carries a short haiku-generated preview (1-5
    lines, ~200 chars) plus `preview: True`. Pass `expand_top=N` (max 5) to
    receive full bodies inline for the top-N highest-scoring hits — useful
    when you're confident the obvious-most-relevant notes will need to be
    read in full anyway, and want to skip the per-id get round trip.
    The remaining hits stay as previews. For ad-hoc deeper reads, call
    `get(id)` or `get_many(ids=[...])`.

    **Previews are for triage, not citation.** A preview is a haiku-sized
    summary of the note — it tells you whether to look closer, but it can
    omit nuance, context, dates, or qualifications that change the meaning.
    Before stating a fact, recommendation, or design point that's anchored
    on a preview, fetch the full body via `get` / `get_many` /
    `expand_top` and read the actual text. Cite from verified content, not
    from a hand-sized summary.

    Set `include_edges=True` for plan/decision lookups where the *shape* of
    the answer is the typed-edge graph (`implements`, `bundled_with`,
    `prerequisite_for`, `supersedes`). Each hit then carries `edges_out` and
    `edges_in` dicts grouped by edge type — directionality kept separate
    because the verb flips ("X implements Y" vs "X is implemented by Y").
    Each edge target is `{id, preview}` (preview is the first line of the
    target note) so you can usually skip the follow-up get.

    Args:
        query: Natural language search query.
        limit: Max results to return (default 10).
        type: Optional filter: idea|memory|feedback|feature|reference|plan|
            task|rule|skill|unsorted. An unrecognized type is an error.
        project: Optional filter to a specific project name.
        include_edges: Attach typed-edge neighbors to each hit (default False).
        confirmed_only: When include_edges, skip unconfirmed agent-proposed
            edges (default True).
        expand_top: Inline full bodies for this many top hits (default 0,
            max 5). Rest stay as previews.
    """
    # Error rather than dropping an unrecognized filter. Silently returning
    # unfiltered hits is worse than no filter at all: the caller reasons
    # about the result set as if it were narrowed.
    if type is not None and type not in READABLE_TYPES:
        return {"error": f"type must be one of {sorted(READABLE_TYPES)}"}
    type_filter: Optional[NoteType] = type  # type: ignore[assignment]
    try:
        qv = embed.engine().embed(query)
    except Exception:
        qv = None
    hits = db.hybrid_search(query, query_vec=qv, limit=limit,
                            type_filter=type_filter, project_filter=project)
    edges_by_id = (
        _grouped_edges_for([n.id for n, _ in hits], confirmed_only=confirmed_only)
        if include_edges else {}
    )
    expand = max(0, min(expand_top, MAX_EXPAND_TOP))
    return {
        "query": query,
        "hits": [
            {"score": round(s, 4),
             **_note_dict(n, preview=(i >= expand)),
             **edges_by_id.get(n.id, {})}
            for i, (n, s) in enumerate(hits)
        ],
    }


@mcp.tool(name="similar")
def infoguana_similar(text: str, limit: int = 10, project: Optional[str] = None,
                  include_edges: bool = False,
                  confirmed_only: bool = True,
                  expand_top: int = 0) -> dict:
    """Find notes semantically similar to a given piece of text.

    Useful when you have a problem description and want to see if anything
    similar has been dealt with before in any project.

    See `search` for the `include_edges` / `confirmed_only` /
    `expand_top` semantics — they behave identically here. Hit `content`
    is the haiku preview by default; `expand_top=N` (max 5) inlines full
    bodies for the closest N hits.

    **Previews are for triage, not citation.** Same rule as `search`:
    fetch the full body via `get` / `get_many` / `expand_top`
    before stating anything anchored on a preview.
    """
    try:
        qv = embed.engine().embed(text)
    except Exception:
        return {"error": "embedding unavailable", "hits": []}
    hits = db.vector_search(qv, limit=limit, project_filter=project)
    edges_by_id = (
        _grouped_edges_for([n.id for n, _ in hits], confirmed_only=confirmed_only)
        if include_edges else {}
    )
    expand = max(0, min(expand_top, MAX_EXPAND_TOP))
    return {
        "hits": [
            {"distance": round(d, 4),
             **_note_dict(n, preview=(i >= expand)),
             **edges_by_id.get(n.id, {})}
            for i, (n, d) in enumerate(hits)
        ],
    }


@mcp.tool(name="add")
def infoguana_add(
    content: str,
    project: Optional[str] = None,
    type: Optional[str] = None,
    tags: Optional[list[str]] = None,
    due_date: Optional[str] = None,
    confidence: str = "unspecified",
    provenance_note: Optional[str] = None,
) -> dict:
    """Save a note into infoguana from the current project context.

    Pass `project` as the name of the project you're working in (usually the
    basename of the current working directory) so the note can be cross-
    referenced later. If you leave `type` unset, infoguana will classify it
    automatically in the background.

    Before saving, prefer calling `tag_suggest(content, project,
    draft_tags=...)` to align with the established tag vocabulary instead
    of minting near-duplicates (`#auth` vs `#authentication`). Re-use a
    suggested tag when it captures the same concept; mint a new one only
    when the suggestions genuinely don't fit.

    Use type='plan' to save a developed feature plan — a plan is intent you
    want to come back to later, with goal + approach + open questions, and is
    automatically marked status='not_started' on creation. Plans graduate into
    `feature` notes once shipped.

    Use type='task' for tracked work that doesn't produce a new deliverable —
    PR reviews, bug fixes, chores, follow-ups. Tasks share the plan lifecycle
    (not_started/pending/complete) and complete through the same flow, but
    never graduate. Pick 'plan' when there's a new feature at the end; pick
    'task' when there's just work to track.

    When the plan or task finishes, call `plan_complete` to attach the
    PR(s) and (optionally) spawn a lessons-learned memory.

    Use type='rule' for standing constraints — hard always-true instructions
    that must be honored every time work touches the relevant scope. Two
    scopes:
    - **Project rule**: pass `project=<name>` for repo-specific constraints
      ("never push to main on this repo", "always run `make lint` before
      committing", "this service must stay under 500ms p99"). Pins to the
      top of `context` for that project only.
    - **Global rule**: pass `project=None` for cross-project norms that
      apply everywhere ("never use `--no-verify` on commits", "always
      paginate GitHub API responses"). Pins to the top of *every* project's
      `context`.
    Rules have no lifecycle (no status). Reserve this type for explicit
    constraints the user has stated — don't infer rules from observed
    patterns.

    Use type='skill' for a reusable procedure — the how-to for a task that
    recurs. Skills pin into `context` as a one-line manifest entry (name +
    description) and their bodies load only when an agent decides one
    applies, so they're the right home for instructions too long to pin and
    too specific to rediscover.

    **A skill body must be a SKILL.md document, frontmatter first**, because
    that frontmatter *is* the manifest entry:

        ---
        name: run-migrations
        description: Apply pending Alembic migrations against a local
          database. Use when the user asks to migrate, after pulling a
          branch that adds a revision, or when the app fails on a schema
          mismatch.
        ---

        # Run migrations
        ...steps...

    `name` is the stable identity the skill is invoked by — kebab-case, no
    spaces. `description` is the *trigger condition*, not a summary: it is
    the only thing a future agent sees before deciding to load the body, so
    enumerate the situations that should fire it. Without frontmatter the
    manifest falls back to the first heading and first paragraph, which
    usually reads as a description of the topic rather than of when to act.

    Two scopes, like rules: `project=None` for a skill that applies
    everywhere, `project=<name>` to scope it to one repo.

    Skill notes skip classification entirely — they carry their own name and
    description, and the classifier has no `skill` label to give them. Like
    'rule', a skill is authored by hand: write one only when the user asks
    for it, and put a how-to you inferred in a 'reference' note instead. A
    skill you write unprompted pins into every future session's manifest,
    above the memories and exempt from the token budget, so it is the one
    note type that cannot be crowded out.

    After the note saves, scan its content for relationships to existing
    notes — explicit `#NNN` references, "this supersedes the old decision",
    "this implements plan X", "the bug in #42 was caused by Y". For each one
    you can justify in a sentence, propose a `link` with the matching
    edge_type and ask the user to confirm before creating it. Skip if there's
    nothing concrete to link.

    `due_date` is plan/task only and accepts an ISO 'YYYY-MM-DD' or a simple
    relative phrase ('today', 'tomorrow', 'in 3 days', 'in 2 weeks'). Stored
    without a time/TZ; "overdue" is computed in the user's local TZ.

    **Provenance** — please fill these so future retrieval can
    weight notes by source trust:

    - `confidence`: how this claim was sourced. One of:
        * `stated`      — the user told you explicitly (chat, comment, etc.)
        * `inferred`    — you derived it from concrete evidence (a diff, PR
                          review, code, log output, doc you read)
        * `speculative` — you extrapolated; the user hasn't confirmed and
                          the evidence is partial
        * `unspecified` — *legacy/escape hatch only*. Don't reach for it
                          when one of the above honestly fits.
    - `provenance_note`: free-text detail capturing the source. Examples:
      "user statement 2026-04-21 chat", "inferred from PR #42 review",
      "web: example.com/foo". Optional but recommended whenever the source
      is identifiable.

    Be honest about confidence — don't mark something `stated` when you
    inferred it. The whole point of the field is to keep your future self
    from treating a guess as a fact.
    """
    # A bad write persists where a bad read is retried. Coercing an
    # unrecognized type to None hands the note to the classifier, and the
    # classifier cannot produce `rule` or `skill` by design — so a typo'd
    # `type='Skill'` silently became a `reference` and no future session
    # learned the capability existed.
    if type is not None and type not in VALID_TYPES:
        return {"error": f"type must be one of {sorted(VALID_TYPES)} "
                         f"(got {type!r}); omit it to let the classifier decide"}
    t: Optional[NoteType] = type  # type: ignore[assignment]
    try:
        parsed_due = duedate.parse_due_input(due_date)
    except ValueError as e:
        return {"error": str(e)}
    if confidence not in VALID_CONFIDENCES:
        return {"error": f"confidence must be one of {sorted(VALID_CONFIDENCES)}"}
    note = db.create_note(NoteCreate(
        content=content,
        type=t,
        project=project,
        tags=tags or [],
        source="mcp",
        due_date=parsed_due,
        confidence=confidence,  # type: ignore[arg-type]
        provenance_note=provenance_note or None,
    ))
    # Synchronous embed + classify here — MCP callers expect a meaningful result
    # and it's fast enough (~1-2s for embed, ~5s if claude classifies).
    try:
        pipeline.process_note(note.id)
    except Exception:
        log.exception("process_note failed for id %d", note.id)
    final = db.get_note(note.id)
    out = _note_dict(final) if final else _note_dict(note)
    if (final or note).type == "skill":
        # Echo back what the manifest entry actually became. `add` otherwise
        # returns neither name nor description, so an author had no way to
        # see the result of a frontmatter mistake — and the most common one,
        # an unquoted colon in the description, makes parse_frontmatter
        # degrade to {} and describe() fall back to the body's first
        # heading and paragraph. The note stores fine and lists fine; it
        # just carries prose where its trigger condition should be, and
        # nothing anywhere raises. Reporting the derived entry (and saying
        # when it came from the fallback path) turns a silent
        # misregistration into something the author can see and fix.
        name, description = skills.describe(final or note)
        out["manifest_entry"] = {"name": name, "description": description}
        if not skills.parse_frontmatter((final or note).content or ""):
            out["manifest_entry"]["warning"] = (
                "No usable YAML frontmatter was parsed, so this name and "
                "description were derived from the body's first heading and "
                "paragraph rather than authored. A colon inside an unquoted "
                "description is the usual cause — quote the value and call "
                "`update` if this entry is not what you intended."
            )
    return out



@mcp.tool(name="tag_suggest")
def infoguana_tag_suggest(
    content: str,
    project: Optional[str] = None,
    draft_tags: Optional[list[str]] = None,
    limit: int = 12,
) -> dict:
    """Rank existing tags by relevance to a draft note before saving.

    Call this *before* `add` to avoid minting near-duplicate tags
    (`#auth` vs `#authentication`, `#db` vs `#database`). Infoguana
    scores its established tag vocabulary by:

    - **semantic**: tags used on notes most similar to your `content`
    - **cooc** (NPMI): tags that historically co-occur with each `draft_tag`
      you've already chosen
    - **in_project**: small bonus for tags already used in `project`

    Each suggestion returns a `score`, the candidate `df` (corpus
    occurrences), and a `from` list naming the signals that fired — so you
    can see *why* a tag surfaced and decide whether it's a fit.

    Workflow: pass the note body and any tags you were going to mint as
    `draft_tags`. Replace your drafts with the top suggestions where they
    capture the same idea; only mint a fresh tag when the suggestions are
    genuinely a poor match for what you mean.

    Suggestions are advisory — the agent picks. Singletons and ephemeral
    identifier patterns (`issue-NNN`, `pr-NNN`) are excluded from the pool.

    Args:
        content: The draft note body (full text).
        project: Optional project the note will be attributed to. Used as
            a soft prior, not a filter.
        draft_tags: Optional list of tags you were planning to use; each
            triggers co-occurrence scoring against the corpus.
        limit: Max suggestions to return (default 12).
    """
    return tag_suggest.suggest_tags(
        content=content,
        project=project,
        draft_tags=draft_tags,
        limit=limit,
    )


@mcp.tool(name="recent")
def infoguana_recent(project: Optional[str] = None, limit: int = 20) -> dict:
    """List the most recently captured notes, optionally scoped to a project."""
    notes = db.recent_notes(project=project, limit=limit)
    return {"notes": [_note_dict(n) for n in notes]}


@mcp.tool(name="get")
def infoguana_get(id: int) -> dict:
    """Fetch a single note by id."""
    note = db.get_note(id)
    if not note:
        return {"error": "not found", "id": id}
    return _note_dict(note)


@mcp.tool(name="get_many")
def infoguana_get_many(ids: list[int]) -> dict:
    """Fetch multiple notes by id in one round trip. Returns full bodies for
    each (same shape as get).

    Use this when you've reviewed a search/context preview list and want to
    pull the bodies of several specific hits at once — cheaper than N
    sequential get calls. Capped at 20 ids per call so a runaway loop
    or misread can't drag in the whole corpus; for larger reads, do another
    search/context first to narrow the set.

    Missing ids return {error, id} entries rather than failing the whole
    call, so a partial-hit list still gets you the rest.

    Args:
        ids: List of note ids to fetch (1-20).
    """
    if not ids:
        return {"notes": [], "missing": [], "error": "ids list is empty"}
    if len(ids) > MAX_GET_MANY:
        return {
            "notes": [], "missing": [],
            "error": f"too many ids ({len(ids)}); cap is {MAX_GET_MANY}",
        }
    notes: list[dict] = []
    missing: list[int] = []
    seen: set[int] = set()
    for nid in ids:
        if nid in seen:
            continue
        seen.add(nid)
        n = db.get_note(nid)
        if n is None:
            missing.append(nid)
        else:
            notes.append(_note_dict(n))
    return {"notes": notes, "missing": missing}


# How many skill notes a name lookup will scan. Names live in SKILL.md
# frontmatter, not in an indexed column, so resolving one means reading
# candidate bodies, so the scan is bounded.
#
# Must be >= graph.SKILLS_FETCH_LIMIT. The manifest is where an agent
# learns a skill's name, so anything the manifest can list has to be
# resolvable by that name; a lookup bound below the manifest bound would
# advertise skills that then come back "not found". Kept as its own
# constant rather than importing graph's — this is a different operation
# with a different cost — and pinned equal by
# tests/test_skills.py::test_skill_lookup_covers_the_manifest.
#
# A corpus past this bound degrades to "not found" for the oldest skills,
# which is why the miss path names the search space it actually covered.
SKILL_LOOKUP_LIMIT = 500


def _skill_payload(note, requested: str) -> dict:
    name, description = skills.describe(note)
    return {
        **_note_dict(note),
        "name": name,
        "description": description,
        "scope": "global" if note.project is None else "project",
        "requested_name": requested,
    }


def _skill_stub(note) -> dict:
    name, description = skills.describe(note)
    return {"id": note.id, "name": name, "description": description,
            "project": note.project,
            "scope": "global" if note.project is None else "project"}


@mcp.tool(name="get_skill")
def infoguana_get_skill(name: str, project: Optional[str] = None) -> dict:
    """Fetch a skill's full SKILL.md body by name, the way it is invoked.

    The manifest in `context` lists each skill as `name (#id)`, and `get(id)`
    works fine when you are holding that listing. This is for when you are
    not: the user typed `/brain-review`, a rule or another note referred to a
    skill by name, or your context was summarized and the ids went with it.
    A name is the skill's stable identity — ids are not portable across
    installs and do not survive a note being re-added.

    Matching is exact after a light fold — case, spaces/underscores vs
    hyphens, and a leading `/` — so `/Brain_Review` and `brain-review` both
    resolve. Nothing fuzzier: a wrong skill followed confidently is worse
    than a miss, and a miss returns `suggestions` with the near names.

    Scope mirrors the manifest. Pass `project` and you search that project's
    skills plus the globals, with a project skill winning over a global of
    the same name — that is how a project overrides a global. Omit `project`
    and the search covers every skill in the corpus, which is the right
    default when you are chasing a name you saw somewhere and don't know
    where it lives.

    When one name matches several skills that the scope can't separate (two
    projects both defining `deploy`), no body is returned — you get
    `ambiguous` with the candidates, so you can re-call with the project or
    with `get(id)`. Returns the same shape as `get` plus `name`,
    `description`, and `scope`.

    Args:
        name: The skill's frontmatter name, e.g. 'brain-review'.
        project: Project scope to search alongside the globals. Omit to
            search every project.
    """
    if not (name or "").strip():
        return {"error": "name is required"}

    if project:
        scoped = db.list_scoped_notes("skill", project, limit=SKILL_LOOKUP_LIMIT)
    else:
        scoped = db.list_notes(type="skill", limit=SKILL_LOOKUP_LIMIT)

    matches = skills.find_by_name(scoped, name)
    # A project skill shadows a global of the same name; that is the whole
    # point of the two scopes. Only a collision the scope can't resolve —
    # two projects, or (impossibly) two notes in one scope — is ambiguous.
    if len(matches) > 1 and project:
        project_local = [n for n in matches if n.project is not None]
        if len(project_local) == 1:
            matches = project_local

    if len(matches) == 1:
        return _skill_payload(matches[0], name)

    if len(matches) > 1:
        return {
            "error": "ambiguous name",
            "requested_name": name,
            "ambiguous": [_skill_stub(n) for n in matches],
            "hint": "re-call with project=<name>, or get(id) for the one you want",
        }

    result: dict = {
        "error": "not found",
        "requested_name": name,
        "searched": f"project={project} plus globals" if project
                    else "all projects",
        "suggestions": skills.suggest_names(scoped, name),
    }
    # A project-scoped lookup that missed is worth one more read: the skill
    # may simply live in another project, and saying so beats letting the
    # agent conclude it doesn't exist. The body stays unfetched — a skill
    # scoped to another repo can reference tooling this one doesn't have,
    # so crossing that boundary is the caller's call to make explicitly.
    if project:
        elsewhere = skills.find_by_name(
            db.list_notes(type="skill", limit=SKILL_LOOKUP_LIMIT), name)
        if elsewhere:
            result["elsewhere"] = [_skill_stub(n) for n in elsewhere]
            result["hint"] = ("exists outside this project's scope; re-call "
                              "with that project, or get(id)")
    return result


VALID_PLAN_STATUSES = {"not_started", "pending", "complete"}


@mcp.tool(name="update")
def infoguana_update(
    id: int,
    content: Optional[str] = None,
    type: Optional[str] = None,
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    status: Optional[str] = None,
    due_date: Optional[str] = None,
    confidence: Optional[str] = None,
    provenance_note: Optional[str] = None,
) -> dict:
    """Update fields on an existing note. Only the fields you pass are changed;
    omitted fields are left alone.

    Use this to refine or correct a previously-saved note rather than creating a
    near-duplicate. If `content` changes, the note's embedding is regenerated so
    search stays consistent.

    For plans, `status` moves the plan between not_started / pending /
    complete — but only as a lightweight transition. To mark a plan complete
    *with PR URLs and lessons learned*, call `plan_complete` instead;
    that path attaches the PRs and spawns a linked lessons-learned memory
    note. Use `status='complete'` here only when the user explicitly asks
    for a silent flip (e.g. closing an obsolete plan they don't want to
    ceremony for).

    `due_date` is plan/task only. Accepts ISO 'YYYY-MM-DD' or relative
    phrases ('today', 'tomorrow', 'in 3 days', 'in 2 weeks'). Pass an empty
    string to clear an existing due date; pass None (omit) to leave it
    unchanged.

    `confidence` and `provenance_note` update the trust
    metadata. Use this to upgrade a legacy 'unspecified' note once you know
    its source, or to correct an honest miss. Pass `provenance_note=""` to
    clear the free-text detail.

    Args:
        id: Note id (from search / recent results).
        content: New content (full replacement).
        type: idea|memory|feedback|feature|reference|plan|task|rule|skill.
        project: Project to attribute the note to.
        tags: New tag list (full replacement — pass all tags you want kept).
        status: For plans only — not_started|pending|complete.
        due_date: For plans/tasks only — ISO date or relative phrase; '' clears.
        confidence: stated|inferred|speculative|unspecified.
        provenance_note: Free-text source detail; '' clears.
    """
    # Validate the argument before the lookup: a bad type is wrong whether or
    # not the id resolves, and checking first keeps this reachable without a
    # database.
    if type is not None and type not in VALID_TYPES:
        return {"error": f"type must be one of {sorted(VALID_TYPES)} "
                         f"(got {type!r})", "id": id}
    existing = db.get_note(id)
    if not existing:
        return {"error": "not found", "id": id}
    t: Optional[NoteType] = type  # type: ignore[assignment]
    if status is not None:
        if status not in VALID_PLAN_STATUSES:
            return {"error": f"status must be one of {sorted(VALID_PLAN_STATUSES)}", "id": id}
        effective_type = t or existing.type
        if existing.type not in ("plan", "task") and effective_type not in ("plan", "task"):
            return {"error": "status only applies to plans and tasks", "id": id}
    parsed_due: Optional[str] = None
    if due_date is not None:
        effective_type = t or existing.type
        if effective_type not in ("plan", "task"):
            return {"error": "due_date only applies to plans and tasks", "id": id}
        if due_date.strip() == "":
            parsed_due = ""  # sentinel: clear the existing date
        else:
            try:
                parsed_due = duedate.parse_due_input(due_date)
            except ValueError as e:
                return {"error": str(e), "id": id}
    if confidence is not None and confidence not in VALID_CONFIDENCES:
        return {"error": f"confidence must be one of {sorted(VALID_CONFIDENCES)}", "id": id}
    updated = db.update_note(id, NoteUpdate(
        content=content,
        type=t,
        project=project,
        tags=tags,
        status=status,  # type: ignore[arg-type]
        due_date=parsed_due,
        confidence=confidence,  # type: ignore[arg-type]
        provenance_note=provenance_note,
    ))
    if updated is None:
        return {"error": "update failed", "id": id}
    # Re-derive when the body changed, or when the type crossed the
    # `skill` boundary in either direction. `skill` is the first type
    # whose preview is a *function of the type*: process_note computes
    # skills.preview_line for skills and a summary for everything else.
    # Before this, a pure type flip left the old preview in place — a
    # retyped skill kept its unrelated summary, and a note retyped *to*
    # skill never got its "name — first sentence" line. Every other
    # retype (plan to task, say) derives its preview the same way from
    # the same body, so re-running for those would spend a classify call
    # to reproduce the identical string.
    crossed_skill = t is not None and (existing.type == "skill") != (t == "skill")
    if content is not None or crossed_skill:
        try:
            pipeline.process_note(id)
        except Exception:
            log.exception("re-embed after update failed for id %d", id)
        updated = db.get_note(id) or updated
    return _note_dict(updated)


@mcp.tool(name="delete")
def infoguana_delete(id: int) -> dict:
    """Permanently delete a note (and its embedding) by id.

    Use sparingly — prefer update to correct a note rather than deleting.
    Appropriate when a note is genuinely obsolete, a duplicate, or being split
    into multiple smaller notes."""
    existing = db.get_note(id)
    if not existing:
        return {"ok": False, "id": id, "error": "not found"}
    ok = db.delete_note(id)
    return {"ok": ok, "id": id}


def _unified_diff(prev: str, curr: str) -> str:
    """Compact unified diff between two version contents. Returns empty
    string when the bodies are identical (e.g. an update only touched
    tags/status). Splits without keepends so a missing trailing newline
    doesn't desync the alignment and produce one giant delete+add block."""
    if prev == curr:
        return ""
    return "\n".join(difflib.unified_diff(
        prev.splitlines(),
        curr.splitlines(),
        fromfile="prev",
        tofile="curr",
        n=3,
        lineterm="",
    ))


@mcp.tool(name="history")
def infoguana_history(id: int) -> dict:
    """Return the full revision history of a note.

    Each entry is a snapshot of the note as it stood at version N, plus a
    `diff_from_prev` showing what changed in the content body relative to
    the previous version (empty string when the update only touched
    metadata like tags or status). Versions are ordered oldest-first; the
    final entry is either the live current state (`change_kind='current'`)
    or — if the note was deleted — a tombstone (`change_kind='delete'`).

    Use this to audit how a note evolved, recover content that an earlier
    update overwrote, or confirm an attribution before citing.

    Args:
        id: Note id (live or deleted).
    """
    versions = db.list_note_versions(id)
    live = db.get_note(id)
    if not versions and not live:
        return {"error": "not found", "id": id}

    entries: list[dict] = list(versions)
    if live is not None:
        entries.append({
            "version": live.version,
            "content": live.content,
            "description": live.description,
            "preview": live.preview,
            "type": live.type,
            "project": live.project,
            "tags": live.tags,
            "status": live.status,
            "linked_prs": live.linked_prs,
            "due_date": live.due_date,
            "edited_at": live.updated_at.isoformat(),
            "change_kind": "current",
        })

    prev_content = ""
    out: list[dict] = []
    for e in entries:
        diff = _unified_diff(prev_content, e["content"]) if out else ""
        out.append({**e, "diff_from_prev": diff})
        prev_content = e["content"]

    return {
        "id": id,
        "deleted": live is None,
        "versions": out,
    }


@mcp.tool(name="context")
def infoguana_context(
    project: str,
    budget_tokens: int = 4000,
    max_hops: int = 4,
    include_types: Optional[list[str]] = None,
    expand_top: int = 0,
) -> dict:
    """Pull a token-budgeted subgraph of memories relevant to a project.

    This is the *preferred* way to bootstrap context at task start: instead of
    dumping a per-project memory file (which burns tokens on irrelevant
    content), this walks the shared infoguana outward from the project node,
    fanning through semantic-similarity edges and IDF-weighted tag edges to
    surface notes that are *actually connected* to the current work.

    Lessons learned in one project will surface here when they share tags or
    semantic neighborhood with the current one, so the agents collectively
    get smarter over time.

    `rule` notes pin to the very top with full bodies — these are standing
    constraints that must be read, not triaged. Two scopes: rules with
    `project=None` are *global* (surface in every project's context), rules
    scoped to this project layer on top. Globals come first, then
    project-specific.

    `skill` notes pin next as a **manifest** under `skills` — one entry
    each carrying `id`, `name`, and `description`, never the body. The
    description is the trigger condition, not the instructions: when a
    task matches one, call `get(id)` to read the full SKILL.md body and
    follow it. Bodies are kept out of the payload on purpose (a skill
    runs 4-8KB), so an entry costs ~100 tokens instead of ~1500.

    Active plans/tasks pin after that, then the BFS neighborhood.

    The pinned rules and the skill manifest are both exempt from
    `budget_tokens` and carry their own caps, so a rule-heavy project
    cannot crowd its own memories out. `rules_tokens_est` and
    `skills_tokens_est` report what each cost; `rules_truncated` and
    `skills_truncated` say whether a bound cut the listing.
    `notes_tokens_est` is what was actually charged against the budget,
    and `total_tokens_est` is the whole payload including the exempt
    sections — budget for the notes slice and read the rest from those
    fields.

    Each note is returned as its haiku-generated preview with
    `preview: True` set on the dict. The 4000-token budget thus surfaces a
    wide neighborhood at low cost. Pass `expand_top=N` (max 5) to inline
    full bodies for the first N notes (active-plans pin first, then by
    reachability) — useful when you want to read the most-relevant
    handful in detail without per-id round trips. Budget sizing accounts
    for the expansion. Call get(id) for ad-hoc deep reads.

    **Previews are for triage, not citation.** A preview is a haiku-sized
    summary — it tells you which notes to read, but can omit nuance that
    changes meaning. Before stating anything anchored on a preview, fetch
    the full body via `get` / `get_many` / `expand_top` and
    read the actual text.

    Args:
        project: Project name (usually the cwd basename).
        budget_tokens: Approximate token budget for returned notes (default 4000).
        max_hops: Cap on BFS depth (default 4).
        include_types: If set, only return notes of these types
            (idea|memory|feedback|feature|reference|plan|task|rule|
            skill|unsorted). An unrecognized type is an error.
        expand_top: Inline full bodies for this many top notes (default 0,
            max 5). Rest stay as previews.
    """
    filt: Optional[list[str]] = None
    if include_types:
        unknown = sorted(set(include_types) - READABLE_TYPES)
        if unknown:
            return {"error": f"unknown types {unknown}; "
                             f"valid: {sorted(READABLE_TYPES)}"}
        filt = list(include_types)
    return graph.build_context(
        project=project,
        budget_tokens=budget_tokens,
        max_hops=max_hops,
        include_types=filt,
        expand_top=max(0, min(expand_top, MAX_EXPAND_TOP)),
    )


# ---------------------------------------------------------------------------
# Plan lifecycle tools.
#
# A 'plan' is short-term, actionable memory: a feature the user has scoped
# out and intends to build later. Plans are created with add(type='plan')
# which auto-sets status='pending'. Pending plans are pinned to the top of
# context output for their project, so the user can walk away and come
# back weeks later to find the plan ready to execute.
#
# When a plan finishes, call plan_complete to flip status to 'complete',
# attach the PR(s) that landed it, and optionally spawn a lessons-learned
# memory so the knowledge persists even after the plan itself retires from
# the default retrieval path.
# ---------------------------------------------------------------------------


def _plan_dict(note) -> dict:
    d = {
        "id": note.id,
        "content": note.content,
        "type": note.type,
        "project": note.project,
        "tags": note.tags,
        "status": note.status,
        "linked_prs": note.linked_prs,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
    }
    if note.due_date:
        disp = duedate.display(note.due_date)
        d["due_date"] = note.due_date
        if disp:
            d["due_state"] = disp["bucket"]
            d["due_in_days"] = disp["days_until"]
    return d


@mcp.tool(name="plans")
def infoguana_plans(
    project: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """List tracked-work notes (plan + task), optionally scoped to a project
    and/or status.

    Defaults to all tracked work across projects. Pass `status='pending'` to
    see what's still outstanding, `status='not_started'` for queued work, or
    `status='complete'` to browse completed entries (and the PRs that landed
    them). Plans and tasks share the same lifecycle and surface together.

    Ordered: overdue/today first (sorted by due date asc), then upcoming due
    dates, then in-progress without a date, then everything else by recency.
    Each plan with a due_date carries `due_state` ('overdue' | 'today' |
    'soon' | 'later') and `due_in_days` so the agent can spot what needs
    attention without re-parsing.

    Args:
        project: Optional project name filter.
        status: Optional status filter: 'not_started' | 'pending' | 'complete'.
        limit: Max entries to return (default 20).
    """
    st = status if status in {"not_started", "pending", "complete"} else None
    today = duedate.today_local().isoformat()
    plans = db.list_plans(project=project, status=st, limit=limit, today=today)
    return {"plans": [_plan_dict(p) for p in plans]}


@mcp.tool(name="plan_complete")
def infoguana_plan_complete(
    id: int,
    pr_urls: Optional[list[str]] = None,
    lessons_learned: Optional[str] = None,
) -> dict:
    """Mark a plan or task complete, attach PR(s) that landed it, and
    optionally spawn a lessons-learned memory note.

    Use this when tracked work (plan or task) has actually finished. The
    note itself is retained (status flips to 'complete') so the original
    intent stays searchable, but it drops out of the default pending-work
    surface in context.

    If you pass `lessons_learned`, a new note of type='memory' is created
    alongside the completed entry, tagged with 'lessons-learned' and
    inheriting the source's project + tags. Use this to capture what was
    surprising, non-obvious, or worth remembering for next time — not just
    what the PR did (the PR description already has that).

    Args:
        id: Plan or task note id.
        pr_urls: Optional list of PR URLs that landed this work (can be
            empty if completion is unrelated to a PR, e.g. cancelled).
        lessons_learned: Optional content for a follow-up memory note
            capturing what you learned while implementing the work.
    """
    try:
        updated, lesson = plans.complete_plan(id, pr_urls=pr_urls,
                                              lessons_learned=lessons_learned)
    except plans.PlanCompletionError as e:
        return {"error": e.message, "id": id}

    result: dict = {"plan": _plan_dict(updated)}
    if lesson is not None:
        result["lessons_note"] = _note_dict(lesson)
    return result


# ---------------------------------------------------------------------------
# GitHub tools (infoguana-chat only — not intended for general project agents).
#
# Reads use the user's personal PAT (INFOGUANA_GITHUB_READ_TOKEN). Whatever that
# PAT can see, the chat can see.
#
# Writes require a `bot_project` argument and use a project-scoped bot PAT
# from INFOGUANA_GITHUB_BOT_TOKENS. Missing token -> clean error back to agent.
# ---------------------------------------------------------------------------


def _gh_error(e: gh.GitHubError) -> dict:
    return {"error": str(e)}


@mcp.tool(name="gh_issue_get")
def infoguana_gh_issue_get(repo: str, number: int) -> dict:
    """Fetch a single GitHub issue (title, body, state, labels, author, dates).

    Args:
        repo: 'owner/name' (e.g. 'octocat/hello-world').
        number: The issue number.
    """
    try:
        return gh.get_issue(repo, number)
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_issue_comments")
def infoguana_gh_issue_comments(repo: str, number: int, limit: int = 50) -> dict:
    """List comments on a GitHub issue, oldest first.

    Use this to pick up the running conversation on an issue — e.g. a daily
    food-log issue the user has been commenting on throughout the day.
    """
    try:
        return {"comments": gh.list_issue_comments(repo, number, limit=limit)}
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_issue_list")
def infoguana_gh_issue_list(repo: str, state: str = "open",
                        labels: Optional[str] = None,
                        limit: int = 20) -> dict:
    """List issues in a repo. `state` is open|closed|all; `labels` is a
    comma-separated label filter ('bug,help-wanted')."""
    try:
        return {"issues": gh.list_issues(repo, state=state, labels=labels,
                                         limit=limit)}
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_pr_get")
def infoguana_gh_pr_get(repo: str, number: int) -> dict:
    """Fetch a single pull request with metadata (head/base refs, merge state,
    additions/deletions, files changed)."""
    try:
        return gh.get_pr(repo, number)
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_pr_comments")
def infoguana_gh_pr_comments(repo: str, number: int, limit: int = 50) -> dict:
    """List PR comments. Returns two lists: `conversation` (issue-style
    comments on the PR) and `review` (inline code-review comments, with path
    and diff hunk)."""
    try:
        return gh.list_pr_comments(repo, number, limit=limit)
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_issue_comment_post")
def infoguana_gh_issue_comment_post(repo: str, number: int, body: str,
                                bot_project: str) -> dict:
    """Post a comment on a GitHub issue AS the project's bot identity.

    The `bot_project` arg chooses which bot PAT to use (from
    INFOGUANA_GITHUB_BOT_TOKENS); pass the project name this chat is scoped to.
    ALWAYS confirm the exact comment body with the user before calling this
    tool — it's a write on shared state that's visible to anyone with access
    to the repo.

    Args:
        repo: 'owner/name'.
        number: Issue number.
        body: Markdown body of the comment.
        bot_project: Project key whose bot PAT should author the comment
            (usually the same value shown in the chat's project seed header).
    """
    try:
        return gh.post_issue_comment(repo, number, body, bot_project)
    except gh.GitHubError as e:
        return _gh_error(e)


@mcp.tool(name="gh_issue_create")
def infoguana_gh_issue_create(repo: str, title: str, body: str,
                          bot_project: str,
                          labels: Optional[list[str]] = None) -> dict:
    """Create a new GitHub issue AS the project's bot identity.

    Same confirmation rule as gh_issue_comment_post: show the user the
    exact title, body, and labels before calling this tool and wait for
    approval. To make sure the user sees the new issue, include `@<handle>`
    in the body so GitHub sends them a notification (the issue itself is
    authored by the bot account, not the user's account).

    Args:
        repo: 'owner/name'.
        title: Issue title.
        body: Markdown body.
        bot_project: Project key whose bot PAT should author the issue.
        labels: Optional list of label names to apply at creation.
    """
    try:
        return gh.create_issue(repo, title, body, bot_project, labels=labels)
    except gh.GitHubError as e:
        return _gh_error(e)


# ---------------------------------------------------------------------------
# Explicit link graph — typed, deliberate edges between notes alongside the
# implicit tag-IDF and semantic-similarity edges built by graph.py.
#
# Edge types (small, deliberate set):
#   implements        — plan → idea/spec
#   caused_by         — bug/incident → root cause
#   supersedes        — new decision → old decision
#   references        — note cites another
#   bundled_with      — explicit "shipped together" relationship
#   prerequisite_for  — explicit dependency relationship
#
# Workflow: agent proposes a link with link, but should confirm with
# the user first (mirrors the add capture pattern). traverse
# walks the typed edges for retrieval and (later) markdown export.
# ---------------------------------------------------------------------------


def _edge_dict(e) -> dict:
    return {
        "from_id": e.from_id,
        "to_id": e.to_id,
        "edge_type": e.edge_type,
        "created_by_agent": e.created_by_agent,
        "confirmed_by_user": e.confirmed_by_user,
        "created_at": e.created_at.isoformat(),
    }


@mcp.tool(name="link")
def infoguana_link(from_id: int, to_id: int, edge_type: str) -> dict:
    """Create a typed edge between two existing notes.

    Use this for *deliberate* connections between notes — e.g. a plan that
    implements an earlier idea, a decision that supersedes an old one, a
    bug-fix note caused_by a root-cause memory. Inferred similarity edges
    (tag overlap, semantic neighborhood) already exist for free; reach for
    link only when the connection is meaningful and you want it
    pinned in retrieval.

    Confirm the link with the user before calling (same pattern as add)
    — this is durable cross-note state, not a free-running heuristic.

    Args:
        from_id: Source note id (the "subject" of the relationship).
        to_id: Target note id.
        edge_type: One of: implements, caused_by, supersedes, references,
            bundled_with, prerequisite_for.
    """
    if edge_type not in VALID_EDGE_TYPES:
        return {"error": f"unknown edge_type '{edge_type}' "
                f"(valid: {sorted(VALID_EDGE_TYPES)})"}
    if from_id == to_id:
        return {"error": "from_id and to_id must differ"}
    if not db.get_note(from_id):
        return {"error": "from_id not found", "id": from_id}
    if not db.get_note(to_id):
        return {"error": "to_id not found", "id": to_id}
    edge = db.create_edge(from_id, to_id, edge_type,  # type: ignore[arg-type]
                          created_by_agent=True, confirmed_by_user=True)
    return {"edge": _edge_dict(edge)}


@mcp.tool(name="unlink")
def infoguana_unlink(from_id: int, to_id: int, edge_type: str) -> dict:
    """Remove a typed edge created by link. No-op if the edge doesn't
    exist. Use to correct a mis-proposed or stale link.
    """
    if edge_type not in VALID_EDGE_TYPES:
        return {"error": f"unknown edge_type '{edge_type}'"}
    removed = db.delete_edge(from_id, to_id, edge_type)  # type: ignore[arg-type]
    return {"ok": removed, "from_id": from_id, "to_id": to_id,
            "edge_type": edge_type}


@mcp.tool(name="infer_edges")
def infoguana_infer_edges(project: Optional[str] = None,
                      limit: int = 200,
                      include_bare: bool = False) -> dict:
    """Backfill pass: scan existing notes for textual cross-references
    ('plan #42', 'supersedes #17', 'depends on #88', 'caused by #5', …)
    and return typed edge proposals for the explicit link graph.

    Read-only — this tool does NOT write edges. Review the proposals with
    the user and create approved ones with `link` (idempotent on
    re-runs). Proposals that duplicate edges already in the graph are
    suppressed, so calling this periodically stays quiet once the backlog
    is drained.

    Verb cues map to edge types:
      - supersedes/replaces/obsoletes #N           → supersedes
      - implements/fulfills #N                     → implements
      - caused by / root cause: #N                 → caused_by
      - bundled with / shipped with #N             → bundled_with
      - requires / depends on / blocked by #N      → prerequisite_for
        (direction flipped: #N is the prerequisite for the citing note)
      - type-hinted 'plan #N' / 'idea #N'          → references

    By default, bare `#N` matches (no verb or type hint) are dropped — they
    are the biggest source of false positives (PR changelog tables inside
    project-overview notes). Pass `include_bare=True` to see them too; each
    proposal carries a `signal` field ('verb' | 'type_hint' | 'bare') so you
    can filter further.

    'PR #N', 'issue #N', 'ticket #N', 'gh #N', 'bug #N', 'commit #N' and
    PR-list spans like 'PRs #115, #119, #123' are treated as GitHub-style
    refs and always skipped.

    Args:
        project: Scope the scan to one project's notes (but edges may still
            point to notes in any project — cross-project refs are kept).
        limit: Cap on proposals returned (default 200).
        include_bare: Include bare `#N` matches with no local cue. Noisy.
    """
    props = inference.infer_edges(project=project, limit=limit,
                                  include_bare=include_bare)
    return {
        "count": len(props),
        "project": project,
        "include_bare": include_bare,
        "proposals": [
            {
                "from_id": p.from_id,
                "to_id": p.to_id,
                "edge_type": p.edge_type,
                "evidence": p.evidence,
                "source_field": p.source_field,
                "signal": p.signal,
            } for p in props
        ],
    }


@mcp.tool(name="traverse")
def infoguana_traverse(start_id: int,
                   depth: int = 2,
                   direction: str = "out",
                   edge_types: Optional[list[str]] = None,
                   include_notes: bool = True) -> dict:
    """Walk the explicit link graph outward from a note, depth-limited.

    Returns the discovered subgraph as a list of nodes (note id + hop count)
    and a list of edges traversed. When `include_notes` is true (default),
    each node is hydrated with the note's content + metadata so the caller
    can render or summarize without a follow-up get.

    Args:
        start_id: Note to start from.
        depth: Max BFS hops (default 2).
        direction: 'out' (from_id -> to_id), 'in' (to_id -> from_id), or 'both'.
        edge_types: Optional filter — only follow these edge kinds.
        include_notes: If True, attach note bodies to each node.
    """
    if direction not in {"out", "in", "both"}:
        return {"error": f"direction must be out|in|both, got '{direction}'"}
    if edge_types:
        bad = [t for t in edge_types if t not in VALID_EDGE_TYPES]
        if bad:
            return {"error": f"unknown edge_types {bad}"}
    if not db.get_note(start_id):
        return {"error": "start_id not found", "id": start_id}

    sub = db.traverse_edges(start_id, depth=depth, direction=direction,
                            edge_types=edge_types)
    nodes = sub["nodes"]
    if include_notes:
        hydrated = []
        for n in nodes:
            note = db.get_note(n["note_id"])
            if note is None:
                continue
            hydrated.append({**n, "note": _note_dict(note)})
        nodes = hydrated
    return {
        "start_id": start_id,
        "depth": depth,
        "direction": direction,
        "edge_types": edge_types,
        "nodes": nodes,
        "edges": sub["edges"],
    }


@mcp.tool(name="export")
def infoguana_export(start_id: int,
                 edge_types: Optional[list[str]] = None,
                 depth: int = 3,
                 direction: str = "both",
                 confirmed_only: bool = True,
                 out_dir: Optional[str] = None,
                 model: Optional[str] = None) -> dict:
    """Synthesize the explicit-edge subgraph rooted at a note (plus any
    linked PRs and their review comments) into a single comprehensive
    engineering write-up. Spawns `claude -p` to do the synthesis — the agent
    receives all hydrated note content and PR context up front and returns
    one cohesive markdown doc, NOT a folder of fragments.

    By default only edges with `confirmed_by_user=1` are followed — agent-
    proposed but unconfirmed edges would just dilute the doc. Set
    `confirmed_only=False` to include them.

    For any plan in the subgraph with `linked_prs`, the PR title/body/state
    and (capped) review threads are fetched via the GitHub PAT and packed
    into the synthesis prompt. PRs the API can't see are skipped.

    Output file defaults to
    `./data/exports/<type>-<id>-<slug>-<UTC-stamp>.md` relative to the
    infoguana process's working directory. Pass `out_dir` to write into a
    different directory (absolute paths recommended).

    Returns: {path, filename, model, node_count, edge_count, pr_count,
    skipped_unconfirmed}.

    Args:
        start_id: Root note to traverse from.
        edge_types: Filter — only follow these edge kinds (default: all).
        depth: Max BFS hops (default 3).
        direction: 'out' | 'in' | 'both' (default 'both' — design history
            runs in both directions).
        confirmed_only: Skip unconfirmed edges (default True).
        out_dir: Override the export directory.
        model: Synthesis model (e.g. 'claude-opus-4-8', 'sonnet'). Defaults
            to export.DEFAULT_EXPORT_MODEL. Suggested values are listed in
            export.SUGGESTED_EXPORT_MODELS.
    """
    if direction not in {"out", "in", "both"}:
        return {"error": f"direction must be out|in|both, got '{direction}'"}
    if edge_types:
        bad = [t for t in edge_types if t not in VALID_EDGE_TYPES]
        if bad:
            return {"error": f"unknown edge_types {bad}"}
    try:
        return export.export_subgraph(
            start_id=start_id,
            edge_types=edge_types,
            depth=depth,
            direction=direction,
            confirmed_only=confirmed_only,
            out_dir=out_dir,
            model=model,
        )
    except ValueError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Read-only filesystem access.
#
# Scoped by an allowlist (INFOGUANA_FS_ALLOWLIST, empty by default, which
# turns these three tools off) with a hardcoded denylist for secrets,
# SSH/GPG keys, `.git/`, and `*.sqlite`.
# Binary files are refused outright — these tools are for source code.
# Every call is recorded in the `fs_reads` audit table.
# ---------------------------------------------------------------------------


@mcp.tool(name="read_file")
def infoguana_read_file(path: str,
                    offset: Optional[int] = None,
                    limit: Optional[int] = None) -> dict:
    """Read a text file from the host filesystem (read-only, allowlisted).

    Returns the file content with each line prefixed `line_no<tab>…` so line
    numbers survive in your context. Useful when grounding answers in the
    user's actual code rather than memory content.

    Access is restricted to paths under the operator-configured allowlist,
    which is empty by default — if no roots are set, every call is refused
    with a message saying so. Secrets, SSH/GPG keys, `.git/` internals, and `*.sqlite`
    files are denylisted and refused. Binary files are refused. Files over
    the size cap (default 500 KB) require paginated reads via offset+limit.

    Args:
        path: Absolute path on the host. `~` is expanded.
        offset: 1-based starting line number. Omit to read from the top.
        limit: Max lines to return. Omit for the whole file (subject to cap).
    """
    try:
        result = fs_access.read_file(path, offset=offset, limit=limit)
    except fs_access.FSAccessError as e:
        return {"error": e.message}
    return fs_access.read_result_to_dict(result)


@mcp.tool(name="list_dir")
def infoguana_list_dir(path: str, max_entries: int = 200) -> dict:
    """List a directory's immediate children (non-recursive).

    Each entry has {name, is_dir, size, mtime, hidden, denied}. Entries that
    match the denylist are still surfaced (so the agent sees they exist) but
    cannot subsequently be read — the `denied` flag marks them.

    Args:
        path: Absolute directory path under the allowlist.
        max_entries: Cap on entries returned, sorted alphabetically.
    """
    try:
        entries = fs_access.list_dir(path, max_entries=max_entries)
    except fs_access.FSAccessError as e:
        return {"error": e.message}
    return {
        "path": path,
        "count": len(entries),
        "entries": fs_access.entries_to_list(entries),
    }


@mcp.tool(name="grep")
def infoguana_grep(pattern: str,
               path: str,
               glob: Optional[str] = None,
               max_matches: int = 200,
               case_insensitive: bool = False) -> dict:
    """Regex search across files under `path`, ripgrep-style.

    Auto-excludes VCS directories (`.git`, `node_modules`, `.venv`, etc.) and
    lockfiles. Respects the allowlist/denylist on every hit. Uses `rg` when
    available, falls back to a pure-Python walker otherwise.

    Args:
        pattern: Regular expression (ripgrep syntax; Python `re` when `rg`
            is unavailable). Anchor with `\\b` for whole-word matches.
        path: Directory or file path under the allowlist to search.
        glob: Optional filename glob (e.g. `*.py`) — filters files before
            reading. Pass `*.py` not `**/*.py`; directory recursion is
            implicit.
        max_matches: Cap on total hits returned (default 200).
        case_insensitive: Case-insensitive matching.
    """
    try:
        hits = fs_access.grep(
            pattern, path, glob=glob,
            max_matches=max_matches,
            case_insensitive=case_insensitive,
        )
    except fs_access.FSAccessError as e:
        return {"error": e.message}
    return {
        "pattern": pattern,
        "path": path,
        "count": len(hits),
        "hits": fs_access.hits_to_list(hits),
    }
