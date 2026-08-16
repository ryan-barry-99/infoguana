"""Checks that a bare checkout is actually runnable.

Everything here passes trivially on a developer box and is only
interesting against a fresh clone — no `.env`, no `data/`, no editable
install. That is the state CI and the release checkout see, and a dirty
working tree cannot reproduce it.

The suite is deliberately import-level. Anything needing a database
belongs in a module with the `db_conn` fixture; these tests must stay
runnable before any such fixture exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import get_args

import pytest

from app.models import Note, NoteType


# Import-only, in dependency order. A module missing from this list is
# not covered — the point is that every first-party module is reachable
# without the deployment's own environment present.
FIRST_PARTY = [
    "app.config",
    "app.models",
    "app.db",
    "app.graph",
    "app.pipeline",
    "app.onboard",
]


@pytest.mark.parametrize("module", FIRST_PARTY)
def test_module_imports_without_deployment_env(module):
    """Import must not require `.env`, `data/` or an editable install."""
    __import__(module)


def test_settings_load_without_env_file():
    """Settings must fall back to defaults rather than raising.

    A required-but-unset field would make a fresh clone unimportable,
    which is a packaging bug rather than a configuration one.
    """
    from app.config import settings

    assert settings.db_path is not None


@pytest.mark.parametrize("note_type", get_args(NoteType))
def test_every_declared_note_type_is_constructible(note_type):
    """Each `NoteType` literal must survive Note validation.

    Parameterized over `NoteType` itself, so a type added to the enum is
    covered the moment it is declared. Note the limit of that: this
    catches a literal that `NoteType` declares but some other validator
    rejects, and it cannot catch the opposite — code writing a type
    string `NoteType` has never heard of. Nothing here substitutes for
    running the feature suites against a clean checkout.
    """
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    note = Note(
        id=1, content="x", type=note_type, project=None, tags=[],
        source="test", created_at=now, updated_at=now,
    )
    assert note.type == note_type
