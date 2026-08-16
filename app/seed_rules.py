"""First-boot seeding of universal global rules.

Ships a small set of `type='rule'`, `project=None` notes that capture
how to use infoguana correctly — preview citation discipline, link
verification, search-before-add, and so on. These are mechanics of the
system, not personal preferences; users who want personal rules add
their own.

Runs once per database lifetime, gated by a sentinel row in
`app_meta`. If a user deletes a seeded rule, it stays deleted — the
sentinel prevents resurrection on the next boot. Existing databases
that already contain rules (i.e. users upgrading from a pre-seeding
build) get the sentinel set without any new inserts, so seeded rules
never collide with hand-authored ones.

Inserts go through `db.create_note` (which fires the FTS5 trigger so
BM25 search finds them immediately) and then directly embed via the
local fastembed model so `similar` and the semantic half of `search`
work too. The Claude classify path is deliberately skipped — content
is curated, and we don't want first-boot to require an Anthropic API
key. Previews and descriptions are hand-written in this file.

If embedding fails (model can't load, fastembed missing), seeding
still completes and the rules are findable via BM25/FTS5; only
semantic retrieval is degraded. The sentinel is still set so the
seeder doesn't retry forever — a later `pipeline.process_note(id,
force_reclassify=False)` per rule, or a backfill script, can repair
embeddings.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from app.models import NoteCreate, NoteUpdate


logger = logging.getLogger(__name__)


_PROVENANCE = "Seeded global rule from infoguana OSS distribution."


GLOBAL_RULES: list[dict] = [
    {
        "description": (
            "Previews from search/similar/context are for triage, not citation — "
            "fetch the full body before stating facts or repeating `#NNN` references."
        ),
        "preview": (
            "Previews are haiku-sized triage summaries; they can omit nuance that "
            "changes meaning. Bare `#NNN` references are easy to hallucinate too. "
            "Fetch full bodies via get/get_many/expand_top before citing."
        ),
        "content": (
            "**Note previews from `search` / `similar` / `context` are for "
            "triage, not citation.** A preview is a haiku-sized summary — it "
            "tells you which notes are worth reading, but it can omit nuance, "
            "dates, qualifications, or context that change the meaning. "
            "**The same applies to bare `#NNN` references: never cite a note "
            "id without confirming it exists and says what you think it "
            "says.**\n\n"
            "**Why:** Citing from a preview leads to confidently-wrong claims "
            "that the full body would have qualified. Bare `#NNN` references "
            "are also easy to hallucinate from working memory instead of "
            "actually retrieving — both failure modes look authoritative and "
            "mislead the reader.\n\n"
            "**How to apply:** Before stating a fact, decision, design "
            "point, or recommendation that's anchored on a preview *or* an "
            "`#NNN` reference, fetch the full body via `get(id)`, "
            "`get_many(ids=[...])`, or `expand_top=N` on the next search "
            "call. Cite from verified content, not from a hand-sized summary "
            "or a remembered id. If you're only orienting yourself on what's "
            "available, previews are fine; if you're about to act on or "
            "repeat what a note says — or write its id into another note — "
            "read it first."
        ),
        "tags": ["infoguana", "retrieval", "preview", "citation", "global-rule"],
    },
    {
        "description": (
            "Don't propose link edges without reading the target's full body — "
            "speculative edges pollute the typed-edge graph."
        ),
        "preview": (
            "Speculative `link` proposals based on shared keywords pollute the "
            "graph; readers trust edges. Before proposing, fetch the target and "
            "justify the edge_type in one sentence."
        ),
        "content": (
            "Don't propose `link` edges to other notes without verifying the "
            "target's actual content first.\n\n"
            "**Why:** Preview-based or vibes-based edge suggestions (\"this "
            "also mentions X, link it?\") pollute the graph. Two notes can "
            "share a keyword while describing unrelated mechanisms, and an "
            "edge implies a relationship the graph reader will trust. "
            "Speculative links are worse than missing ones.\n\n"
            "**How to apply:** Before proposing any `link`, fetch the target "
            "note's full body (`get` or `get_many`) and confirm the "
            "relationship matches the edge_type's semantics in one specific "
            "sentence (e.g. \"this implements #N because N proposed exactly "
            "this approach\", \"this is caused_by #N because N is the "
            "incident that introduced the bug\"). If the justification is "
            "just \"both notes mention the same component/keyword,\" that's "
            "not an edge — skip it. Applies to all edge types: implements, "
            "caused_by, supersedes, references, bundled_with, "
            "prerequisite_for."
        ),
        "tags": ["graph-hygiene", "edges", "verification", "global-rule"],
    },
    {
        "description": (
            "Don't write infoguana note IDs in code comments, commits, or PRs — "
            "they're internal state that rots outside infoguana."
        ),
        "preview": (
            "Note IDs are internal infoguana state; they mean nothing to readers "
            "outside an agent session and rot when notes merge/supersede/delete. "
            "Keep code/commit/PR text self-contained. Cross-refs inside infoguana are fine."
        ),
        "content": (
            "**Never reference infoguana note IDs in code comments or "
            "docstrings.** Don't write \"See note <id>\" or \"(per <id>)\" "
            "in source files.\n\n"
            "**Why:** Note IDs are internal infoguana state — they mean "
            "nothing to anyone reading the code outside an agent session "
            "with infoguana access, and they rot if a note is merged, "
            "superseded, or deleted. Same goes for commit messages and PR "
            "descriptions: any artifact future developers read without "
            "infoguana shouldn't depend on infoguana state to make "
            "sense.\n\n"
            "**How to apply:**\n"
            "- When writing code comments, keep the substance "
            "self-contained: explain the constraint, the ambiguity, the "
            "why — directly in the comment. No \"see note #N\" pointers.\n"
            "- Same goes for commit messages, PR descriptions, and any "
            "artifact other developers will read without infoguana.\n"
            "- It's still fine (and expected) to cross-reference notes "
            "*inside infoguana itself* — that's what the typed-edge "
            "graph and `#N` references are for. Just don't leak those IDs "
            "out into the codebase."
        ),
        "tags": ["infoguana", "code-comments", "note-ids", "global-rule"],
    },
    {
        "description": (
            "Search infoguana before adding a new note — prefer update over "
            "near-duplicate to preserve links, history, and provenance."
        ),
        "preview": (
            "Near-duplicate notes fragment retrieval: BM25 and semantic search "
            "split the signal, edges land on whichever copy surfaced first. "
            "Search first; edit in place via update(id) to keep links intact."
        ),
        "content": (
            "**Search infoguana before adding a new note.** Near-"
            "duplicates fragment retrieval and break the assumption that a "
            "note id is a stable handle on an idea.\n\n"
            "**Why:** When the same concept exists across two or three "
            "notes, BM25 + semantic search splits the signal between them, "
            "edges get drawn to whichever copy happened to surface that "
            "day, and the graph stops reflecting a coherent picture. "
            "Editing an existing note in place preserves links, version "
            "history, and provenance.\n\n"
            "**How to apply:**\n"
            "- Before `add`, call `search(query=...)` or "
            "`similar(text=...)` with the concept you're about to capture. "
            "If a note already covers it, call `update(id=NNN, "
            "content=...)` to refine in place.\n"
            "- Slight angle variation isn't a reason to add a new note — "
            "append or rewrite the existing body.\n"
            "- Genuinely new concepts (different mechanism, different "
            "domain, different decision) are fine to add."
        ),
        "tags": ["infoguana", "duplicates", "search-first", "global-rule"],
    },
    {
        "description": (
            "Call tag_suggest before minting new tags — reuse vocabulary so "
            "tag-edge retrieval stays connected."
        ),
        "preview": (
            "Free-form tagging produces drift (`auth`/`authentication`/`authn`); "
            "tag-edge retrieval weights co-occurrence by IDF and loses signal "
            "across synonyms. Call tag_suggest first; reuse existing tags."
        ),
        "content": (
            "**Call `tag_suggest` before minting new tags.** Reuse existing "
            "vocabulary unless you're capturing a genuinely new concept.\n\n"
            "**Why:** Free-form tagging produces drift: `auth`, "
            "`authentication`, `auth-system`, and `authn` end up on "
            "different notes covering the same area, and tag-edge "
            "retrieval (which weights co-occurring tags by IDF) loses "
            "signal across them. The corpus accumulates a long tail of "
            "singleton tags that connect nothing. Tags are graph edges; "
            "they only work when they're shared.\n\n"
            "**How to apply:**\n"
            "- Before writing tags on `add` or `update`, call "
            "`tag_suggest(content=...)` to see existing tags ranked by "
            "frequency and semantic similarity to the note.\n"
            "- Prefer an existing tag over a new one when it captures the "
            "same area, even if the wording isn't perfect.\n"
            "- New tags are appropriate when nothing in the vocabulary "
            "fits — capture the principle, not the surface form (e.g. "
            "`rate-limiting`, not `429-fix`)."
        ),
        "tags": ["infoguana", "tags", "vocabulary", "tag-normalization", "global-rule"],
    },
    {
        "description": (
            "Capture how and why, not just the category — bad notes inflate the "
            "corpus and dilute search relevance."
        ),
        "preview": (
            "\"Fixed the rate limiting bug\" saves nothing; the mechanism plus "
            "the constraint that ruled out the obvious fix does. Include inline "
            "snippets — bare paths rot cross-repo. Structure with Why/How to apply."
        ),
        "content": (
            "**Memories must capture the *how* and *why*, not just the "
            "category.** A note titled \"fixed the rate limiting bug\" is "
            "useless to future-you; the same note with the mechanism and "
            "the constraint that made the obvious fix wrong is worth its "
            "retrieval cost.\n\n"
            "**Why:** The point of cross-project memory is to save the "
            "next session from re-deriving what you already learned. A "
            "category label doesn't save derivation — only the substance "
            "does. Bad notes inflate the corpus, dilute search relevance, "
            "and train the agent to write more bad notes.\n\n"
            "**How to apply:**\n"
            "- ❌ Bad — what without how: `\"figured out FTS5 BM25 "
            "tuning\"`, `\"fixed the rate limiting bug\"`, `\"set up "
            "Docker properly\"`.\n"
            "- ✅ Good — actionable, self-contained: `\"FTS5 BM25 for "
            "short notes: k1=1.2, b=0.65. Default b=0.75 over-penalized "
            "short content (~30-100 tokens) and tanked recall.\"`\n"
            "- Include code snippets inline. Infoguana is cross-repo, "
            "so a bare path like `foo.cpp:42` rots when accessed from a "
            "different project. Save the substance.\n"
            "- Structure feedback/project notes with a **Why:** line (the "
            "reason) and a **How to apply:** line (when this kicks in) so "
            "future-you can judge edge cases instead of blindly following "
            "the rule."
        ),
        "tags": ["infoguana", "note-quality", "memory-guidance", "global-rule"],
    },
]


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM app_meta WHERE key = 'global_rules_seeded'"
    ).fetchone()
    return row is not None


def _mark_seeded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)",
        ("global_rules_seeded", datetime.now(timezone.utc).isoformat()),
    )


def _has_existing_rules(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM notes WHERE type = 'rule' AND project IS NULL LIMIT 1"
    ).fetchone()
    return row is not None


def seed_if_needed(conn: sqlite3.Connection) -> int:
    """Insert the global rule set if this DB has never been seeded and has
    no existing global rules. Returns the number of rules inserted (0 if
    skipped). Safe to call on every boot."""
    # Imported here to avoid a circular import: db.py imports this module,
    # and create_note lives in db.
    from app import db

    _ensure_meta_table(conn)
    conn.commit()

    if _is_seeded(conn):
        return 0

    if _has_existing_rules(conn):
        # User is upgrading from a pre-seed build with hand-authored
        # rules. Don't pile seed rules on top; just mark seeded so future
        # boots don't re-check.
        _mark_seeded(conn)
        conn.commit()
        logger.info("seed_rules: existing rules present; marked seeded without insert")
        return 0

    # Insert + set preview/description for each rule. Done in two steps
    # because create_note doesn't accept description/preview (those are
    # normally filled by the classify pipeline), but update_note does.
    new_ids: list[int] = []
    for rule in GLOBAL_RULES:
        note = db.create_note(NoteCreate(
            content=rule["content"],
            type="rule",
            project=None,
            tags=rule["tags"],
            source="seed",
            confidence="stated",
            provenance_note=_PROVENANCE,
        ))
        db.update_note(note.id, NoteUpdate(
            description=rule["description"],
            preview=rule["preview"],
        ))
        new_ids.append(note.id)

    # Embed all seeded notes in one batched call. Best-effort: if the
    # local fastembed model can't load (e.g. fastembed not installed in
    # a stripped-down environment), seeding still completes — the rules
    # are findable via BM25/FTS5, just not via semantic similarity.
    try:
        from app import embed
        from app.pipeline import _embedding_text
        notes = [db.get_note(i) for i in new_ids]
        texts = [_embedding_text(n) for n in notes if n]
        vecs = embed.engine().embed_many(texts)
        for n, vec in zip([n for n in notes if n], vecs):
            db.store_embedding(n.id, vec)
    except Exception:
        logger.exception("seed_rules: embedding failed; rules will be searchable via BM25 only")

    _mark_seeded(conn)
    conn.commit()
    logger.info("seed_rules: inserted %d global rules (ids=%s)", len(new_ids), new_ids)
    return len(new_ids)
