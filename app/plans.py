"""Plan lifecycle helpers shared between the MCP tool and the web route."""
import logging
from typing import Optional

from app import db, pipeline
from app.models import Note, NoteCreate, NoteUpdate


log = logging.getLogger(__name__)


class PlanCompletionError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def complete_plan(
    id: int,
    pr_urls: Optional[list[str]] = None,
    lessons_learned: Optional[str] = None,
) -> tuple[Note, Optional[Note]]:
    """Mark a tracked-work note (plan or task) complete, merge in any new PR
    URLs, and optionally create a linked lessons-learned memory note. Raises
    PlanCompletionError on invalid input (note missing, wrong type, update
    failed).

    Returns (updated_note, lessons_note_or_None).
    """
    existing = db.get_note(id)
    if not existing:
        raise PlanCompletionError(404, f"note {id} not found")
    if existing.type not in ("plan", "task"):
        raise PlanCompletionError(
            400, f"note {id} is type={existing.type!r}, not 'plan' or 'task'"
        )

    merged_prs = list(dict.fromkeys([*existing.linked_prs, *(pr_urls or [])]))
    updated = db.update_note(id, NoteUpdate(
        status="complete",
        linked_prs=merged_prs,
    ))
    if updated is None:
        raise PlanCompletionError(500, f"update failed for note {id}")

    # Auto-advance: when a bundled task completes, flip the next
    # not_started sibling to pending so the agent's context only ever shows
    # the immediate next step's full body.
    if existing.type == "task":
        parent = db.bundled_parent_of(id)
        if parent is not None:
            siblings = db.bundled_tasks_of(parent.id)
            for sib in siblings:
                if sib.id == id:
                    continue
                if sib.status == "not_started":
                    db.update_note(sib.id, NoteUpdate(status="pending"))
                    break

    lesson: Optional[Note] = None
    if lessons_learned and lessons_learned.strip():
        tags = list(dict.fromkeys([*existing.tags, "lessons-learned"]))
        lesson = db.create_note(NoteCreate(
            content=lessons_learned.strip(),
            type="memory",
            project=existing.project,
            tags=tags,
            source="mcp",
            confidence="inferred",
            provenance_note=f"lessons-learned synthesis from plan #{id}",
        ))
        # Link the lessons note back to the source plan — the spawn implies
        # this relationship and the graph should reflect it (otherwise the
        # lessons note ends up floating, with no traversal path back to the
        # work it came out of). User-confirmed because passing lessons_learned
        # is itself the explicit ask.
        try:
            db.create_edge(
                from_id=lesson.id,
                to_id=id,
                edge_type="references",
                created_by_agent=False,
                confirmed_by_user=True,
            )
        except Exception:
            log.exception(
                "auto-link failed: lessons %d -references-> plan %d",
                lesson.id, id,
            )
        try:
            pipeline.process_note(lesson.id)
        except Exception:
            log.exception("process_note failed for lessons-learned id %d", lesson.id)
        lesson = db.get_note(lesson.id) or lesson

    return updated, lesson
