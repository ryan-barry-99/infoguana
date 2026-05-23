"""Background processing for new notes: embed + classify."""
import logging

from app import classify, db, embed
from app.config import settings
from app.models import NoteUpdate


log = logging.getLogger(__name__)


IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "image/heif"}


def _embedding_text(note) -> str:
    parts = [note.content or ""]
    if note.description:
        parts.append(note.description)
    if note.tags:
        parts.append(" ".join(note.tags))
    return " ".join(p for p in parts if p).strip() or "(empty)"


def process_note(note_id: int, force_reclassify: bool = False) -> None:
    """Embed + classify a note. Called from a background task (or synchronously
    from the MCP add tool).

    Classification runs when the note is unsorted, when force_reclassify is
    set, or when the note has no preview yet — so plans created with an
    explicit type still get one Haiku-quality preview at creation time
    (plan #322). Type/tags/project/description from classify are only
    persisted when the note's existing fields are in scope for re-derivation
    (unsorted-on-create or explicit force_reclassify); the preview piggybacks
    on the same call regardless. Preview always ends populated — falls back
    to first-line truncation when classify is skipped or unavailable."""
    note = db.get_note(note_id)
    if not note:
        log.warning("process_note: note %d not found", note_id)
        return

    image_paths = []
    for att in note.attachments:
        if (att.mime_type or "").lower() in IMAGE_MIMES:
            image_paths.append(settings.attachments_dir / att.path)

    cls = None
    if note.type == "unsorted" or force_reclassify or note.preview is None:
        cls = classify.classify(note.content, image_paths=image_paths or None)

    update_kwargs: dict = {}
    if cls is not None:
        if note.type == "unsorted" or force_reclassify:
            # On force reclassify we replace tags (keep intent fresh); on
            # initial classify we merge with any user-supplied tags.
            merged_tags = cls.tags if force_reclassify else list(dict.fromkeys([*note.tags, *cls.tags]))
            update_kwargs.update(
                type=cls.type,
                tags=merged_tags,
                project=note.project or cls.project,
                description=cls.description,
            )
        if cls.preview:
            update_kwargs["preview"] = cls.preview

    if "preview" not in update_kwargs:
        update_kwargs["preview"] = classify.derive_fallback_preview(note.content)

    if update_kwargs:
        db.update_note(note_id, NoteUpdate(**update_kwargs))
        note = db.get_note(note_id) or note

    # Always (re)embed — content or description may have changed.
    try:
        vec = embed.engine().embed(_embedding_text(note))
        db.store_embedding(note_id, vec)
    except Exception:
        log.exception("embedding note %d failed", note_id)
