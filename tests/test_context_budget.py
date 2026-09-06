"""Tests for how build_context divides its token budget, and how
onboard.build reports that division.

Three behaviors meet here, and each one failed silently before:

* Rules are pinned but exempt from `budget_tokens`. Charged against it
  they crowded a rule-heavy project's own memories out, and once the
  allowance ran dry the pin loop broke early — dropping the *oldest
  project-specific* rules, since the sort is global-first then
  newest-first. A dropped constraint and a project that never had one
  render identically.
* The rule pin scopes in SQL. Fetching globally and filtering in Python
  applies the cap across every project, so a big corpus elsewhere can
  evict this project's rules entirely.
* The blob reports what the notes slice actually spent. Printing the
  whole payload beside the budget billed the exempt sections to the
  notes allowance and read as a large overrun.
"""
from __future__ import annotations

from app import db, graph, onboard
from app.models import NoteCreate


def _rules(project, n, size=1):
    for i in range(n):
        db.create_note(NoteCreate(
            content=f"rule {project or 'global'} {i} " + ("x " * size),
            type="rule", project=project, source="test"))


def _notes(project, n):
    for i in range(n):
        db.create_note(NoteCreate(
            content=f"memory {i} about the widget subsystem",
            type="memory", project=project, source="test"))


# --- rules are exempt from the notes budget --------------------------------

def test_rules_are_not_charged_against_budget_tokens(db_conn):
    """The whole point of the exemption: `notes_tokens_est` measures the
    BFS only, so a rule-heavy project spends its full allowance on
    memories instead of losing it to constraints it also has to ship."""
    _rules("alpha", 20, size=200)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    assert ctx["rules_tokens_est"] > 0
    assert ctx["notes_tokens_est"] == 0
    assert ctx["total_tokens_est"] == (
        ctx["notes_tokens_est"] + ctx["rules_tokens_est"]
        + ctx["skills_tokens_est"])


def test_a_rule_heavy_project_still_surfaces_its_memories(db_conn):
    """Charged against the budget, rules this size left nothing for the
    BFS and the caller saw an empty note list on a project that had 10.

    Sized to exceed the whole 4000-token allowance on its own even at the
    old 20-token envelope, so the test fails against the pre-fix code
    rather than merely passing on both sides of it."""
    _rules("alpha", 20, size=800)
    _notes("alpha", 10)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    assert ctx["notes"], "rules crowded the memories out"
    assert len(ctx["rules"]) == 20


def test_no_rule_is_dropped_when_rules_outweigh_the_budget(db_conn):
    """The early `break` on a spent budget dropped the tail of the sort,
    which is the oldest project-specific rules — the ones most likely to
    encode something this repo alone depends on."""
    _rules(None, 5, size=300)
    _rules("alpha", 5, size=300)
    ctx = graph.build_context(project="alpha", budget_tokens=100)
    assert len(ctx["rules"]) == 10
    assert ctx["rules_truncated"] is False


# --- the rule pin scopes in SQL --------------------------------------------

def test_other_projects_rules_cannot_evict_this_projects(db_conn, monkeypatch):
    """Fetch-then-filter applied the cap across every project. With the
    limit lowered to make the cap reachable, a foreign corpus newer than
    this project's rules consumed the entire fetch and `alpha` came back
    with nothing."""
    _rules("alpha", 3)
    _rules("zulu", 50)          # created later, so newest-first favors them
    monkeypatch.setattr(graph, "RULES_FETCH_LIMIT", 10)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    assert [r["project"] for r in ctx["rules"]] == ["alpha"] * 3


# --- both bounds report through rules_truncated ----------------------------

def test_fetch_limit_sets_rules_truncated(db_conn, monkeypatch):
    """This bound used to be silent — `rules_truncated` only ever meant
    the token cap, so rules lost to the fetch limit vanished unreported."""
    _rules("alpha", 12)
    monkeypatch.setattr(graph, "RULES_FETCH_LIMIT", 10)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    assert ctx["rules_truncated"] is True


def test_token_cap_sets_rules_truncated(db_conn, monkeypatch):
    _rules("alpha", 10, size=200)
    monkeypatch.setattr(graph, "RULES_TOKEN_CAP", 500)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    assert ctx["rules_truncated"] is True
    assert len(ctx["rules"]) < 10


def test_rules_truncated_is_false_on_an_ordinary_corpus(db_conn):
    _rules("alpha", 5)
    assert graph.build_context(project="alpha")["rules_truncated"] is False


# --- what the blob says ----------------------------------------------------

def test_blob_warns_when_rules_were_dropped(db_conn, monkeypatch):
    """Nothing rendered `rules_truncated`, so a dropped constraint looked
    exactly like a project that never had one."""
    _rules("alpha", 12)
    monkeypatch.setattr(graph, "RULES_FETCH_LIMIT", 10)
    blob = onboard.build(project="alpha", budget_tokens=4000)
    assert "Some rules were dropped" in blob
    # The recovery call has to be one an agent can actually make. `search`
    # takes `query` as a required positional, so a notice naming
    # `search(type='rule', ...)` with no query hands the reader a
    # TypeError where it promised the missing constraints. And `project`
    # must stay unset: the filter is an equality match, so naming this
    # project would exclude every global rule.
    assert "search(query=" in blob
    assert "search(type=" not in blob
    notice = next(l for l in blob.splitlines() if "Some rules were dropped" in l)
    assert "project=" not in notice, notice


def test_blob_is_silent_about_truncation_when_nothing_was_dropped(db_conn):
    _rules("alpha", 5)
    assert "Some rules were dropped" not in onboard.build(project="alpha")


def test_blob_bills_only_the_notes_slice_to_the_budget(db_conn):
    """The heading used to print `total_tokens_est`, which includes the
    exempt sections — agents read that as a budget overrun and said so.

    Memories are seeded so the charged and total figures actually differ;
    with an empty BFS the two are equal and the assertion proves
    nothing."""
    _rules("alpha", 10, size=100)
    _notes("alpha", 10)
    ctx = graph.build_context(project="alpha", budget_tokens=4000)
    blob = onboard.build(project="alpha", budget_tokens=4000)
    header = next(l for l in blob.splitlines()
                  if l.startswith("## relevant memories"))
    assert ctx["notes_tokens_est"] > 0
    assert ctx["total_tokens_est"] != ctx["notes_tokens_est"]
    assert f"(~{ctx['notes_tokens_est']} tokens, budget 4000" in header
    assert f"(~{ctx['total_tokens_est']} tokens" not in header
    assert "exempt from that budget" in header


# --- pinned active work is rendered, not just paid for ---------------------

def test_active_plans_are_rendered_in_the_blob(db_conn):
    """`_pin_active_work` charges plans against the budget and adds them
    to `seen_note_ids`, suppressing them from the BFS. Unrendered, the
    blob paid for content it did not contain."""
    db.create_note(NoteCreate(
        content="Ship the widget rewrite before the freeze",
        type="plan", status="pending", project="alpha", source="test"))
    blob = onboard.build(project="alpha", budget_tokens=4000)
    assert "## pending plans and tasks for `alpha`" in blob
    assert "Ship the widget rewrite" in blob
    assert "plan_complete(id=" in blob


def test_no_plans_section_when_there_is_no_tracked_work(db_conn):
    _notes("alpha", 2)
    assert "pending plans and tasks" not in onboard.build(project="alpha")


# --- crowded out is distinguished from empty -------------------------------

def test_empty_project_says_it_is_empty(db_conn):
    blob = onboard.build(project="alpha", budget_tokens=4000)
    assert "No relevant memories yet" in blob
    assert "crowded out" not in blob


def test_crowded_out_is_not_reported_as_empty(db_conn):
    """A note that doesn't fit in the remainder is indistinguishable from
    one that was never there, so the two cases have to be named apart.
    Pinned active work is the only thing that still charges the notes
    budget ahead of the BFS."""
    for i in range(40):
        db.create_note(NoteCreate(
            content=f"plan {i} " + ("detail " * 200),
            type="plan", status="pending", project="alpha", source="test"))
    _notes("alpha", 5)
    blob = onboard.build(project="alpha", budget_tokens=4000)
    assert "crowded out, not absent" in blob
    assert "No relevant memories yet" not in blob
    assert "budget_tokens=8000" in blob


# --- the envelope allowance ------------------------------------------------

def test_note_sizing_includes_the_serialization_envelope(db_conn):
    """A note's cost is its text plus the fields `_bfs_neighborhood`
    attaches. The old flat 20 undercounted by enough that a caller asking
    for 4000 tokens of notes received roughly 9,400."""
    n = db.create_note(NoteCreate(
        content="a short memory", type="memory", project="alpha",
        source="test"))
    assert graph._note_tokens(n) >= graph.NOTE_ENVELOPE_TOKENS
