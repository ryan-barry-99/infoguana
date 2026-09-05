"""Shared fixtures.

The suite is deliberately split by what a test needs to touch. Anything
that can be exercised as a pure function over strings gets no fixture at
all — see test_skills.py, which is the bulk of it. Tests needing a
database use `db_conn`, which builds a real schema in a tmp_path so no
test can reach the deployment's own data/infoguana.db.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Note


def make_note(content: str, *, id: int = 1, description: str | None = None,
              project: str | None = None, type: str = "skill",
              tags: list[str] | None = None) -> Note:
    """A Note carrying `content`, with the boilerplate filled in.

    Skill logic reads `content`, `description`, `id` and nothing else, so
    the rest is fixed rather than parameterized — a test that varies
    `source` or `created_at` is testing pydantic, not us.
    """
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Note(
        id=id, content=content, description=description, type=type,
        project=project, tags=tags or [], source="test",
        created_at=now, updated_at=now,
    )


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """A real initialized database in tmp_path.

    `init_db` reads `settings.db_path` at call time, so pointing that at
    tmp_path is enough to keep every test off the deployment's own
    data/infoguana.db. The module-level connection is reset on both sides
    so no test inherits another's handle.
    """
    from app import config, db

    monkeypatch.setattr(config.settings, "db_path", tmp_path / "test.db")
    db._conn = None
    conn = db.init_db()
    # init_db seeds the universal global rules on first boot. Real
    # behavior, but it means a "fresh" database already holds a dozen
    # notes — a test asserting on counts or ordering would be reading
    # seeded content it did not create. Clear them so each test starts
    # from a corpus it fully controls; tests that care about seeding
    # should assert on it explicitly rather than inherit it.
    conn.execute("DELETE FROM notes")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()
        db._conn = None
