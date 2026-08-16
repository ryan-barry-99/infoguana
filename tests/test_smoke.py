"""Checks that a bare checkout is actually runnable.

Everything here passes trivially on a developer box and is only
interesting against a fresh clone — no `.env`, no `data/`. That is the
state CI and the release checkout see, and a dirty working tree cannot
reproduce it.

The suite is deliberately import-level. Anything needing a database
belongs in a module with the `db_conn` fixture; these tests must stay
runnable before any such fixture exists.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

import app
from app.models import NoteType


# Enumerated rather than hand-listed, so a module added later is covered
# without anyone remembering to extend a list. The point is that every
# first-party module is reachable without the deployment's own
# environment present.
def _first_party_modules() -> list[str]:
    import pkgutil

    return sorted(m.name for m in pkgutil.walk_packages(app.__path__, "app."))


def test_module_enumeration_found_the_entrypoints() -> None:
    """Guard the enumeration itself.

    If `app` ever resolves to a namespace package, gets shadowed, or the
    walk otherwise yields nothing, `_first_party_modules` returns an
    empty list and the parametrized test below is *skipped* rather than
    failed — an empty parametrize list is not an error, so the whole
    import sweep would vanish on a green run.

    Asserting on the two entrypoints rather than a module count: they
    are the things the module docstring claims a bare checkout can
    actually run, and unlike a threshold they do not need revisiting
    every time a module is added or removed.
    """
    modules = _first_party_modules()
    assert {"app.main", "app.mcp_server"} <= set(modules)


@pytest.mark.parametrize("module", _first_party_modules())
def test_module_imports_without_deployment_env(module: str) -> None:
    """Import must not require `.env`, `data/` or an editable install."""
    __import__(module)


# --- note-type declaration drift -------------------------------------------
#
# `NoteType` is not the only place the set of note types is written down.
# Adding a literal there leaves the other copies stale, and every test
# that only reads `NoteType` stays green while the new type is unusable
# in the UI and silently dropped by the type filters. These check the
# copies against the enum instead of checking the enum against itself.
#
# Two copies are importable constants. The browse filter's is not — it is
# an inline list inside a dict literal in a request handler — so it is
# read out of the source with `ast`. The colour map in
# `app/templates/graph.html` is NOT covered here: extracting it means
# parsing a JavaScript object literal out of a Jinja template, which is
# more fragility than the check is worth. A type added without a colour
# there degrades to a default colour rather than breaking, which is why
# it is the one copy left unguarded.


def _browse_filter_type_lists() -> list[list[str]]:
    """Every `all_types` list literal in the browse routes.

    Parsed rather than imported: the lists live inline in dict literals
    inside request handlers, so there is no constant to read. Anchored
    off the package location rather than the working directory so the
    test does not depend on where pytest was invoked from.
    """
    source = (Path(app.__file__).parent / "routes" / "views.py").read_text()
    found: list[list[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "all_types"):
                continue
            if isinstance(value, ast.List):
                found.append([
                    e.value for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ])
    return found


def test_browse_filter_lists_were_found() -> None:
    """Guard the parser itself.

    If `all_types` is renamed or restructured, `_browse_filter_type_lists`
    silently returns nothing and the parametrized test below vanishes
    instead of failing — an empty parametrize list is not an error.
    """
    assert _browse_filter_type_lists(), "no all_types literal found in views.py"


@pytest.mark.parametrize("types", _browse_filter_type_lists())
def test_browse_filter_offers_every_note_type(types: list[str]) -> None:
    """The browse filter must offer exactly the declared types.

    Equality, not subset: a type missing here is unreachable in the UI,
    and a type here that `NoteType` does not declare filters to nothing.
    """
    assert set(types) == set(get_args(NoteType))


@pytest.mark.parametrize("module_name", ["app.mcp_server", "app.classify"])
def test_valid_types_are_declared_note_types(module_name: str) -> None:
    """`VALID_TYPES` sets must not name a type `NoteType` lacks.

    Subset, not equality, and deliberately so — both are narrower than
    `NoteType` on purpose. `app.classify` excludes `rule` and `unsorted`
    because the classifier must never assign them, and `app.mcp_server`
    excludes `unsorted` from its search filter. What this catches is a
    typo or a stale literal: a member here that no longer exists in the
    enum, which in `mcp_server` coerces the filter to `None` and silently
    returns unfiltered results.
    """
    import importlib

    valid = importlib.import_module(module_name).VALID_TYPES
    assert set(valid) <= set(get_args(NoteType))
