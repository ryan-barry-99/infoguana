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
# Four copies are importable constants. The browse filter's is not — it is
# an inline list inside a dict literal in a request handler — so it is
# read out of the source with `ast`. Two copies are NOT covered here, both
# because extracting them means parsing a literal out of a template: the
# colour map in `app/templates/graph.html`, and the type dropdown in
# `app/templates/_note_card.html`. That is more fragility than the checks
# are worth, but the two degrade differently and it is worth knowing which
# is which. A missing colour falls back to a default and nothing breaks. A
# missing dropdown option is worse: the select has no matching entry, so
# the browser posts the empty "auto (reclassify)" value and saving the note
# resets it to `unsorted`. The `_MANUAL_TYPES` check below is the practical
# guard on that one, since the dropdown and that constant are edited
# together or the edit route rejects the type outright.


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


@pytest.mark.parametrize("module_name,attr,excluded", [
    # `unsorted` is the classifier's own default bucket, so no MCP caller
    # should be able to write it by hand. Reads are a different question —
    # see the READABLE_TYPES check below.
    ("app.mcp_server", "VALID_TYPES", {"unsorted"}),
    # The web edit form's manual-type whitelist: everything the type
    # dropdown offers except the "auto (reclassify)" option, which posts an
    # empty string rather than 'unsorted'.
    ("app.routes.notes", "_MANUAL_TYPES", {"unsorted"}),
    # The classifier must never assign `rule` or `skill` — both are authored
    # deliberately — and never re-derive `unsorted`.
    ("app.classify", "VALID_TYPES", {"rule", "skill", "unsorted"}),
])
def test_valid_types_are_declared_note_types(
    module_name: str, attr: str, excluded: set[str]
) -> None:
    """Each type whitelist must be exactly `NoteType` minus its exclusions.

    Equality against a declared exclusion set, not a subset assertion. A
    subset catches a stale literal — a member the enum no longer has — but
    it cannot catch the opposite and more damaging case: a type added to
    `NoteType` and forgotten here. That direction fails silently, in a
    different way at each site: `mcp_server` rejects the write, so a note
    typed by hand never gets the type it asked for; `routes.notes` 400s the
    edit form, leaving the type unselectable in the card so the form posts
    "auto" and the save resets the note to 'unsorted'; `classify` logs and
    falls back to 'idea'. Naming the exclusions keeps the deliberate
    narrowing intact while making the next added type fail loudly, and
    point at the constant to update.
    """
    import importlib

    valid = getattr(importlib.import_module(module_name), attr)
    assert set(valid) == set(get_args(NoteType)) - excluded


def test_readable_types_covers_every_declared_note_type() -> None:
    """`READABLE_TYPES` is the read-filter set and must span the whole enum.

    Separate from the write gate on purpose: `unsorted` is a real state a
    caller may want to list but must not assign by hand, so the read set is
    a strict superset. Collapsing the two is what let `search(type='skill')`
    return unfiltered results while `skill` was missing from the write gate
    — an unrecognized filter used to coerce to `None`, and a `None` filter
    means "no filter" rather than "no matches", so the caller got a full
    result set it believed was narrowed. Both read sites now error instead,
    which only stays correct while this set spans the enum.
    """
    from app.mcp_server import READABLE_TYPES

    assert set(READABLE_TYPES) == set(get_args(NoteType))


# --- unrecognized types are rejected, not coerced ---------------------------
#
# These stay in the import-level suite deliberately: every guard below runs
# before the tool touches a database, so they need no fixture. That ordering
# is the property under test as much as the error is — a guard that ran after
# the lookup would still be correct, but only reachable with a database, and
# the version of this bug that shipped was one nobody could cheaply test for.


def _tool(name: str):
    """The plain function behind an MCP tool name.

    Whether the module-level name is the function itself or a FastMCP tool
    object wrapping it depends on how the server was registered, so unwrap
    only if there is something to unwrap.
    """
    import app.mcp_server as mcp_server

    tool = getattr(mcp_server, name)
    return getattr(tool, "fn", tool)


@pytest.mark.parametrize("tool,kwargs", [
    ("infoguana_search", {"query": "x", "type": "bogus"}),
    ("infoguana_add", {"content": "x", "type": "bogus"}),
    ("infoguana_update", {"id": 1, "type": "bogus"}),
    ("infoguana_context", {"project": "infoguana", "include_types": ["bogus"]}),
])
def test_unrecognized_type_is_an_error(tool: str, kwargs: dict) -> None:
    """An unknown type must fail loudly rather than degrade to a default.

    Each of these once spelled its check `type if type in VALID_TYPES else
    None` and carried on. On a read that means "no filter" rather than "no
    matches", so the caller got a full result set it believed was narrowed;
    on a write it meant the note fell through to the classifier, which
    cannot produce `rule` or `skill` at all, so a typo silently landed the
    note under the wrong type and no later session could tell.
    """
    result = _tool(tool)(**kwargs)

    assert "error" in result, f"{tool} accepted an unrecognized type"


def test_unsorted_is_readable_but_not_writable() -> None:
    """The asymmetry the two sets exist to express.

    `unsorted` is a legitimate thing to filter a read by — the browse UI
    offers it — but assigning it by hand is the classifier's job, not a
    caller's. A single set cannot say both, which is why there are two.

    Only the write half is asserted at its call site. The read half is
    checked against `READABLE_TYPES` instead: a read that *accepts* the
    type goes on to query the database, so calling it here would need a
    fixture this module deliberately does without.
    """
    from app.mcp_server import READABLE_TYPES, VALID_TYPES

    assert "unsorted" in READABLE_TYPES
    assert "unsorted" not in VALID_TYPES
    assert "error" in _tool("infoguana_add")(content="x", type="unsorted")


# --- preview payloads stay preview-sized ------------------------------------


def _note(**overrides):
    """A minimal in-memory `Note`; no database involved."""
    from datetime import datetime

    from app.models import Note

    fields = {
        "id": 1, "content": "body", "preview": "preview text", "type": "skill",
        "project": "infoguana", "tags": [], "source": "test",
        "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1),
    }
    fields.update(overrides)
    return Note(**fields)


def test_long_provenance_is_clamped_only_in_preview_mode() -> None:
    """Preview hits must not smuggle an unbounded field past the budget.

    `provenance_note` is the one serialized field with no length discipline,
    and `skill` notes carry the longest ones in the corpus — enough that a
    single hit cost several times what a preview is budgeted for, while the
    caller reasonably assumed a preview-sized response. Clamping is
    preview-only: `get` and `expand_top` callers have already accepted the
    cost of a full body.
    """
    from app.mcp_server import PREVIEW_PROVENANCE_CHARS, _note_dict

    note = _note(provenance_note="x" * 5000)

    clamped = _note_dict(note, preview=True)["provenance_note"]
    assert len(clamped) <= PREVIEW_PROVENANCE_CHARS + 1  # + the ellipsis
    assert clamped.endswith("…")
    assert _note_dict(note, preview=False)["provenance_note"] == "x" * 5000


def test_short_provenance_is_passed_through_unchanged() -> None:
    """The clamp must not touch the common case or append a false ellipsis."""
    from app.mcp_server import _note_dict

    note = _note(provenance_note="stated by the user")

    assert _note_dict(note, preview=True)["provenance_note"] == "stated by the user"


# --- edge inference recognizes every note type ------------------------------


@pytest.mark.parametrize("word", sorted(set(get_args(NoteType)) - {"unsorted"}))
def test_note_type_words_are_type_hinted_references(word: str) -> None:
    """`<type> #N` in prose must register as a type hint, not a bare `#N`.

    `_scan_text` grades a match by whether the type word captured, and
    `infer_edges` discards bare proposals unless explicitly asked for them.
    So a type missing from the prefix list does not produce a worse edge —
    it produces no edge at all, silently, which is indistinguishable from
    the note simply not referring to anything.
    """
    from app.inference import _BARE_RE

    match = _BARE_RE.search(f"see {word} #123 for context")

    assert match is not None, f"{word} #123 did not match at all"
    assert match.group(1) is not None, f"{word} #123 registered as a bare reference"
