"""First-boot seeding of the global skill set.

The repo ships no database — `data/` and `*.db` are gitignored — so every
skill that a fresh install is supposed to start with has to arrive as code.
The bodies live as real SKILL.md documents under `app/skill_seeds/` rather
than as string literals in this module: a skill note's body *is* a SKILL.md
file verbatim (see app/skills.py), and keeping it in a `.md` means it can be
read, diffed and linted as the document it is.

Deliberately separate from `seed_rules`, and gated on its own `app_meta`
key. Reusing the rule seeder's `global_rules_seeded` sentinel would mean no
existing install ever receives a skill — every deployment that has ever
booted already has that key set. Worse, `seed_rules._has_existing_rules`
marks the DB seeded *without inserting* whenever any global rule exists,
which every real deployment hits, so a shared key would skip skills forever
rather than merely once.
"""
import logging
import sqlite3
from pathlib import Path

from app import skills
from app.models import NoteCreate, NoteUpdate


logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).parent / "skill_seeds"

_META_KEY = "global_skills_seeded"

_PROVENANCE = (
    "Seeded on first boot from app/skill_seeds/. Edit or delete freely — "
    "the seeder runs once and will not reinstate a skill you removed."
)


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)"
    )


def _is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (_META_KEY,)
    ).fetchone()
    return row is not None


def _mark_seeded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, '1')",
        (_META_KEY,),
    )


def _has_existing_skills(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM notes WHERE type = 'skill' LIMIT 1"
    ).fetchone()
    return row is not None


def seed_documents() -> list[tuple[str, str]]:
    """(stem, body) for every seed document, sorted by filename.

    Sorted so the insert order — and therefore the seeded ids — is the same
    on every install, which keeps a fresh deployment's manifest reproducible.
    """
    if not SEED_DIR.is_dir():
        return []
    return [(p.stem, p.read_text(encoding="utf-8"))
            for p in sorted(SEED_DIR.glob("*.md"))]


def seed_if_needed(conn: sqlite3.Connection) -> int:
    """Insert the global skill set if this DB has never been skill-seeded
    and carries no skills of its own. Returns the number inserted (0 if
    skipped). Safe to call on every boot."""
    # Imported here for the same reason seed_rules does it: db.py imports
    # this module, and create_note lives in db.
    from app import db

    _ensure_meta_table(conn)
    conn.commit()

    if _is_seeded(conn):
        return 0

    if _has_existing_skills(conn):
        # An install that already has skills — added by hand, or restored
        # from a backup taken before this seeder existed. Don't pile the
        # shipped set on top of a curated one; just record that the
        # decision was made so later boots stop asking.
        _mark_seeded(conn)
        conn.commit()
        logger.info("seed_skills: skills already present; marked seeded "
                    "without insert")
        return 0

    new_ids: list[int] = []
    for stem, body in seed_documents():
        note = db.create_note(NoteCreate(
            content=body,
            type="skill",
            project=None,
            source="seed",
            confidence="stated",
            provenance_note=_PROVENANCE,
        ))
        # create_note takes neither description nor preview — those are
        # normally filled by the classify pipeline, which skips skills
        # entirely. So derive them here from the document itself, the same
        # way pipeline.process_note and graph._pin_skills do. The manifest
        # line has to be the *authored* frontmatter description, never a
        # generated summary: it is the trigger condition an agent decides
        # on, and it has to be exact.
        name, description = skills.describe(note)
        if name != stem:
            # Non-fatal, but worth saying out loud: the filename is what a
            # maintainer greps for and the frontmatter name is what
            # get_skill resolves, so a mismatch means the two disagree
            # about the skill's identity.
            logger.warning(
                "seed_skills: %s.md declares name %r; filename and "
                "frontmatter name should match", stem, name)
        db.update_note(note.id, NoteUpdate(
            description=description,
            preview=skills.preview_line(note),
        ))
        new_ids.append(note.id)

    # Embed in one batched call, best-effort — same tradeoff as the rule
    # seeder. If the local model can't load, seeding still completes and
    # the skills stay findable via BM25; only semantic similarity is lost,
    # and the context manifest doesn't use embeddings at all.
    try:
        from app import embed
        from app.pipeline import _embedding_text
        notes = [db.get_note(i) for i in new_ids]
        texts = [_embedding_text(n) for n in notes if n]
        vecs = embed.engine().embed_many(texts)
        for n, vec in zip([n for n in notes if n], vecs):
            db.store_embedding(n.id, vec)
    except Exception:
        logger.exception("seed_skills: embedding failed; skills will be "
                         "searchable via BM25 only")

    _mark_seeded(conn)
    conn.commit()
    logger.info("seed_skills: inserted %d global skills (ids=%s)",
                len(new_ids), new_ids)
    return len(new_ids)
