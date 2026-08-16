# Changelog

Notable changes to infoguana. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This file starts partway through the project's life; changes before the first
entry below are recorded only in the commit history.

## Unreleased

### Added

- `skill` is a first-class note type, accepted by `NoteType`, the MCP write
  and read paths, the web edit form and browse filter, and the graph legend.
  It is authored by hand: the classifier never assigns it, by design.
- `READABLE_TYPES`, the set of types a caller may filter reads by. It is a
  superset of the write set, because `unsorted` is a real state worth listing
  but not one to assign by hand.

### Changed

- An unrecognized note type is now an error rather than a silent fallback.
  `search`, `context`, `add` and `update` return an error naming the valid
  set. Previously each spelled its check as `type if type in VALID_TYPES else
  None`: on a read that meant "no filter" rather than "no matches", so the
  caller received a full unfiltered payload it had every reason to believe was
  narrowed, and on a write it handed the note to the classifier, which cannot
  produce `rule` or `skill` at all. **This is a behavior change for existing
  callers** — a stale or misspelled type that used to return plausible-looking
  results now fails.
- `provenance_note` is clamped to 200 characters in preview-mode hits from
  `search` and `similar`. It was the one serialized field with no length
  discipline, and on the largest notes it cost several times what a preview
  is budgeted for. Full text still comes back from `expand_top`, `get` and
  `get_many`.
- Edge inference recognizes `feature`, `task`, `rule` and `skill` as type
  hints before a `#N` reference. Without them such a reference degraded to a
  *bare* proposal, which `infer_edges` discards unless asked for it, so the
  edge silently never formed.

### Fixed

- A database containing `skill` notes could not be read at all. `NoteType`
  lacked the literal, so pydantic raised `literal_error` on every row it
  materialized, taking down `/browse`, `get_note`, `hybrid_search` and
  `build_context`.
- Saving a note from the edit card no longer silently retypes it. A type
  missing from the form's whitelist left the dropdown with no matching
  option, so the browser posted the empty "auto (reclassify)" value and the
  save reset the note to `unsorted`.
