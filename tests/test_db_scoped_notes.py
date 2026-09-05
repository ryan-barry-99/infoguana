"""Tests for db.list_scoped_notes — the two-scope fetch behind the rule
and skill pins.

Worth testing because it only misbehaves at the cap, which no real corpus
has reached yet: 49 rules against a limit of 200. The failure is silent
when it comes — the caller sets a truncation flag but cannot say which
scope was lost, and a reader seeing a full global list will not guess the
project-specific set was the casualty.
"""
from __future__ import annotations

import pytest

from app import db
from app.models import NoteCreate


def _make(project, n, type="rule"):
    for i in range(n):
        db.create_note(NoteCreate(
            content=f"{project or 'global'} {type} {i}", type=type,
            project=project, source="test"))


def test_returns_globals_and_project_notes(db_conn):
    _make(None, 2)
    _make("alpha", 3)
    _make("beta", 4)
    got = db.list_scoped_notes("rule", "alpha")
    assert len(got) == 5
    assert {n.project for n in got} == {None, "alpha"}


def test_globals_come_first(db_conn):
    _make("alpha", 3)
    _make(None, 2)
    got = db.list_scoped_notes("rule", "alpha")
    assert [n.project for n in got] == [None, None, "alpha", "alpha", "alpha"]


def test_type_is_respected(db_conn):
    _make(None, 2, type="rule")
    _make(None, 3, type="skill")
    assert len(db.list_scoped_notes("rule", "alpha")) == 2
    assert len(db.list_scoped_notes("skill", "alpha")) == 3


def test_no_project_returns_only_globals(db_conn):
    _make(None, 2)
    _make("alpha", 3)
    got = db.list_scoped_notes("rule", None)
    assert [n.project for n in got] == [None, None]


def test_many_globals_do_not_zero_out_the_project_scope(db_conn):
    """Regression: `ORDER BY project IS NULL DESC ... LIMIT n` put every
    global ahead of every project row, so once the globals of a type
    reached the limit the project's own rules — the constraints most
    specific to the session — came back empty."""
    _make(None, 20)
    _make("alpha", 5)
    got = db.list_scoped_notes("rule", "alpha", limit=10)
    assert len(got) == 10
    projects = [n.project for n in got]
    assert projects.count("alpha") > 0, "project scope was zeroed out by globals"
    assert projects.count(None) > 0, "global scope was zeroed out"


def test_many_project_notes_do_not_evict_the_globals(db_conn):
    """The mirror case, which the original ordering existed to prevent —
    it must stay prevented."""
    _make(None, 5)
    _make("alpha", 20)
    got = db.list_scoped_notes("rule", "alpha", limit=10)
    assert len(got) == 10
    assert [n.project for n in got].count(None) == 5, "all globals must survive"


def test_an_unused_share_is_lent_to_the_other_scope(db_conn):
    """A project with no rules of its own must still see every global that
    fits, not just its half."""
    _make(None, 8)
    got = db.list_scoped_notes("rule", "alpha", limit=10)
    assert len(got) == 8


def test_project_gets_the_whole_budget_when_there_are_no_globals(db_conn):
    _make("alpha", 8)
    assert len(db.list_scoped_notes("rule", "alpha", limit=10)) == 8


def test_the_limit_is_never_exceeded(db_conn):
    _make(None, 30)
    _make("alpha", 30)
    assert len(db.list_scoped_notes("rule", "alpha", limit=7)) == 7


def test_other_projects_are_excluded(db_conn):
    _make("alpha", 2)
    _make("beta", 2)
    got = db.list_scoped_notes("rule", "alpha")
    assert {n.project for n in got} == {"alpha"}


def test_newest_first_within_each_scope(db_conn):
    _make(None, 3)
    got = db.list_scoped_notes("rule", None)
    assert [n.content for n in got] == [
        "global rule 2", "global rule 1", "global rule 0"]
