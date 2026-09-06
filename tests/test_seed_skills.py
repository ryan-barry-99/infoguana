"""Seeding of the shipped global skill set.

The repo ships no database, so `app/skill_seeds/*.md` is the only way a
fresh install starts with any skill at all — and the sentinel logic is
where that quietly goes wrong, since reusing the rule seeder's key would
skip skills on every deployment that has ever booted.
"""
from __future__ import annotations

import pytest

from app import db, graph, onboard, seed_rules, seed_skills, skills


def _skills_in(conn):
    return conn.execute(
        "SELECT id FROM notes WHERE type = 'skill'"
    ).fetchall()


@pytest.fixture
def unseeded(db_conn):
    """db_conn runs the real `init_db`, which already seeds — so a test
    about first-boot behavior has to clear the sentinel the boot set.
    Notes are wiped by db_conn itself."""
    db_conn.execute("DELETE FROM app_meta WHERE key = ?",
                    (seed_skills._META_KEY,))
    db_conn.commit()
    return db_conn


def _meta(conn, key):
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


# --- the shipped documents -------------------------------------------------

def test_ships_at_least_one_seed_document():
    """A packaging or path regression turns the whole feature into a
    no-op that every other test here would still pass."""
    assert seed_skills.seed_documents(), "no seed documents found on disk"


def test_seed_documents_parse_as_skill_md():
    """Each shipped body must yield an authored name and description —
    `describe` falls back to the first heading and paragraph when
    frontmatter is unparseable, which reads as a topic summary rather
    than a trigger condition and never raises."""
    from tests.conftest import make_note

    for stem, body in seed_skills.seed_documents():
        note = make_note(body)
        assert skills.parse_frontmatter(body), \
            f"{stem}.md has no parseable frontmatter"
        name, description = skills.describe(note)
        assert name == stem, f"{stem}.md declares name {name!r}"
        assert description.strip(), f"{stem}.md has an empty description"


def test_seed_documents_are_sorted_deterministically():
    stems = [stem for stem, _ in seed_skills.seed_documents()]
    assert stems == sorted(stems)


# --- first boot ------------------------------------------------------------

def test_fresh_db_receives_the_shipped_skills(unseeded):
    """The db_conn fixture clears notes after init_db, so seed explicitly."""
    inserted = seed_skills.seed_if_needed(unseeded)
    assert inserted == len(seed_skills.seed_documents())
    assert len(_skills_in(unseeded)) == inserted


def test_seeded_skill_carries_authored_description_not_a_summary(unseeded):
    """The manifest line is the trigger condition an agent decides on, so
    it has to be the frontmatter text verbatim, not a generated preview."""
    seed_skills.seed_if_needed(unseeded)
    note = db.list_notes(type="skill", limit=10)[0]
    _, authored = skills.describe(note)
    assert note.description == authored
    assert note.preview == skills.preview_line(note)


def test_seeded_skills_are_global(unseeded):
    seed_skills.seed_if_needed(unseeded)
    assert all(n.project is None for n in db.list_notes(type="skill", limit=10))


def test_seeding_is_idempotent(unseeded):
    first = seed_skills.seed_if_needed(unseeded)
    assert first > 0
    assert seed_skills.seed_if_needed(unseeded) == 0
    assert len(_skills_in(unseeded)) == first


def test_deleted_skill_is_not_reinstated(unseeded):
    """The sentinel, not the row count, is what stops a re-insert — a user
    who deletes a shipped skill should not get it back every boot."""
    seed_skills.seed_if_needed(unseeded)
    unseeded.execute("DELETE FROM notes WHERE type = 'skill'")
    unseeded.commit()
    assert seed_skills.seed_if_needed(unseeded) == 0
    assert _skills_in(unseeded) == []


def test_existing_skills_are_not_piled_on(unseeded):
    """An install with hand-authored skills gets marked seeded without an
    insert, mirroring seed_rules' behavior for hand-authored rules."""
    db.create_note(_skill_create("---\nname: mine\ndescription: x\n---\n\nbody"))
    assert seed_skills.seed_if_needed(unseeded) == 0
    assert len(_skills_in(unseeded)) == 1
    assert _meta(unseeded, "global_skills_seeded") == "1"


def _skill_create(content):
    from app.models import NoteCreate
    return NoteCreate(content=content, type="skill", project=None, source="test")


# --- the sentinel is the whole point ---------------------------------------

def test_rule_sentinel_does_not_suppress_skill_seeding(unseeded):
    """Regression guard for the failure this module exists to avoid.

    Every deployment that has ever booted already carries
    `global_rules_seeded`. If skills shared that key, no existing install
    would ever receive one — and `seed_rules._has_existing_rules` sets the
    key *without inserting* whenever a global rule exists, which every
    real deployment hits, so the skip would be permanent rather than
    one-time.
    """
    seed_rules._ensure_meta_table(unseeded)
    seed_rules._mark_seeded(unseeded)
    unseeded.commit()
    assert seed_rules.seed_if_needed(unseeded) == 0

    assert seed_skills.seed_if_needed(unseeded) > 0
    assert len(_skills_in(unseeded)) == len(seed_skills.seed_documents())


def test_skill_and_rule_sentinels_are_distinct_keys(unseeded):
    """seed_rules hardcodes its key rather than exposing a constant, so
    assert against the DB: after both seeders run, two distinct rows."""
    seed_rules.seed_if_needed(unseeded)
    seed_skills.seed_if_needed(unseeded)
    assert _meta(unseeded, "global_rules_seeded") is not None
    assert _meta(unseeded, seed_skills._META_KEY) is not None
    assert seed_skills._META_KEY != "global_rules_seeded"


def test_init_db_seeds_skills_on_first_boot(tmp_path, monkeypatch):
    """End-to-end through the real boot path, not the helper — a seeder
    nobody calls is the other way this ships as a no-op."""
    from app import config

    monkeypatch.setattr(config.settings, "db_path", tmp_path / "boot.db")
    db._conn = None
    conn = db.init_db()
    try:
        assert len(_skills_in(conn)) == len(seed_skills.seed_documents())
    finally:
        conn.close()
        db._conn = None


# --- what the agent actually sees ------------------------------------------

def test_seeded_skill_reaches_the_context_manifest(unseeded):
    seed_skills.seed_if_needed(unseeded)
    ctx = graph.build_context(project="anything", budget_tokens=4000)
    names = [s["name"] for s in ctx["skills"]]
    assert names == [stem for stem, _ in seed_skills.seed_documents()]
    # Bodies must stay out of the payload; that is the entire design.
    assert all("content" not in s for s in ctx["skills"])
    assert ctx["skills_tokens_est"] > 0
    assert ctx["skills_truncated"] is False


def test_seeded_skill_renders_in_the_onboard_blob(unseeded):
    seed_skills.seed_if_needed(unseeded)
    blob = onboard.build(project="anything", budget_tokens=4000)
    assert "## skills available" in blob
    for stem, _ in seed_skills.seed_documents():
        assert f"- {stem} (#" in blob


def test_manifest_cost_is_not_billed_to_the_notes_budget(unseeded):
    """The heading prints what the BFS spent; the exempt sections are
    reported in their own clause. Billing them to the notes allowance is
    what made agents report a 3x budget overrun that never happened.

    Both exempt sections count: the manifest and the pinned rules. The
    heading is asserted against `notes_tokens_est` rather than a
    subtraction from the total, so it stays honest if a third exempt
    section is ever added."""
    seed_skills.seed_if_needed(unseeded)
    ctx = graph.build_context(project="anything", budget_tokens=4000)
    blob = onboard.build(project="anything", budget_tokens=4000)
    header = next(l for l in blob.splitlines()
                  if l.startswith("## relevant memories"))
    exempt = ctx["rules_tokens_est"] + ctx["skills_tokens_est"]
    assert ctx["total_tokens_est"] - exempt == ctx["notes_tokens_est"]
    assert f"(~{ctx['notes_tokens_est']} tokens, budget 4000" in header, header
    assert f"add ~{exempt} more, exempt" in header, header


# --- regressions from the first review round -------------------------------

def test_project_scoped_skill_does_not_suppress_the_shipped_globals(unseeded):
    """A skill someone wrote for one repo says nothing about whether this
    install wants the shipped global set. Treating it as a veto withheld
    infoguana-onboard permanently, since the sentinel latches."""
    from app.models import NoteCreate
    db.create_note(NoteCreate(
        content="---\nname: local\ndescription: x\n---\n\nbody",
        type="skill", project="some-repo", source="test"))
    assert seed_skills.seed_if_needed(unseeded) == len(
        seed_skills.seed_documents())
    names = {skills.describe(n)[0]
             for n in db.list_notes(type="skill", limit=50)}
    assert "infoguana-onboard" in names


def test_a_global_skill_still_suppresses_seeding(unseeded):
    """The other half of the same predicate: a curated *global* set is what
    the shipped one must not be piled onto."""
    from app.models import NoteCreate
    db.create_note(NoteCreate(
        content="---\nname: mine\ndescription: x\n---\n\nbody",
        type="skill", project=None, source="test"))
    assert seed_skills.seed_if_needed(unseeded) == 0


def test_seeded_preview_is_clamped(unseeded, monkeypatch):
    """preview_line returns the description's whole first sentence, and
    4.1% of real SKILL.md descriptions run past the preview bound."""
    from app import classify
    long_desc = "Use this when " + "something happens and " * 30 + "you care."
    body = f"---\nname: verbose\ndescription: {long_desc}\n---\n\n# V\n\nbody\n"
    monkeypatch.setattr(seed_skills, "seed_documents",
                        lambda: [("verbose", body)])
    seed_skills.seed_if_needed(unseeded)
    note = db.list_notes(type="skill", limit=10)[0]
    assert len(skills.preview_line(note)) > classify.PREVIEW_MAX_CHARS, \
        "fixture no longer exercises the clamp"
    assert len(note.preview) <= classify.PREVIEW_MAX_CHARS


def test_empty_seed_directory_does_not_latch_the_sentinel(unseeded, monkeypatch):
    """An absent data directory means the package did not ship it, not that
    this database declined the skills. Latching would make fixing the
    packaging useless — no later boot would retry."""
    monkeypatch.setattr(seed_skills, "seed_documents", lambda: [])
    assert seed_skills.seed_if_needed(unseeded) == 0
    assert _meta(unseeded, seed_skills._META_KEY) is None
    # And a later boot, once the documents are there, still seeds.
    assert seed_skills.seed_if_needed(unseeded) == len(
        seed_skills.seed_documents())


def test_the_manifest_admits_a_large_skill_corpus(unseeded):
    """The bounds are runaway backstops, not a ration on writing skills.

    250 skills at the measured median entry cost is well inside both
    bounds; under the previous 6000-token cap this truncated at ~60. The
    transport, not this cap, is what ultimately limits the manifest —
    see the SKILLS_TOKEN_CAP comment in graph.py.
    """
    from app.models import NoteCreate
    desc = ("Use when the caller needs the thing done. " * 4).strip()
    for i in range(250):
        db.create_note(NoteCreate(
            content=f"---\nname: skill-{i:03d}\ndescription: {desc}\n---\n\nbody",
            type="skill", project=None, source="test"))
    ctx = graph.build_context(project="anything", budget_tokens=4000)
    assert len(ctx["skills"]) == 250
    assert ctx["skills_truncated"] is False
    # And the notes budget is untouched by a manifest this size. Asserted
    # against `notes_tokens_est` rather than a subtraction from the total,
    # which now also carries the exempt rules — that form passes here only
    # because this fixture has none, and would break on a fixture that did.
    assert ctx["notes_tokens_est"] <= 4000
