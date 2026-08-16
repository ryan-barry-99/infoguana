import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app import db, duedate, embed, graph
from app.templating import templates


log = logging.getLogger(__name__)

# Lets the /browse search box double as a jump-to-id input: when the
# user types `42` or `#42`, we surface that exact note as the top hit
# alongside the normal hybrid-search results.
_ID_QUERY = re.compile(r"^#?(\d+)$")


def _embed_query(q: str) -> Optional[list[float]]:
    """Best-effort embed for hybrid search; falls back to FTS-only on
    error so a flaky model load can't break the browse search box."""
    try:
        return embed.engine().embed(q)
    except Exception:
        log.exception("query embed failed, falling back to FTS-only")
        return None


router = APIRouter(tags=["views"])


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # Graph is the cover page — everything else lives under its own route.
    return templates.TemplateResponse(request, "graph.html", {})


@router.get("/graph", response_class=HTMLResponse)
def graph_alias(request: Request) -> HTMLResponse:
    # Kept for backwards compatibility with any existing links/bookmarks.
    return templates.TemplateResponse(request, "graph.html", {})


@router.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request) -> HTMLResponse:
    notes = db.recent_notes(limit=30)
    db.attach_links(notes)
    return templates.TemplateResponse(
        request, "index.html", {"notes": notes}
    )


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request) -> HTMLResponse:
    """Portfolio dashboard: every project infoguana knows about with counts
    of pending / complete plans, total notes, and last activity. Each cell
    links into /browse with matching filters. Hidden projects (toggled via
    the per-row × button) are excluded from the table and surfaced through
    a dropdown so they can be unhidden in-page."""
    return templates.TemplateResponse(request, "projects.html", {
        "rows": db.project_stats(),
        "hidden": db.list_hidden_projects(),
    })


@router.get("/projects/{name}/ranked-notes")
def project_ranked_notes(name: str) -> list[int]:
    """Note ids for `name` ordered the same way build_context ranks them —
    pending plans first, then BFS reachability from the project node.
    Used as a sort key by the graph view's project side panel; returns
    note ids only (the panel renders previews from data it already has)."""
    return graph.rank_project_notes(name)


@router.post("/projects/{name}/visibility")
async def set_project_visibility(name: str, request: Request) -> dict:
    """Toggle a project's hidden flag. Body: hidden=1|0."""
    form = await request.form()
    raw = str(form.get("hidden") or "").strip()
    if raw not in ("0", "1"):
        raise HTTPException(400, "hidden must be 0 or 1")
    db.set_project_hidden(name, raw == "1")
    return {"name": name, "hidden": raw == "1"}


@router.get("/agenda", response_class=HTMLResponse)
def agenda_page(request: Request,
                project: list[str] = Query(default_factory=list),
                show_complete: bool = False,
                flat: bool = False) -> HTMLResponse:
    """Date-ordered list of plans + tasks. Sectioned by bucket: overdue,
    today, this week (1-7d), later (>7d), and in-progress with no date.
    `project` accepts repeated values (`?project=a&project=b`) to view
    multiple projects together — useful for grouping related projects
    (e.g. a firmware repo and its companion API repo). `show_complete=true`
    appends recently-completed work at the bottom.

    `flat=true` flattens each bucket into a single list sorted purely by
    due_date (then recency) — useful when the question is "what's next?"
    rather than "what's next per project?"."""
    today_iso = duedate.today_local().isoformat()
    projects = [p for p in (project or []) if p]
    rows = db.list_plans(project=projects or None, today=today_iso, limit=500)
    # Mirror the /projects tab's visibility model: hidden projects don't
    # surface in the agenda either. If the user explicitly picked a hidden
    # project via the URL, honor that — they asked. Otherwise drop items
    # whose project is on the hidden list.
    hidden = set(db.list_hidden_projects())
    if hidden and not projects:
        rows = [n for n in rows if (n.project or "") not in hidden]

    # Each section is an ordered list of (project_name, [notes]) groups so
    # personal vs work clusters don't intertwine within a bucket. Project
    # order: alphabetical (None — uncattributed — last). Note order within
    # a project: list_plans already returned overdue/upcoming first, then
    # by due_date asc, then recency — preserve that by walking `rows` in
    # order and appending into the matching group.
    bucket_groups: dict[str, dict[str, list]] = {
        "overdue": {}, "today": {}, "soon": {}, "later": {},
        "no_date": {}, "complete": {},
    }
    def _push(bucket: str, n) -> None:
        key = n.project or ""
        bucket_groups[bucket].setdefault(key, []).append(n)

    for n in rows:
        if n.status == "complete":
            if show_complete:
                _push("complete", n)
            continue
        if not n.due_date:
            # All non-complete tracked work without a date lives in
            # `no_date` so a freshly-created plan/task surfaces somewhere
            # obvious (otherwise capturing a plan and not setting a date
            # would make it invisible to the agenda).
            _push("no_date", n)
            continue
        bucket = duedate.state_bucket(n.due_date)
        if bucket and bucket in bucket_groups:
            _push(bucket, n)

    # Materialize as ordered lists for templating: empty project key sorts
    # last (Unicode-wise; force it explicitly so '' lands after any name).
    def _sort_key(item):
        proj = item[0]
        return (1, "") if proj == "" else (0, proj.lower())

    # `no_date` always stays grouped by project — there's no date to sort
    # on, so flattening would just produce a meaningless mixed pile. Dated
    # buckets honor the `flat` toggle.
    sections = {}
    for bucket, groups in bucket_groups.items():
        if flat and bucket != "no_date":
            ordered: list = []
            # Walk `rows` again to preserve list_plans's ordering across
            # projects within a bucket; the dict-of-lists in bucket_groups
            # has lost the cross-project interleaving.
            for n in rows:
                key = n.project or ""
                if key in groups and n in groups[key]:
                    ordered.append(n)
            sections[bucket] = [(None, ordered)] if ordered else []
        else:
            sections[bucket] = sorted(groups.items(), key=_sort_key)

    db.attach_links(rows)
    return templates.TemplateResponse(request, "agenda.html", {
        "sections": sections,
        "project": projects,
        "show_complete": show_complete,
        "flat": flat,
        # Only projects that actually have plans/tasks AND aren't hidden
        # via the /projects tab — keeps the chip picker scoped to the
        # agenda's domain. When show_complete is on, widen the picker so
        # you can also filter to projects whose only tracked work has
        # been completed.
        "all_projects": [
            p for p in db.list_plan_project_names(include_complete=show_complete)
            if p not in hidden
        ],
        "today_iso": today_iso,
        "total": sum(len(notes) for groups in bucket_groups.values()
                     for notes in groups.values()),
        # Render cards in preview mode so a long agenda stays scannable.
        "collapsed": True,
    })


@router.get("/browse", response_class=HTMLResponse)
def browse_page(request: Request,
                project: Optional[str] = None,
                type: list[str] = Query(default_factory=list),
                status: Optional[str] = None,
                tag: Optional[str] = None,
                id: Optional[int] = None,
                q: Optional[str] = None,
                limit: int = 100) -> HTMLResponse:
    """Filterable list of notes — the missing third browse mode alongside
    /capture (recents) and the graph view. Supports any combination of
    project / type / status filters; bookmarkable via URL params.

    `id=N` short-circuits the filters and renders just that one note.
    Used as the click-through target for typed-edge link chips, so a chip
    can land on a permalink-style page even when the linked note isn't
    among the current filter results.

    `q=...` runs hybrid search scoped by the active filters — a single
    "find a note within this slice" affordance instead of a separate
    page. Status is post-filtered (hybrid_search doesn't take it). When
    `q` is purely numeric (`42` or `#42`) the matching note is pinned
    to the top of the result list so the search box doubles as a
    jump-to-id input."""
    if id is not None:
        single = db.get_note(id)
        notes = [single] if single else []
        db.attach_links(notes)
        # Fall back to the tombstone if the note was deleted, so #N jumps
        # still resolve to *something* (rendered with the deleted treatment).
        tombstones = [] if single else [t for t in [db.get_tombstone(id)] if t]
        return templates.TemplateResponse(request, "browse.html", {
            "notes": notes,
            "tombstones": tombstones,
            "project": None, "type": [], "status": None, "tag": None, "q": None,
            "single_id": id,
            "limit": limit,
            "all_projects": db.list_project_names(),
            "all_types": ["idea", "memory", "feedback", "feature",
                          "reference", "plan", "task", "rule", "skill",
                          "unsorted"],
        })

    # Normalize empty-string params from the form to None so SQL filters
    # don't accidentally match empty values. `type` is multi-valued — drop
    # any empty strings (e.g. an "any type" placeholder if one slipped in).
    project = project or None
    types = [t for t in (type or []) if t]
    status = status or None
    tag = tag or None
    q = (q or "").strip() or None

    pinned_tombstone = None  # set by id-jump if it resolves to a deleted note
    if q:
        qv = _embed_query(q)
        raw = db.hybrid_search(q, query_vec=qv, limit=limit,
                               type_filter=types or None,
                               project_filter=project)
        notes = [n for n, _ in raw]
        id_match = _ID_QUERY.match(q)
        if id_match:
            pin_id = int(id_match.group(1))
            pinned = db.get_note(pin_id)
            if pinned is not None:
                notes = [pinned] + [n for n in notes if n.id != pin_id]
            else:
                # Live note's gone — try the tombstone so #N still resolves to
                # *something* at the top of the result list.
                pinned_tombstone = db.get_tombstone(pin_id)
                if pinned_tombstone is not None:
                    notes = [pinned_tombstone] + notes
        if status:
            notes = [n for n in notes if n.status == status]
        if tag:
            notes = [n for n in notes if tag in (n.tags or [])]
    else:
        notes = db.list_notes(project=project, type=types or None,
                              status=status, tag=tag, limit=limit)
    db.attach_links(notes)

    # Surface deleted notes when the user is searching by query or jumping
    # by id — they're hidden from filter-only browse so the default list
    # stays "live notes only". Project/type filtering also applies.
    tombstones: list = []
    if q:
        ts = db.search_tombstones(q, limit=limit)
        if project:
            ts = [t for t in ts if t.project == project]
        if types:
            ts = [t for t in ts if t.type in types]
        if tag:
            ts = [t for t in ts if tag in (t.tags or [])]
        # The id-jump path already pinned a matching tombstone to the top of
        # `notes` so it lands as the first result; drop it here to avoid
        # rendering it twice.
        if pinned_tombstone is not None:
            ts = [t for t in ts if t.id != pinned_tombstone.id]
        tombstones = ts
    return templates.TemplateResponse(request, "browse.html", {
        "notes": notes,
        "tombstones": tombstones,
        "project": project,
        "type": types,
        "status": status,
        "tag": tag,
        "q": q,
        "single_id": None,
        "limit": limit,
        # Collapse cards to previews across the whole list view (search,
        # filtered, or unfiltered). Single-note view (?id=N) keeps full
        # content since it's a permalink. Capture / graph render with full
        # content too.
        "collapsed": True,
        "all_projects": db.list_project_names(),
        "all_types": ["idea", "memory", "feedback", "feature",
                      "reference", "plan", "task", "rule", "skill",
                      "unsorted"],
    })


@router.get("/graph.json")
def graph_json(project: Optional[str] = None, k: int = 3,
               threshold: float = 1.2) -> dict:
    return graph.build_graph(
        per_node_k=k,
        distance_threshold=threshold,
        project_filter=project,
    )


@router.get("/healthz")
def health() -> dict:
    return {"ok": True}
