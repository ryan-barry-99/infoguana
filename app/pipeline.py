"""Background processing for new notes: embed + classify."""
import logging

from app import classify, db, embed, skills
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
    explicit type still get one Haiku-quality preview at creation time.
    Type/tags/project/description from classify are only
    persisted when the note's existing fields are in scope for re-derivation
    (unsorted-on-create or explicit force_reclassify) *and* its current type
    is one the classifier can actually return; the preview piggybacks on the
    same call regardless. Preview always ends populated — falls back
    to first-line truncation when classify is skipped or unavailable."""
    note = db.get_note(note_id)
    if not note:
        log.warning("process_note: note %d not found", note_id)
        return

    # A skill note is a SKILL.md document that already states its own name
    # and trigger condition in frontmatter, so there is nothing for the
    # classifier to derive: its type is human-assigned, its description is
    # authored, and its preview is that same manifest line. Calling Haiku
    # would spend a request to produce a summary of a document that
    # already summarizes itself — and one it can't type correctly, since
    # `skill` isn't in classify.VALID_TYPES. Embedding still runs below;
    # that's local.
    if note.type == "skill":
        # Derived from the document's own shape (see skills.preview_line),
        # then through clamp_preview like every other write to this column —
        # skills.describe bounds at MANIFEST_DESCRIPTION_CHARS, which is a
        # *manifest* bound, and the manifest is budget-exempt where a
        # `search` hit is not.
        preview = classify.clamp_preview(skills.preview_line(note))
        db.apply_classification(note_id, preview=preview)
        note = db.get_note(note_id) or note
        _embed(note)
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
        # `rule` and `skill` are human-assigned and absent from
        # classify.VALID_TYPES, so the classifier can never return them —
        # _parse coerces anything unrecognized to 'idea'. Adopting its
        # answer on a force_reclassify would therefore overwrite the type
        # a person deliberately chose, dropping the note out of the
        # context pin it was typed for. Editing a skill's body would
        # quietly turn it into a reference. The preview refresh below
        # still runs, which is the part these notes actually want.
        reclassifiable = (note.type == "unsorted"
                          or note.type in classify.VALID_TYPES)
        if reclassifiable and (note.type == "unsorted" or force_reclassify):
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
        # Classifier fields are system-derived — bypass the snapshot path so
        # they don't read as a user edit in version history. Both first-time
        # classify and force_reclassify go through here; the user's intent
        # is "classify this", not "edit fields one-by-one".
        db.apply_classification(note_id, **update_kwargs)
        note = db.get_note(note_id) or note

    # Always (re)embed — content or description may have changed.
    _embed(note)


def _embed(note) -> None:
    try:
        vec = embed.engine().embed(_embedding_text(note))
        db.store_embedding(note.id, vec)
    except Exception:
        log.exception("embedding note %d failed", note.id)
