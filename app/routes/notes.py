import asyncio
import difflib
import hashlib
import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.datastructures import UploadFile

from app import db, duedate, export, pipeline, plans
from app.config import settings
from app.models import Note, NoteCreate, NoteUpdate
from app.templating import templates


log = logging.getLogger(__name__)
router = APIRouter(prefix="/notes", tags=["notes"])


IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _save_upload(note_id: int, upload: UploadFile) -> tuple[str, str, int]:
    data = upload.file.read()
    size = len(data)
    if size == 0:
        raise HTTPException(400, "empty upload")
    if size > settings.attachment_max_bytes:
        raise HTTPException(413, f"upload exceeds {settings.attachment_max_bytes} bytes")

    mime = (upload.content_type or "").lower()
    if not mime:
        mime = mimetypes.guess_type(upload.filename or "")[0] or "application/octet-stream"
    ext = IMAGE_EXT.get(mime) or Path(upload.filename or "").suffix or ".bin"

    digest = hashlib.sha256(data).hexdigest()[:16]
    rel = f"{note_id}/{digest}{ext}"
    dest = settings.attachments_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return rel, mime, size


@router.post("", response_model=Note)
def create(data: NoteCreate, background: BackgroundTasks) -> Note:
    note = db.create_note(data)
    background.add_task(pipeline.process_note, note.id)
    return note


@router.get("/{note_id}", response_model=Note)
def get(note_id: int) -> Note:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    return note


@router.patch("/{note_id}", response_model=Note)
def update(note_id: int, data: NoteUpdate) -> Note:
    note = db.update_note(note_id, data)
    if not note:
        raise HTTPException(404, "not found")
    return note


@router.delete("/{note_id}")
def delete(note_id: int) -> dict:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")

    # Remember attachment paths so we can scrub files on disk after DB deletion.
    files = [settings.attachments_dir / a.path for a in note.attachments]
    note_dir = settings.attachments_dir / str(note_id)

    if not db.delete_note(note_id):
        raise HTTPException(404, "not found")

    for f in files:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            log.exception("failed to unlink %s", f)
    try:
        if note_dir.is_dir() and not any(note_dir.iterdir()):
            note_dir.rmdir()
    except Exception:
        pass

    return {"deleted": note_id}


@router.post("/form", response_class=HTMLResponse)
async def create_from_form(
    request: Request,
    background: BackgroundTasks,
) -> HTMLResponse:
    """Multipart form endpoint — accepts text content, optional project, and
    optionally one or more image files. Returns the new note card HTML."""
    form = await request.form()
    content = str(form.get("content") or "").strip()
    project = str(form.get("project") or "").strip()
    uploads: list[UploadFile] = [u for u in form.getlist("image") if isinstance(u, UploadFile) and u.filename]

    if not content and not uploads:
        raise HTTPException(400, "need text or an image")

    note = db.create_note(NoteCreate(
        content=content,
        project=project or None,
        source="web",
    ))

    for upload in uploads:
        rel, mime, size = _save_upload(note.id, upload)
        db.add_attachment(note.id, rel, mime, size)

    # Refetch so the card includes its attachments.
    note = db.get_note(note.id) or note
    db.attach_links([note])
    background.add_task(pipeline.process_note, note.id)
    return templates.TemplateResponse(request, "_note_card.html", {"note": note})


def _diff_lines(prev: str, curr: str) -> list[dict]:
    """Classify unified-diff lines for the history template. Returns
    [{kind, text}, ...] where kind is 'add' | 'del' | 'hunk' | 'ctx'.
    Empty list when the bodies are identical.

    Splits without keepends so a missing trailing newline doesn't make
    'foo' and 'foo\\n' look like distinct lines to the differ — that
    bug groups every adjacent edit into one big delete+add block instead
    of showing minimal per-line changes."""
    if prev == curr:
        return []
    raw = difflib.unified_diff(
        prev.splitlines(),
        curr.splitlines(),
        fromfile="prev", tofile="curr", n=3, lineterm="",
    )
    out: list[dict] = []
    for line in raw:
        if line.startswith("+++") or line.startswith("---"):
            continue  # file-name headers — noise for in-app history
        if line.startswith("@@"):
            out.append({"kind": "hunk", "text": line})
        elif line.startswith("+"):
            out.append({"kind": "add", "text": line})
        elif line.startswith("-"):
            out.append({"kind": "del", "text": line})
        else:
            out.append({"kind": "ctx", "text": line})
    return out


def _format_edited_at(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %H:%M")
    except (ValueError, TypeError):
        return iso


@router.get("/{note_id}/history/panel", response_class=HTMLResponse)
def note_history_panel(request: Request, note_id: int) -> HTMLResponse:
    """Server-rendered history view for the note-card modal. Lists every
    archived version + the live current state, with colored unified diffs
    between adjacent content versions."""
    versions = db.list_note_versions(note_id)
    live = db.get_note(note_id)
    if not versions and not live:
        raise HTTPException(404, "not found")

    entries: list[dict] = list(versions)
    if live is not None:
        entries.append({
            "version": live.version,
            "content": live.content,
            "type": live.type,
            "project": live.project,
            "tags": live.tags,
            "status": live.status,
            "edited_at": live.updated_at.isoformat(),
            "change_kind": "current",
        })

    prev_content = ""
    prev_version: Optional[int] = None
    prev_tags: list[str] = []
    prev_type: Optional[str] = None
    prev_project: Optional[str] = None
    prev_status: Optional[str] = None
    rendered: list[dict] = []
    for i, e in enumerate(entries):
        diff = _diff_lines(prev_content, e["content"]) if i > 0 else []
        # Per-tag diff status: 'added' shows in green, 'removed' in rose
        # strikethrough, otherwise unchanged. Union-ordered (current tags
        # first, then any removed-from-prior) so a reader can scan the
        # before+after at a glance.
        cur_tags = list(e.get("tags") or [])
        if i == 0:
            tag_chips = [{"tag": t, "state": "unchanged"} for t in cur_tags]
        else:
            cur_set = set(cur_tags)
            prev_set = set(prev_tags)
            tag_chips = []
            for t in cur_tags:
                tag_chips.append({"tag": t, "state": "added" if t not in prev_set else "unchanged"})
            for t in prev_tags:
                if t not in cur_set:
                    tag_chips.append({"tag": t, "state": "removed"})
        rendered.append({
            **e,
            "diff_lines": diff,
            "prev_version": prev_version,
            "edited_at_display": _format_edited_at(e["edited_at"]),
            "tag_chips": tag_chips,
            "type_changed": i > 0 and e.get("type") != prev_type,
            "project_changed": i > 0 and e.get("project") != prev_project,
            "status_changed": i > 0 and e.get("status") != prev_status,
            "prev_type": prev_type,
            "prev_project": prev_project,
            "prev_status": prev_status,
        })
        prev_content = e["content"]
        prev_version = e["version"]
        prev_tags = cur_tags
        prev_type = e.get("type")
        prev_project = e.get("project")
        prev_status = e.get("status")

    return templates.TemplateResponse(request, "_note_history.html", {
        "note_id": note_id,
        "deleted": live is None,
        "versions": rendered,
    })


@router.get("/{note_id}/history")
def note_history(note_id: int) -> dict:
    """Return revision snapshots for a note (oldest first), plus the live
    current state when the note still exists. See the MCP `history` tool
    for the entry shape."""
    versions = db.list_note_versions(note_id)
    live = db.get_note(note_id)
    if not versions and not live:
        raise HTTPException(404, "not found")
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
    return {"id": note_id, "deleted": live is None, "versions": entries}


@router.get("/{note_id}/links")
def note_links(note_id: int) -> list[dict]:
    """JSON list of the note's confirmed typed-edge neighbors. Used by
    the graph view to halo linked nodes with edge-type-specific colors
    when the note is selected. Returns an empty list rather than 404 for
    a missing note id — callers are decorating UI, not asserting state."""
    if not db.get_note(note_id):
        return []
    links = db.batch_links_for([note_id]).get(note_id, [])
    return [
        {"target_id": l.target_id, "edge_type": l.edge_type,
         "direction": l.direction}
        for l in links
    ]


@router.get("/{note_id}/card", response_class=HTMLResponse)
def note_card(request: Request, note_id: int,
              context: Optional[str] = None,
              collapsed: bool = False) -> HTMLResponse:
    """Used by HTMX polling on unsorted notes to self-replace once classified,
    and by the graph tab to populate its side panel.

    `context='graph'` suppresses the view-in-graph action button (we're
    already in the graph). HTMX polling omits the param so the button
    stays visible after a card refreshes in browse/capture.

    `collapsed=1` matches the /browse list view's preview-mode rendering so
    a card that re-renders mid-list (e.g. unsorted polling completing) keeps
    its collapsed shape."""
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    db.attach_links([note])
    return templates.TemplateResponse(
        request, "_note_card.html",
        {"note": note, "context": context, "collapsed": collapsed},
    )


@router.post("/{note_id}/complete", response_class=HTMLResponse)
async def complete_plan_form(
    request: Request,
    note_id: int,
    background: BackgroundTasks,
) -> HTMLResponse:
    """Mark a plan complete from the web UI. Accepts a multipart form with:
    - pr_urls: one URL per line (optional)
    - lessons_learned: markdown body (optional)
    Returns the refreshed note card."""
    form = await request.form()
    raw_prs = str(form.get("pr_urls") or "")
    pr_urls = [line.strip() for line in raw_prs.splitlines() if line.strip()]
    lessons = str(form.get("lessons_learned") or "").strip() or None

    try:
        plans.complete_plan(note_id, pr_urls=pr_urls, lessons_learned=lessons)
    except plans.PlanCompletionError as e:
        raise HTTPException(e.code, e.message)

    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    db.attach_links([note])
    return templates.TemplateResponse(request, "_note_card.html", {"note": note})


_MANUAL_TYPES = {"idea", "memory", "feedback", "feature", "reference", "plan",
                 "task", "rule", "skill"}


@router.post("/{note_id}/edit", response_class=HTMLResponse)
async def edit_from_form(
    request: Request,
    note_id: int,
    background: BackgroundTasks,
    collapsed: bool = False,
) -> HTMLResponse:
    """Apply user-edited content/project/type to a note.

    If `type` is empty (form's "auto" option), reset type to 'unsorted' and
    queue a Haiku reclassify + re-embed — the original behavior. The card
    re-renders in polling state so the UI updates when the new classification
    lands.

    If `type` is a manual choice, write it directly without dropping into
    'unsorted'. Tags / description are kept as-is. Content changes still
    trigger a re-embed so search stays consistent. This is the migration
    path for the tracked-work types (e.g. flipping a stale plan → task without
    nuking its metadata)."""
    existing = db.get_note(note_id)
    if not existing:
        raise HTTPException(404, "not found")

    form = await request.form()
    new_content = str(form.get("content") or "").strip()
    new_project = str(form.get("project") or "").strip() or None
    raw_type = str(form.get("type") or "").strip()

    if not new_content and not existing.attachments:
        raise HTTPException(400, "need text or an image")

    if raw_type and raw_type not in _MANUAL_TYPES:
        raise HTTPException(400, f"invalid type {raw_type!r}")

    content_changed = new_content != (existing.content or "")

    if raw_type:
        # Manual type — preserve tags/description, write the type directly.
        # If the note is being promoted into the tracked-work lifecycle
        # (plan/task) and currently has no status, mirror create_note's
        # behavior and seed status='not_started'. Existing plan↔task swaps
        # keep their lifecycle position untouched.
        seed_status = (
            "not_started"
            if raw_type in ("plan", "task") and existing.status is None
            else None
        )
        db.update_note(note_id, NoteUpdate(
            content=new_content,
            project=new_project,
            type=raw_type,
            status=seed_status,
        ))
        # Re-embed only when the body actually changed; otherwise the
        # existing embedding still represents this content.
        if content_changed:
            background.add_task(pipeline.process_note, note_id, True)
    else:
        # Auto — reset type so the polling UI shows 'classifying' while
        # Haiku re-runs.
        db.update_note(note_id, NoteUpdate(
            content=new_content,
            project=new_project,
            type="unsorted",
            description=None,
            tags=[],
        ))
        background.add_task(pipeline.process_note, note_id, True)

    note = db.get_note(note_id) or existing
    db.attach_links([note])
    return templates.TemplateResponse(
        request, "_note_card.html", {"note": note, "collapsed": collapsed},
    )


VALID_PLAN_STATUSES = {"not_started", "pending", "complete"}


@router.post("/{note_id}/status", response_class=HTMLResponse)
async def set_plan_status(request: Request, note_id: int,
                          collapsed: bool = False) -> HTMLResponse:
    """Manually transition a plan's status. Only valid for plans. The
    heavyweight path (→ complete with PR URLs + lessons) lives on
    /plans/{id}/complete-chat — this endpoint is for the lightweight
    transitions (not_started <-> pending, or un-shipping complete -> pending)
    and for cases where the user wants to mark something complete without
    talking to an agent.

    `collapsed=1` re-renders the card in preview mode (matches the /browse
    list view) so flipping a status from a collapsed list doesn't drop the
    card into full-content mode."""
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    if note.type not in ("plan", "task"):
        raise HTTPException(400, "status only applies to plans and tasks")

    form = await request.form()
    new_status = str(form.get("status") or "").strip()
    if new_status not in VALID_PLAN_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_PLAN_STATUSES)}")

    db.update_note(note_id, NoteUpdate(status=new_status))
    updated = db.get_note(note_id) or note
    db.attach_links([updated])
    return templates.TemplateResponse(
        request, "_note_card.html", {"note": updated, "collapsed": collapsed},
    )


@router.post("/{note_id}/due-date", response_class=HTMLResponse)
async def set_due_date(request: Request, note_id: int,
                       collapsed: bool = False) -> HTMLResponse:
    """Set, update, or clear a plan/task due date. Form field `due_date`
    accepts ISO 'YYYY-MM-DD' or relative phrases ('today', 'tomorrow',
    'in 3 days'). An empty value clears the date. Re-renders the card."""
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    if note.type not in ("plan", "task"):
        raise HTTPException(400, "due_date only applies to plans and tasks")

    form = await request.form()
    raw = str(form.get("due_date") or "").strip()
    if raw == "":
        update_value = ""  # sentinel: clear
    else:
        try:
            update_value = duedate.parse_due_input(raw) or ""
        except ValueError as e:
            raise HTTPException(400, str(e))

    db.update_note(note_id, NoteUpdate(due_date=update_value))
    updated = db.get_note(note_id) or note
    db.attach_links([updated])
    return templates.TemplateResponse(
        request, "_note_card.html", {"note": updated, "collapsed": collapsed},
    )


@router.post("/{note_id}/export")
async def export_subgraph(
    note_id: int, model: Optional[str] = Form(None),
) -> Response:
    """Synthesize the note's edge subgraph + linked PRs into a single
    comprehensive markdown doc via `claude -p`, persist it under
    ./data/exports/, and stream it back as a download. `model` selects the
    synthesis model (defaults to export.DEFAULT_EXPORT_MODEL)."""
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "not found")
    try:
        result = await asyncio.to_thread(
            export.export_subgraph, note_id, model=model,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    path = Path(result["path"])
    return Response(
        content=path.read_bytes(),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        },
    )


@router.get("/{note_id}/attachments/{rest:path}")
def attachment(note_id: int, rest: str) -> FileResponse:
    """Serve an attachment by its stored relative path, scoped to a note id."""
    rel = f"{note_id}/{rest}"
    safe = (settings.attachments_dir / rel).resolve()
    base = settings.attachments_dir.resolve()
    if base not in safe.parents and safe != base:
        raise HTTPException(404, "not found")
    if not safe.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(safe)
